from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.clock import FrozenClock  # noqa: E402
from backend.llm import (  # noqa: E402
    MAX_RETRIES,
    MAX_TOKENS,
    TEMPERATURE,
    THINKING_MODE,
    TIMEOUT_SECONDS,
    LiveLLM,
    StubLLM,
)
from backend.pipeline import handle_message  # noqa: E402
from backend.query import PROMPT_PATH as PLANNER_PROMPT_PATH  # noqa: E402
from backend.query import QUERY_TYPES, week_start  # noqa: E402
from backend.router import PROMPT_PATH as ROUTER_PROMPT_PATH  # noqa: E402
from backend.storage import FakeStorage  # noqa: E402
from backend.trace import Tracer  # noqa: E402

from eval.run_intent_eval import (  # noqa: E402
    RESULTS_DIR,
    TRACE_PATH,
    add_matrix_args,
    build_clients,
    client_model,
    effective_temperature,
    git_commit,
    load_dotenv,
    safe_div,
    sha256_file,
    snapshot,
)
from eval.stability import (  # noqa: E402
    aggregate_stability,
    aggregate_usage,
    collect_usages,
    render_stability,
)


DATASET_PATH = ROOT / "eval" / "datasets" / "query-dataset.jsonl"


def load_cases(view: str) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        for field in ("fixture", "now", "expected_type", "expected_count", "source"):
            if field not in case:
                raise ValueError(f"{case['case_id']} is missing required field {field!r}")
        if case["expected_type"] not in QUERY_TYPES:
            raise ValueError(f"{case['case_id']} has unknown type {case['expected_type']!r}")
    if view == "all":
        return cases
    return [case for case in cases if view in case["views"]]


def seed_storage(case: dict[str, Any]) -> FakeStorage:
    """每条 Case 一个独立的内存存储：天然隔离，跑完即弃，并行也不冲突。"""
    storage = FakeStorage()
    for index, item in enumerate(case["fixture"], start=1):
        storage.rows.append(
            {
                "id": f"seed-{index:03d}",
                "ts": item["ts"],
                "state": item.get("state", "complete"),
                "trace_id": "t-seed",
                "exercise": item["exercise"],
                "weight_kg": item.get("weight_kg"),
                "sets": item.get("sets"),
                "reps": item.get("reps"),
                "duration_min": item.get("duration_min"),
                "distance_km": item.get("distance_km"),
            }
        )
    return storage


def stub_plan(case: dict[str, Any]) -> str:
    """stub 剧本按 Case 的期望类型生成，验证的是执行与编排，不是模型理解力。"""
    now = FrozenClock(case["now"]).now()
    today = now.strftime("%Y-%m-%d")
    if case["expected_type"] == "unsupported":
        return '{"type":"unsupported"}'
    if case["expected_type"] == "list_by_date":
        # 从 fixture 推出该查哪天：取期望条数>0 时命中的那天，否则用今天
        target = today
        for item in case["fixture"]:
            local = datetime.fromisoformat(item["ts"]).astimezone(now.tzinfo)
            if case["expected_count"] > 0 and item["exercise"] in case["expected_exercises"]:
                target = local.strftime("%Y-%m-%d")
                break
        return json.dumps({"type": "list_by_date", "date": target})
    exercise = case["expected_exercises"][0] if case["expected_exercises"] else "卧推"
    return json.dumps(
        {
            "type": "count_by_exercise",
            "exercise": exercise,
            "from": week_start(now),
            "to": today,
        },
        ensure_ascii=False,
    )


def assert_contract(result: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[tuple[str, bool]] = []
    if result.get("ok") is True:
        records = result.get("records")
        checks.append(("records_is_list", isinstance(records, list)))
        checks.append(("count_matches_len", result.get("count") == len(records or [])))
        checks.append(("query_type_enum", (result.get("query") or {}).get("type") in QUERY_TYPES))
        checks.append(("supported_is_bool", isinstance(result.get("supported"), bool)))
    else:
        checks.append(("no_records_on_failure", "records" not in result))
    return [{"name": name, "pass": passed} for name, passed in checks]


def assert_target(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    plan = result.get("query") or {}
    records = result.get("records") or []
    actual_exercises = [row.get("exercise") for row in records]
    return [
        {"name": "plan_type_equals", "pass": plan.get("type") == case["expected_type"]},
        {"name": "count_equals", "pass": result.get("count") == case["expected_count"]},
        {"name": "supported_equals", "pass": result.get("supported") == case["expected_supported"]},
        # 顺序也是口径的一部分：口径 5 要求按写入时间倒序
        {"name": "exercises_in_order", "pass": actual_exercises == case["expected_exercises"]},
    ]


def assert_protection(
    case: dict[str, Any], result: dict[str, Any], storage: FakeStorage
) -> list[dict[str, Any]]:
    """第三问：查询是只读的，且边界外的记录不许混进来。"""
    seeded = len(case["fixture"])
    records = result.get("records") or []
    expected_set = Counter(case["expected_exercises"])
    actual_set = Counter(row.get("exercise") for row in records)
    return [
        {"name": "query_no_write", "pass": len(storage.read_all()) == seeded},
        {"name": "no_extra_records", "pass": actual_set == expected_set},
    ]


def assert_consistency(
    result: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    exec_event = next((e for e in events if e["event"] == "query_executed"), None)
    checks = [
        {"name": "single_trace_id", "pass": len({e["trace_id"] for e in events}) == 1},
        {
            "name": "trace_plan_matches",
            "pass": exec_event is not None
            and exec_event["payload"].get("type") == (result.get("query") or {}).get("type"),
        },
        {
            "name": "trace_count_matches",
            "pass": exec_event is not None
            and exec_event["payload"].get("count") == result.get("count"),
        },
    ]
    return checks


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    storage = seed_storage(case)
    clock = FrozenClock(case["now"])  # Case 自带时间，明年跑结果不变
    tracer = Tracer(TRACE_PATH)

    if run_mode == "stub":
        router_llm = StubLLM(raw_text=json.dumps({"intent": "query", "confidence": 0.9}))
        planner_llm = StubLLM(raw_text=stub_plan(case))
    else:
        if live_llm is None:
            raise RuntimeError("live LLM is not initialized")
        router_llm = planner_llm = live_llm

    actual = handle_message(
        case["input"], router_llm, planner_llm, storage, tracer,
        query_llm=planner_llm, clock=clock,
    )

    contract = assert_contract(actual)
    target = assert_target(case, actual) if actual.get("ok") else []
    protection = assert_protection(case, actual, storage)
    consistency = assert_consistency(actual, tracer.events) if actual.get("ok") else []
    all_checks = contract + target + protection + consistency
    failed = [check["name"] for check in all_checks if not check["pass"]]

    if tracer.write_failed:
        verdict = "ERROR"
    elif not actual.get("ok") and actual.get("error_code") in {"llm_timeout", "llm_api_error"}:
        verdict = "ERROR"
    elif case.get("tier") == "observation":
        verdict = "REVIEW"
    else:
        verdict = "PASS" if not failed else "FAIL"

    return {
        "case_id": case["case_id"],
        "input": case["input"],
        "now": case["now"],
        "category": case["category"],
        "risk": case.get("risk", "normal"),
        "views": case["views"],
        "expected_type": case["expected_type"],
        "actual_type": (actual.get("query") or {}).get("type") or actual.get("error_code"),
        "expected_count": case["expected_count"],
        "actual_count": actual.get("count"),
        "verdict": verdict,
        "pass": verdict == "PASS",
        "failed_checks": failed,
        "assertion_total": len(all_checks),
        "assertion_passed": sum(1 for c in all_checks if c["pass"]),
        "stage": actual.get("stage"),
        "trace_id": actual.get("trace_id"),
        "usages": collect_usages(tracer.events),
    }


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(r["verdict"] for r in results)
    evaluated = [r for r in results if r["verdict"] in {"PASS", "FAIL"}]
    confusion = {t: Counter() for t in QUERY_TYPES}
    for r in evaluated:
        if r["expected_type"] in confusion:
            confusion[r["expected_type"]][r["actual_type"]] += 1
    total = sum(r["assertion_total"] for r in evaluated)
    passed = sum(r["assertion_passed"] for r in evaluated)
    return {
        "total": len(results),
        "verdicts": {s: verdicts.get(s, 0) for s in ("PASS", "FAIL", "REVIEW", "ERROR")},
        "case_pass_rate": round(safe_div(verdicts.get("PASS", 0), len(evaluated)), 4),
        "assertion_pass_rate": round(safe_div(passed, total), 4),
        "assertion_total": total,
        "assertion_passed": passed,
        "query_write_violations": sum(
            1 for r in evaluated if "query_no_write" in r["failed_checks"]
        ),
        "boundary_failures": sum(
            1 for r in evaluated if r["verdict"] == "FAIL" and r["category"].startswith("boundary-")
        ),
        "type_confusion": {k: dict(v) for k, v in confusion.items()},
    }


def render_report(metadata, metrics_by_run, results) -> str:
    lines = [
        "# Query Eval Report",
        "",
        f"- generated_at: {metadata['generated_at']}",
        f"- view: {metadata['view']}",
        f"- run_mode: {metadata['run_mode']}",
        f"- model: {metadata['model']}",
        f"- git_commit: {metadata['git_commit']}",
        f"- router_prompt_hash: {metadata['router_prompt_hash']}",
        f"- planner_prompt_hash: {metadata['planner_prompt_hash']}",
        f"- dataset_hash: {metadata['dataset_hash']}",
        f"- temperature: {metadata['temperature']}",
        f"- runs: {metadata['runs']}",
        "",
        "> 每条 Case 自带 fixture 与 now，时间已冻结，结果可复现。",
        "",
    ]
    for entry in metrics_by_run:
        m = entry["metrics"]
        lines.extend(
            [
                f"## {entry['model']} · Run {entry['run_index']}",
                "",
                "### 结果口径",
                "",
                f"- 【核心结果】Case 级 {m['case_pass_rate']:.4f}"
                f"（PASS {m['verdicts']['PASS']} / FAIL {m['verdicts']['FAIL']}）",
                f"- 【断言明细】断言级 {m['assertion_pass_rate']:.4f}"
                f"（{m['assertion_passed']} / {m['assertion_total']}）",
                f"- 【待复核】REVIEW {m['verdicts']['REVIEW']}",
                f"- 【执行异常】ERROR {m['verdicts']['ERROR']}（不计入通过率）",
                "",
                "### 硬门禁",
                "",
                f"- 查询写入违规：{m['query_write_violations']}（必须为 0）",
                f"- 时间边界用例失败：{m['boundary_failures']}（必须为 0）",
                "",
                "### 查询类型混淆矩阵",
                "",
                "```json",
                json.dumps(m["type_confusion"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    lines.extend(
        render_stability(
            aggregate_stability(results), aggregate_usage(results), metadata["runs"]
        )
    )
    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| model | run | case_id | now | expected_type | actual | 条数 期望/实际 | verdict | 失败断言 | trace_id |",
            "|---|---:|---|---|---|---|---|:---:|---|---|",
        ]
    )
    for r in results:
        lines.append(
            f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['now'][:10]} | "
            f"{r['expected_type']} | "
            f"{r['actual_type']} | {r['expected_count']}/{r['actual_count']} | {r['verdict']} | "
            f"{', '.join(r['failed_checks']) or '-'} | {r['trace_id']} |"
        )
    badcases = [r for r in results if r["verdict"] != "PASS"]
    lines.extend(["", "## Badcases", ""])
    if not badcases:
        lines.append("None.")
    else:
        lines.extend(["| model | run | case_id | input | 失败断言 |", "|---|---:|---|---|---|"])
        for r in badcases:
            lines.append(
                f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['input']} | "
                f"{', '.join(r['failed_checks']) or '-'} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--views", default="discovery", choices=["discovery", "locked", "regression", "all"]
    )
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    add_matrix_args(parser)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    cases = load_cases(args.views)
    if not cases:
        raise SystemExit("no cases selected")
    clients = build_clients(args.run_mode, args.models, args.temperature)

    before = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    all_results, metrics_by_run = [], []
    for llm in clients:
        model_name = client_model(llm)
        for run_index in range(1, args.runs + 1):
            run_results = []
            for case in cases:
                result = run_case(case, args.run_mode, llm)
                result["run_index"] = run_index
                result["model"] = model_name
                run_results.append(result)
            all_results.extend(run_results)
            metrics_by_run.append({
                "model": model_name,
                "run_index": run_index,
                "metrics": calculate_metrics(run_results),
            })
    after = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    if before != after:
        raise SystemExit("unexpected business file write detected")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"query-results-{timestamp}.jsonl"
    report_path = RESULTS_DIR / f"query-report-{timestamp}.md"
    result_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n", encoding="utf-8"
    )
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "view": args.views,
        "run_mode": args.run_mode,
        "model": ", ".join(client_model(llm) for llm in clients),
        "runs": args.runs,
        "git_commit": git_commit(),
        "router_prompt_hash": sha256_file(ROUTER_PROMPT_PATH),
        "planner_prompt_hash": sha256_file(PLANNER_PROMPT_PATH),
        "dataset_hash": sha256_file(DATASET_PATH),
        "temperature": effective_temperature(args),
        "max_tokens": MAX_TOKENS,
        "thinking": THINKING_MODE,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
    }
    report_path.write_text(render_report(metadata, metrics_by_run, all_results), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.relative_to(ROOT)),
                "report_path": str(report_path.relative_to(ROOT)),
                "metrics": metrics_by_run,
            },
            ensure_ascii=False,
        )
    )
    return 0 if all(r["verdict"] in {"PASS", "REVIEW"} for r in all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
