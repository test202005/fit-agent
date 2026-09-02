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

from backend.extractor import PROMPT_PATH as EXTRACT_PROMPT_PATH  # noqa: E402
from backend.extractor import QUANT_FIELDS, STATES  # noqa: E402
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
from backend.router import PROMPT_PATH as ROUTER_PROMPT_PATH  # noqa: E402
from backend.storage import FakeStorage, RECORD_FIELDS  # noqa: E402
from backend.trace import Tracer  # noqa: E402

# 复用 iter-1 已验证的通用件，不复制一份
from eval.run_intent_eval import (  # noqa: E402
    RESULTS_DIR,
    TRACE_PATH,
    git_commit,
    load_dotenv,
    safe_div,
    sha256_file,
    snapshot,
)


DATASET_PATH = ROOT / "eval" / "datasets" / "extract-dataset.jsonl"
EXPECTED_NODES = {"router", "extractor", "storage"}


def load_cases(view: str) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        for field in ("assertion", "source", "expected_state", "expected_write_count"):
            if field not in case:
                raise ValueError(f"{case['case_id']} is missing required field {field!r}")
        if case["expected_state"] not in (*STATES, "router_rejected"):
            raise ValueError(f"{case['case_id']} has unknown state {case['expected_state']!r}")
    if view == "all":
        return cases
    return [case for case in cases if view in case["views"]]


def normalize(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in RECORD_FIELDS}


def assert_contract(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """第一问：结构与枚举。被上游拦下的响应有自己的形状，不能套抽取层的契约。"""
    checks: list[tuple[str, bool]] = []
    if case.get("expected_stage") == "router":
        checks.append(("router_shape_intent", result.get("intent") in {"query", "reject"}))
        checks.append(("no_extract_fields", "records" not in result and "state" not in result))
        return [{"name": name, "pass": passed} for name, passed in checks]
    if result.get("ok") is True:
        checks.append(("state_enum", result.get("state") in STATES))
        records = result.get("records")
        checks.append(("records_is_list", isinstance(records, list)))
        if isinstance(records, list):
            checks.append(
                (
                    "record_fields",
                    all(
                        isinstance(r, dict) and set(RECORD_FIELDS) <= set(r)
                        for r in records
                    ),
                )
            )
            checks.append(
                (
                    "quant_types",
                    all(
                        r.get(f) is None or isinstance(r.get(f), (int, float))
                        for r in records
                        for f in QUANT_FIELDS
                    ),
                )
            )
        checks.append(("written_ids_present", isinstance(result.get("written_ids"), list)))
    else:
        checks.append(("no_records_on_failure", "records" not in result))
    return [{"name": name, "pass": passed} for name, passed in checks]


def assert_target(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """第二问：该抽的抽对了吗。"""
    if case.get("expected_stage") == "router":
        # 非 record 输入应被上游拦下，压根走不到抽取层
        return [
            {"name": "stopped_at_router", "pass": result.get("stage") == "router"},
            {"name": "not_routed_to_record", "pass": result.get("intent") != "record"},
        ]
    expected = [normalize(r) for r in case["expected_records"]]
    actual = [normalize(r) for r in result.get("records", [])]
    return [
        {"name": "state_equals", "pass": result.get("state") == case["expected_state"]},
        {"name": "record_count_equals", "pass": len(actual) == len(expected)},
        {"name": "records_equal", "pass": actual == expected},
    ]


def assert_protection(
    case: dict[str, Any], result: dict[str, Any], storage: FakeStorage
) -> list[dict[str, Any]]:
    """第三问：不该写的没写。写入条数必须精确，多写少写都算失败。"""
    rows = storage.read_all()
    checks = [
        {"name": "write_count_exact", "pass": len(rows) == case["expected_write_count"]},
    ]
    if case["expected_state"] in ("invalid", "router_rejected"):
        # 退出标准 3：抽不出内容一律零写入
        checks.append({"name": "invalid_no_write", "pass": len(rows) == 0})
    return checks


def assert_consistency(
    result: dict[str, Any], storage: FakeStorage, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """第四问：落盘的和返回的是不是一回事。"""
    rows = storage.read_all()
    checks: list[tuple[str, bool]] = []
    if result.get("ok") is True and result.get("state") not in (None, "invalid"):
        returned = [normalize(r) for r in result.get("records", [])]
        stored = [normalize(r) for r in rows]
        checks.append(("stored_matches_returned", stored == returned))
        checks.append(
            (
                "stored_state_matches",
                all(
                    row["state"] == rec.get("state")
                    for row, rec in zip(rows, result.get("records", []))
                ),
            )
        )
    trace_ids = {event["trace_id"] for event in events}
    checks.append(("single_trace_id", len(trace_ids) == 1))
    checks.append(
        (
            "trace_id_on_rows",
            all(row["trace_id"] == result.get("trace_id") for row in rows),
        )
    )
    return [{"name": name, "pass": passed} for name, passed in checks]


def stub_extractor_payload(case: dict[str, Any]) -> str:
    records = [normalize(r) for r in case["expected_records"]]
    return json.dumps({"records": records}, ensure_ascii=False)


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    if run_mode == "stub":
        # 剧本要跟着 Case 的预期走：期望被上游拦下的，router 就该判 reject
        router_intent = "reject" if case.get("expected_stage") == "router" else "record"
        router_llm = StubLLM(
            raw_text=json.dumps({"intent": router_intent, "confidence": 0.9})
        )
        extractor_llm = StubLLM(raw_text=stub_extractor_payload(case))
    else:
        if live_llm is None:
            raise RuntimeError("live LLM is not initialized")
        router_llm = extractor_llm = live_llm

    storage = FakeStorage()
    tracer = Tracer(TRACE_PATH)
    actual = handle_message(case["input"], router_llm, extractor_llm, storage, tracer)

    contract = assert_contract(case, actual)
    target = assert_target(case, actual) if actual.get("ok") else []
    protection = assert_protection(case, actual, storage)
    consistency = assert_consistency(actual, storage, tracer.events)
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
        "category": case["category"],
        "risk": case.get("risk", "normal"),
        "views": case["views"],
        "expected_state": case["expected_state"],
        "actual_state": (
            actual.get("state")
            or actual.get("error_code")
            or ("router_rejected" if actual.get("stage") == "router" else None)
        ),
        "expected_write_count": case["expected_write_count"],
        "actual_write_count": len(storage.read_all()),
        "verdict": verdict,
        "pass": verdict == "PASS",
        "failed_checks": failed,
        "assertion_total": len(all_checks),
        "assertion_passed": sum(1 for check in all_checks if check["pass"]),
        "stage": actual.get("stage"),
        "trace_id": actual.get("trace_id"),
    }


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(result["verdict"] for result in results)
    evaluated = [r for r in results if r["verdict"] in {"PASS", "FAIL"}]
    confusion = {expected: Counter() for expected in (*STATES, "router_rejected")}
    for result in evaluated:
        if result["expected_state"] in confusion:
            confusion[result["expected_state"]][result["actual_state"]] += 1
    assertion_total = sum(r["assertion_total"] for r in evaluated)
    assertion_passed = sum(r["assertion_passed"] for r in evaluated)
    return {
        "total": len(results),
        "verdicts": {s: verdicts.get(s, 0) for s in ("PASS", "FAIL", "REVIEW", "ERROR")},
        "case_pass_rate": round(safe_div(verdicts.get("PASS", 0), len(evaluated)), 4),
        "assertion_pass_rate": round(safe_div(assertion_passed, assertion_total), 4),
        "assertion_total": assertion_total,
        "assertion_passed": assertion_passed,
        # 两条硬门禁
        "invalid_write_violations": sum(
            1
            for r in evaluated
            if r["expected_state"] in ("invalid", "router_rejected")
            and r["actual_write_count"] > 0
        ),
        "write_count_violations": sum(
            1 for r in evaluated if r["actual_write_count"] != r["expected_write_count"]
        ),
        "state_confusion": {k: dict(v) for k, v in confusion.items()},
    }


def render_report(
    metadata: dict[str, Any], metrics_by_run: list[dict[str, Any]], results: list[dict[str, Any]]
) -> str:
    lines = [
        "# Extract Eval Report",
        "",
        f"- generated_at: {metadata['generated_at']}",
        f"- view: {metadata['view']}",
        f"- run_mode: {metadata['run_mode']}",
        f"- model: {metadata['model']}",
        f"- git_commit: {metadata['git_commit']}",
        f"- router_prompt_hash: {metadata['router_prompt_hash']}",
        f"- extract_prompt_hash: {metadata['extract_prompt_hash']}",
        f"- dataset_hash: {metadata['dataset_hash']}",
        f"- temperature: {metadata['temperature']}",
        f"- max_tokens: {metadata['max_tokens']}",
        f"- runs: {len(metrics_by_run)}",
        "",
        "> Stub 结果只验证编排与契约，不代表抽取质量。"
        if metadata["run_mode"] == "stub"
        else "> Live 结果代表该模型、该 Prompt、该数据集快照下的抽取行为。",
        "",
    ]
    for index, metrics in enumerate(metrics_by_run, start=1):
        lines.extend(
            [
                f"## Run {index}",
                "",
                "### 结果口径",
                "",
                f"- 【核心结果】Case 级 {metrics['case_pass_rate']:.4f}"
                f"（PASS {metrics['verdicts']['PASS']} / FAIL {metrics['verdicts']['FAIL']}）",
                f"- 【断言明细】断言级 {metrics['assertion_pass_rate']:.4f}"
                f"（{metrics['assertion_passed']} / {metrics['assertion_total']}）",
                f"- 【待复核】REVIEW {metrics['verdicts']['REVIEW']}",
                f"- 【执行异常】ERROR {metrics['verdicts']['ERROR']}（不计入通过率）",
                "",
                "### 硬门禁",
                "",
                f"- invalid 写入违规：{metrics['invalid_write_violations']}（必须为 0）",
                f"- 写入条数错误：{metrics['write_count_violations']}（必须为 0）",
                "",
                "### 三态混淆矩阵",
                "",
                "```json",
                json.dumps(metrics["state_confusion"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Case results",
            "",
            "| run | case_id | expected_state | actual | 写入 期望/实际 | verdict | 失败断言 | trace_id |",
            "|---:|---|---|---|---|:---:|---|---|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result['run_index']} | {result['case_id']} | {result['expected_state']} | "
            f"{result['actual_state']} | {result['expected_write_count']}/{result['actual_write_count']} | "
            f"{result['verdict']} | {', '.join(result['failed_checks']) or '-'} | {result['trace_id']} |"
        )
    badcases = [r for r in results if r["verdict"] != "PASS"]
    lines.extend(["", "## Badcases", ""])
    if not badcases:
        lines.append("None.")
    else:
        lines.extend(["| run | case_id | input | 失败断言 |", "|---:|---|---|---|"])
        for result in badcases:
            escaped = result["input"].replace("|", "\\|")
            lines.append(
                f"| {result['run_index']} | {result['case_id']} | {escaped} | "
                f"{', '.join(result['failed_checks']) or '-'} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--views", default="discovery", choices=["discovery", "locked", "regression", "all"]
    )
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    cases = load_cases(args.views)
    if not cases:
        raise SystemExit("no cases selected")
    live_llm = LiveLLM() if args.run_mode == "live" else None

    before = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    all_results: list[dict[str, Any]] = []
    metrics_by_run: list[dict[str, Any]] = []
    for run_index in range(1, args.runs + 1):
        run_results = []
        for case in cases:
            result = run_case(case, args.run_mode, live_llm)
            result["run_index"] = run_index
            run_results.append(result)
        all_results.extend(run_results)
        metrics_by_run.append(calculate_metrics(run_results))
    after = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    if before != after:
        raise SystemExit("unexpected business file write detected")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"extract-results-{timestamp}.jsonl"
    report_path = RESULTS_DIR / f"extract-report-{timestamp}.md"
    result_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "view": args.views,
        "run_mode": args.run_mode,
        "model": live_llm.model if live_llm else "stub",
        "git_commit": git_commit(),
        "router_prompt_hash": sha256_file(ROUTER_PROMPT_PATH),
        "extract_prompt_hash": sha256_file(EXTRACT_PROMPT_PATH),
        "dataset_hash": sha256_file(DATASET_PATH),
        "temperature": TEMPERATURE,
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
