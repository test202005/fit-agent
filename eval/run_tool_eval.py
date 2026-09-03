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

from backend.agent import MAX_TOOL_CALLS  # noqa: E402
from backend.agent import PROMPT_PATH as AGENT_PROMPT_PATH  # noqa: E402
from backend.agent import run_agent  # noqa: E402
from backend.clock import FrozenClock  # noqa: E402
from backend.llm import (  # noqa: E402
    MAX_RETRIES,
    MAX_TOOL_TOKENS,
    TEMPERATURE,
    THINKING_MODE,
    TIMEOUT_SECONDS,
    LiveLLM,
    StubLLM,
    ToolCall,
)
from backend.storage import FakeStorage  # noqa: E402
from backend.tools import TOOL_NAMES  # noqa: E402
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


DATASET_PATH = ROOT / "eval" / "datasets" / "tool-dataset.jsonl"


def load_cases(view: str) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        for field in ("input", "now", "expected_tools", "expected_tool_count", "source"):
            if field not in case:
                raise ValueError(f"{case['case_id']} is missing required field {field!r}")
        for tool in case["expected_tools"]:
            if tool["name"] not in TOOL_NAMES:
                raise ValueError(f"{case['case_id']} references unknown tool {tool['name']!r}")
    if view == "all":
        return cases
    return [case for case in cases if view in case["views"]]


def seed_storage(case: dict[str, Any]) -> FakeStorage:
    storage = FakeStorage()
    for index, item in enumerate(case.get("fixture", []), start=1):
        storage.rows.append(
            {
                "id": f"seed-{index:03d}", "ts": item["ts"],
                "state": item.get("state", "complete"), "trace_id": "t-seed",
                "exercise": item["exercise"], "weight_kg": None, "sets": None,
                "reps": None, "duration_min": None, "distance_km": None,
            }
        )
    return storage


def stub_calls(case: dict[str, Any]) -> list[ToolCall]:
    """stub 按期望剧本调用，验证编排与断言；模型的真实选择能力只在 live 下验证。"""
    return [ToolCall(name=t["name"], arguments=dict(t["args"])) for t in case["expected_tools"]]


def actual_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": step["tool"], "args": {k: v for k, v in step["arguments"].items() if v is not None}}
        for step in result.get("trajectory", [])
    ]


def assert_contract(result: dict[str, Any]) -> list[dict[str, Any]]:
    """第一问：结构与枚举。"""
    checks: list[tuple[str, bool]] = []
    if result.get("ok") is True:
        traj = result.get("trajectory")
        checks.append(("trajectory_is_list", isinstance(traj, list)))
        checks.append(("tool_count_matches_len", result.get("tool_count") == len(traj or [])))
        checks.append(
            ("tools_in_registry", all(s["tool"] in TOOL_NAMES or not s["ok"] for s in traj or []))
        )
        checks.append(("within_call_limit", len(traj or []) <= MAX_TOOL_CALLS))
    else:
        checks.append(("no_trajectory_on_failure", "trajectory" not in result))
    return [{"name": n, "pass": p} for n, p in checks]


def assert_trajectory(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """第二问 + trajectory 断言：调用序列、参数、无多余调用。"""
    expected = case["expected_tools"]
    actual = actual_calls(result)
    exp_names = [t["name"] for t in expected]
    act_names = [c["name"] for c in actual]
    args_match = len(expected) == len(actual) and all(
        e["args"] == a["args"] for e, a in zip(expected, actual)
    )
    # 定位第一个分歧步骤：早期错误会级联，只报最终结果等于没有归因信息
    divergence = None
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            divergence = i + 1
            break
    return [
        {"name": "tool_count_equals", "pass": len(actual) == case["expected_tool_count"]},
        {"name": "tool_sequence_equals", "pass": act_names == exp_names},
        {"name": "tool_args_match", "pass": args_match},
        {"name": "no_extra_tool_calls", "pass": len(actual) <= len(expected)},
        {"name": "all_steps_succeeded", "pass": not result.get("failed_steps")},
    ], divergence


def assert_protection(case: dict[str, Any], storage: FakeStorage) -> list[dict[str, Any]]:
    """第三问：不该写的没写。"""
    seeded = len(case.get("fixture", []))
    writes = len(storage.read_all()) - seeded
    expected_writes = sum(1 for t in case["expected_tools"] if t["name"] == "create_record")
    checks = [{"name": "write_count_exact", "pass": writes == expected_writes}]
    if case["expected_tool_count"] == 0:
        checks.append({"name": "no_tool_no_write", "pass": writes == 0})
    return checks


def assert_consistency(result: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """第四问：trace 记录的调用与返回一致。"""
    resp = next((e for e in events if e["event"] == "agent_response"), None)
    traced = [c["name"] for c in (resp["payload"]["tool_calls"] if resp else [])]
    actual = [s["tool"] for s in result.get("trajectory", [])]
    return [
        {"name": "single_trace_id", "pass": len({e["trace_id"] for e in events}) == 1},
        {"name": "trace_calls_match", "pass": traced == actual},
    ]


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    storage = seed_storage(case)
    clock = FrozenClock(case["now"])
    tracer = Tracer(TRACE_PATH)
    llm = StubLLM(tool_calls=stub_calls(case)) if run_mode == "stub" else live_llm
    if llm is None:
        raise RuntimeError("live LLM is not initialized")

    result = run_agent(case["input"], llm, storage, tracer, clock, f"t-{case['case_id']}")

    contract = assert_contract(result)
    divergence = None
    if result.get("ok"):
        trajectory_checks, divergence = assert_trajectory(case, result)
    else:
        trajectory_checks = []
    protection = assert_protection(case, storage)
    consistency = assert_consistency(result, tracer.events) if result.get("ok") else []
    all_checks = contract + trajectory_checks + protection + consistency
    failed = [c["name"] for c in all_checks if not c["pass"]]

    if tracer.write_failed:
        verdict = "ERROR"
    elif not result.get("ok") and result.get("error_code") in {"llm_timeout", "llm_api_error"}:
        verdict = "ERROR"
    elif case.get("tier") == "observation":
        verdict = "REVIEW"
    else:
        verdict = "PASS" if not failed else "FAIL"

    return {
        "case_id": case["case_id"], "input": case["input"], "category": case["category"],
        "risk": case.get("risk", "normal"), "views": case["views"],
        "expected_tools": [t["name"] for t in case["expected_tools"]],
        "actual_tools": [s["tool"] for s in result.get("trajectory", [])],
        "expected_tool_count": case["expected_tool_count"],
        "actual_tool_count": result.get("tool_count", 0),
        "first_divergence_step": divergence,
        "verdict": verdict, "pass": verdict == "PASS", "failed_checks": failed,
        "assertion_total": len(all_checks),
        "assertion_passed": sum(1 for c in all_checks if c["pass"]),
        "trace_id": result.get("trace_id"),
        "usages": collect_usages(tracer.events),
    }


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(r["verdict"] for r in results)
    ev = [r for r in results if r["verdict"] in {"PASS", "FAIL"}]
    total = sum(r["assertion_total"] for r in ev)
    passed = sum(r["assertion_passed"] for r in ev)
    # 工具选择混淆矩阵：期望调用的第一个工具 vs 实际第一个（无调用记为 none）
    confusion: dict[str, Counter] = {}
    for r in ev:
        exp = r["expected_tools"][0] if r["expected_tools"] else "none"
        act = r["actual_tools"][0] if r["actual_tools"] else "none"
        confusion.setdefault(exp, Counter())[act] += 1
    return {
        "total": len(results),
        "verdicts": {s: verdicts.get(s, 0) for s in ("PASS", "FAIL", "REVIEW", "ERROR")},
        "case_pass_rate": round(safe_div(verdicts.get("PASS", 0), len(ev)), 4),
        "assertion_pass_rate": round(safe_div(passed, total), 4),
        "assertion_total": total, "assertion_passed": passed,
        "param_hallucinations": sum(
            1 for r in ev if "tool_args_match" in r["failed_checks"]
        ),
        "unwanted_tool_calls": sum(
            1 for r in ev if r["expected_tool_count"] == 0 and r["actual_tool_count"] > 0
        ),
        "write_violations": sum(1 for r in ev if "no_tool_no_write" in r["failed_checks"]),
        "tool_confusion": {k: dict(v) for k, v in confusion.items()},
    }


def render_report(metadata, metrics_by_run, results) -> str:
    lines = [
        "# Tool Use Eval Report", "",
        f"- generated_at: {metadata['generated_at']}",
        f"- view: {metadata['view']}", f"- run_mode: {metadata['run_mode']}",
        f"- model: {metadata['model']}", f"- git_commit: {metadata['git_commit']}",
        f"- agent_prompt_hash: {metadata['agent_prompt_hash']}",
        f"- dataset_hash: {metadata['dataset_hash']}",
        f"- temperature: {metadata['temperature']}",
        f"- max_tool_calls: {MAX_TOOL_CALLS}",
        f"- runs: {metadata['runs']}", "",
        "> stub 只验证编排与断言；模型的工具选择能力只在 live 模式下体现。", "",
    ]
    for entry in metrics_by_run:
        m = entry["metrics"]
        lines.extend([
            f"## {entry['model']} · Run {entry['run_index']}", "", "### 结果口径", "",
            f"- 【核心结果】Case 级 {m['case_pass_rate']:.4f}"
            f"（PASS {m['verdicts']['PASS']} / FAIL {m['verdicts']['FAIL']}）",
            f"- 【断言明细】断言级 {m['assertion_pass_rate']:.4f}"
            f"（{m['assertion_passed']} / {m['assertion_total']}）",
            f"- 【待复核】REVIEW {m['verdicts']['REVIEW']}",
            f"- 【执行异常】ERROR {m['verdicts']['ERROR']}（不计入通过率）", "",
            "### 硬门禁", "",
            f"- 参数幻觉：{m['param_hallucinations']}（必须为 0）",
            f"- 不该调却调了工具：{m['unwanted_tool_calls']}（必须为 0）",
            f"- 不该写却写了：{m['write_violations']}（必须为 0）", "",
            "### 工具选择混淆矩阵（首个工具）", "", "```json",
            json.dumps(m["tool_confusion"], ensure_ascii=False, indent=2), "```", "",
        ])
    lines.extend(
        render_stability(
            aggregate_stability(results), aggregate_usage(results), metadata["runs"]
        )
    )
    lines.extend(["", "## Case results", "",
        "| model | run | case_id | 期望工具 | 实际 | 数量 期望/实际 | 首个分歧 | verdict | 失败断言 | trace_id |",
        "|---|---:|---|---|---|---|:---:|:---:|---|---|"])
    for r in results:
        lines.append(
            f"| {r['model']} | {r['run_index']} | {r['case_id']} | "
            f"{'+'.join(r['expected_tools']) or '无'} | "
            f"{'+'.join(r['actual_tools']) or '无'} | {r['expected_tool_count']}/{r['actual_tool_count']} | "
            f"{r['first_divergence_step'] or '-'} | {r['verdict']} | "
            f"{', '.join(r['failed_checks']) or '-'} | {r['trace_id']} |"
        )
    bad = [r for r in results if r["verdict"] != "PASS"]
    lines.extend(["", "## Badcases", ""])
    if not bad:
        lines.append("None.")
    else:
        lines.extend(["| model | run | case_id | input | 首个分歧步骤 | 失败断言 |",
                      "|---|---:|---|---|:---:|---|"])
        for r in bad:
            lines.append(
                f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['input']} | "
                f"{r['first_divergence_step'] or '-'} | {', '.join(r['failed_checks']) or '-'} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--views", default="discovery",
                        choices=["discovery", "locked", "regression", "all"])
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
                r = run_case(case, args.run_mode, llm)
                r["run_index"] = run_index
                r["model"] = model_name
                run_results.append(r)
            all_results.extend(run_results)
            metrics_by_run.append({
                "model": model_name,
                "run_index": run_index,
                "metrics": calculate_metrics(run_results),
            })
    if before != snapshot([ROOT / "backend", ROOT / "eval" / "datasets"]):
        raise SystemExit("unexpected business file write detected")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"tool-results-{ts}.jsonl"
    report_path = RESULTS_DIR / f"tool-report-{ts}.md"
    result_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n", encoding="utf-8")
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "view": args.views,
        "run_mode": args.run_mode,
        "model": ", ".join(client_model(llm) for llm in clients),
        "runs": args.runs,
        "git_commit": git_commit(), "agent_prompt_hash": sha256_file(AGENT_PROMPT_PATH),
        "dataset_hash": sha256_file(DATASET_PATH), "temperature": effective_temperature(args),
        "max_tokens": MAX_TOOL_TOKENS, "thinking": THINKING_MODE,
        "timeout_seconds": TIMEOUT_SECONDS, "max_retries": MAX_RETRIES,
    }
    report_path.write_text(render_report(metadata, metrics_by_run, all_results), encoding="utf-8")
    print(json.dumps({
        "result_path": str(result_path.relative_to(ROOT)),
        "report_path": str(report_path.relative_to(ROOT)),
        "metrics": metrics_by_run,
    }, ensure_ascii=False))
    return 0 if all(r["verdict"] in {"PASS", "REVIEW"} for r in all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
