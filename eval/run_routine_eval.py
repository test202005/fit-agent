"""训练安排调整顺序评测：分层判定，首次参数与最终状态分开记账。

端到端只看最终状态，「首次被拒、重试成功」会被记成通过。这里把每层单独判：
L1 意图、L2 首次协议、L2' 首次顺序、L3 执行、L4 最终状态、L5 回复（只记录）。
门禁（L1、L4、Trace 契约）全过但 L2 / L2' 失败记 REVIEW，不算干净通过。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
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
    MAX_TOOL_TOKENS,
    THINKING_MODE,
    TIMEOUT_SECONDS,
    StubLLM,
    ToolCall,
    ToolCallResult,
)
from backend.prompt_registry import load_prompt_asset  # noqa: E402
from backend.routine import FakeRoutineStore  # noqa: E402
from backend.routine_agent import MAX_ROUNDS, run_routine_agent  # noqa: E402
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
from eval.trace_contract import assert_trace_contract  # noqa: E402


DATASET_PATH = ROOT / "eval" / "datasets" / "routine-dataset.jsonl"
FIXTURES_PATH = ROOT / "eval" / "datasets" / "routine-fixtures.json"
USER_ID = "eval-user"
NOW = "2026-09-25T20:00:00+08:00"
VIEWS = ["discovery", "boundary", "pressure"]
WRITE_EXPECTATIONS = {"yes", "no", "optional"}
POSITIONS = {"first", "last", "before", "after"}


def load_fixtures() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def load_cases(view: str) -> list[dict[str, Any]]:
    fixtures = load_fixtures()
    cases = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        for field in ("case_id", "views", "category", "input", "fixture", "op", "expect_write", "source"):
            if field not in case:
                raise ValueError(f"{case.get('case_id')} is missing required field {field!r}")
        if case["fixture"] not in fixtures:
            raise ValueError(f"{case['case_id']} references unknown fixture {case['fixture']!r}")
        if case["expect_write"] not in WRITE_EXPECTATIONS:
            raise ValueError(f"{case['case_id']} has invalid expect_write")
        op = case["op"]
        if (op is None) != (case["expect_write"] == "no"):
            raise ValueError(f"{case['case_id']}: op must be null exactly when expect_write is 'no'")
        if op is not None:
            ids = {u["unit_id"] for u in fixtures[case["fixture"]]}
            if op["position"] not in POSITIONS or op["target"] not in ids:
                raise ValueError(f"{case['case_id']} has invalid op")
            if (op["position"] in {"before", "after"}) != (op.get("anchor") in ids):
                raise ValueError(f"{case['case_id']} has invalid anchor")
    if view == "all":
        return cases
    return [case for case in cases if view in case["views"]]


# ---------- 期望顺序（独立于被测实现，只在评测侧计算）----------


def apply_move(order: list[str], op: dict[str, Any]) -> list[str]:
    """PRD 2.2 位置规则：取出目标，其余相对顺序不变，按规则插回全量顺序。"""
    rest = [u for u in order if u != op["target"]]
    position = op["position"]
    if position == "first":
        index = 0
    elif position == "last":
        index = len(rest)
    else:
        index = rest.index(op["anchor"]) + (1 if position == "after" else 0)
    return rest[:index] + [op["target"]] + rest[index:]


def order_matches(before: list[dict[str, Any]], order: Any, op: dict[str, Any] | None) -> bool:
    """顺序是否正确。paused 单元对用户不可见，目标与 paused 的相对位置不唯一，因此判三件事：
    是全量排列；active 视图等于期望；非目标单元的全量相对顺序不变。"""
    ids = [u["unit_id"] for u in before]
    if not isinstance(order, list) or sorted(order) != sorted(ids) or len(set(order)) != len(order):
        return False
    if op is None:
        return order == ids
    active = {u["unit_id"] for u in before if u["status"] == "active"}
    expected = apply_move(ids, op)
    if [u for u in order if u in active] != [u for u in expected if u in active]:
        return False
    target = op["target"]
    return [u for u in order if u != target] == [u for u in ids if u != target]


# ---------- 分层判定 ----------


def judge(case: dict[str, Any], before: list[dict[str, Any]], result: dict[str, Any],
          contract: dict[str, Any]) -> dict[str, Any]:
    op = case["op"]
    expect = case["expect_write"]
    writes = [a for a in result.get("attempts", []) if a["tool"] == "set_routine_order"]
    first = writes[0] if writes else None
    final_units = result.get("final_units", [])
    final_order = [u["unit_id"] for u in final_units]
    statuses_kept = {u["unit_id"]: u["status"] for u in final_units} == {
        u["unit_id"]: u["status"] for u in before
    }

    gates: dict[str, bool] = {}
    if expect == "yes":
        gates["L1_write_called"] = bool(writes)
    elif expect == "no":
        gates["L1_no_write"] = result.get("writes", 0) == 0
    gates["L3_within_rounds"] = result.get("error_code") != "max_rounds_exceeded"
    gates["L4_final_state"] = statuses_kept and order_matches(before, final_order, op)
    for check in contract["checks"]:
        gates[f"trace_{check['name']}"] = check["pass"]

    observations: dict[str, bool] = {}
    if first is not None and expect != "no":
        observations["L2_first_protocol_ok"] = first["ok"]
        observations["L2p_first_order_correct"] = first["ok"] and order_matches(
            before, first["args"].get("order"), op
        )

    missing_paused_share = None
    if first is not None and first["error_code"] == "incomplete_order":
        submitted = first["args"].get("order") or []
        missing = [u for u in before if u["unit_id"] not in submitted]
        if missing:
            paused = sum(1 for u in missing if u["status"] == "paused")
            missing_paused_share = round(paused / len(missing), 4)

    return {
        "gates": gates,
        "observations": observations,
        "write_attempts": len(writes),
        "retried": len(writes) > 1,
        "successful_writes": result.get("writes", 0),
        "first_error_code": first["error_code"] if first else None,
        "missing_paused_share": missing_paused_share,
        "final_order": final_order,
    }


def verdict_of(result: dict[str, Any], judged: dict[str, Any], trace_failed: bool) -> str:
    if trace_failed or result.get("error_code") in {"llm_timeout", "llm_api_error"}:
        return "ERROR"
    if not all(judged["gates"].values()):
        return "FAIL"
    if not all(judged["observations"].values()):
        return "REVIEW"
    return "PASS"


# ---------- Stub 剧本：按正确行为应答，只验证编排与断言 ----------


def stub_rounds(case: dict[str, Any], units: list[dict[str, Any]]) -> list[ToolCallResult]:
    if case["category"] == "unrelated":
        return []
    rounds = [ToolCallResult(tool_calls=[ToolCall(
        name="get_routine", arguments={"include_paused": True}, id="stub-1")])]
    if case["op"] is not None:
        order = apply_move([u["unit_id"] for u in units], case["op"])
        rounds.append(ToolCallResult(tool_calls=[ToolCall(
            name="set_routine_order", arguments={"order": order}, id="stub-2")]))
    return rounds


def build_trace_id(case_id: str) -> str:
    return f"t-{case_id}-{uuid.uuid4()}"


def run_case(case: dict[str, Any], run_mode: str, live_llm: Any,
             fixtures: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    store = FakeRoutineStore()
    store.seed(USER_ID, fixtures[case["fixture"]])
    before = store.read(USER_ID)
    tracer = Tracer(TRACE_PATH)
    if run_mode == "stub":
        llm = StubLLM(tool_rounds=stub_rounds(case, before), text="已完成。")
    else:
        llm = live_llm
    if llm is None:
        raise RuntimeError("live LLM is not initialized")

    trace_id = build_trace_id(case["case_id"])
    result = run_routine_agent(
        case["input"], llm, store, tracer, FrozenClock(NOW), trace_id, USER_ID,
        context={"case_id": case["case_id"], "fixture": case["fixture"], "arm": "v1-default"},
    )
    contract = assert_trace_contract(tracer.events, "routine_reorder", trace_id)
    judged = judge(case, before, result, contract)
    verdict = verdict_of(result, judged, tracer.write_failed)
    failed = [name for name, ok in {**judged["gates"], **judged["observations"]}.items() if not ok]
    tracer.emit(
        trace_id, "evaluation",
        {"verdict": verdict, "failures": [{"step": name.split("_")[0], "check": name} for name in failed]},
        node="eval",
    )
    return {
        "case_id": case["case_id"], "input": case["input"], "category": case["category"],
        "views": case["views"], "fixture": case["fixture"], "expect_write": case["expect_write"],
        "verdict": verdict, "pass": verdict == "PASS", "failed_checks": failed,
        "rounds": result.get("rounds"), "error_code": result.get("error_code"),
        "reply": result.get("text", ""),
        **judged,
        "trace_id": trace_id,
        "usages": collect_usages(tracer.events),
    }


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(r["verdict"] for r in results)
    writes = [r for r in results if r["expect_write"] == "yes" and r["verdict"] != "ERROR"]
    first_ok = sum(1 for r in writes if r["observations"].get("L2_first_protocol_ok"))
    first_correct = sum(1 for r in writes if r["observations"].get("L2p_first_order_correct"))
    final_ok = sum(1 for r in writes if r["gates"]["L4_final_state"])
    masked = sum(
        1 for r in writes
        if r["write_attempts"] and r["gates"]["L4_final_state"]
        and not r["observations"]["L2_first_protocol_ok"]
    )
    shares = [r["missing_paused_share"] for r in writes if r["missing_paused_share"] is not None]
    error_codes = Counter(r["first_error_code"] for r in writes if r["first_error_code"])
    return {
        "total": len(results),
        "verdicts": {s: verdicts.get(s, 0) for s in ("PASS", "FAIL", "REVIEW", "ERROR")},
        "write_cases": len(writes),
        "first_protocol_rate": round(safe_div(first_ok, len(writes)), 4),
        "first_order_rate": round(safe_div(first_correct, len(writes)), 4),
        "final_state_rate": round(safe_div(final_ok, len(writes)), 4),
        "masked_failures": masked,
        "avg_write_attempts": round(safe_div(sum(r["write_attempts"] for r in writes), len(writes)), 2),
        "first_error_codes": dict(error_codes),
        "missing_paused_share": round(sum(shares) / len(shares), 4) if shares else None,
    }


def render_report(metadata: dict[str, Any], metrics_by_run: list[dict[str, Any]],
                  results: list[dict[str, Any]]) -> str:
    lines = [
        "# Routine Reorder Eval Report", "",
        *[f"- {key}: {value}" for key, value in metadata.items()], "",
        "> stub 按正确剧本应答，只验证编排、断言与 Trace；首次参数问题只在 live 下才可能出现。", "",
    ]
    for entry in metrics_by_run:
        m = entry["metrics"]
        lines.extend([
            f"## {entry['model']} · Run {entry['run_index']}", "",
            f"- Case：PASS {m['verdicts']['PASS']} / REVIEW {m['verdicts']['REVIEW']} / "
            f"FAIL {m['verdicts']['FAIL']} / ERROR {m['verdicts']['ERROR']}",
            f"- 需写入 Case：{m['write_cases']}",
            f"- 首次协议通过率：{m['first_protocol_rate']:.4f}",
            f"- 首次顺序正确率：{m['first_order_rate']:.4f}",
            f"- 最终状态正确率：{m['final_state_rate']:.4f}",
            f"- 被掩盖失败数（首次被拒、最终正确）：{m['masked_failures']}",
            f"- 平均写工具尝试次数：{m['avg_write_attempts']}",
            f"- 首次被拒错误码：{json.dumps(m['first_error_codes'], ensure_ascii=False)}",
            f"- 首次缺失项中 paused 占比：{m['missing_paused_share']}", "",
        ])
    lines.extend(render_stability(aggregate_stability(results), aggregate_usage(results), metadata["runs"]))
    lines.extend([
        "", "## Case results", "",
        "| model | run | case_id | 期望写入 | 首次协议 | 首次顺序 | 写尝试 | 首次错误码 | 最终状态 | verdict | trace_id |",
        "|---|---:|---|:---:|:---:|:---:|:---:|---|:---:|:---:|---|",
    ])

    def mark(value: bool | None) -> str:
        return "-" if value is None else ("✓" if value else "✗")

    for r in results:
        lines.append(
            f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['expect_write']} | "
            f"{mark(r['observations'].get('L2_first_protocol_ok'))} | "
            f"{mark(r['observations'].get('L2p_first_order_correct'))} | {r['write_attempts']} | "
            f"{r['first_error_code'] or '-'} | {mark(r['gates']['L4_final_state'])} | "
            f"{r['verdict']} | {r['trace_id']} |"
        )
    lines.extend(["", "## 回复原文（L5，人工核对）", "",
                  "| model | run | case_id | input | reply |", "|---|---:|---|---|---|"])
    for r in results:
        reply = (r["reply"] or "").replace("\n", " ").replace("|", "\\|")
        lines.append(f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['input']} | {reply} |")
    lines.extend(["", "查看单条链路：`.venv/bin/python eval/trace_view.py <trace_id>`"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--views", default="discovery", choices=[*VIEWS, "all"])
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    parser.add_argument("--cases", default="", help="逗号分隔的 case_id，只跑这些")
    add_matrix_args(parser)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    fixtures = load_fixtures()
    cases = load_cases(args.views)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        cases = [c for c in cases if c["case_id"] in wanted]
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
                r = run_case(case, args.run_mode, llm, fixtures)
                r["run_index"] = run_index
                r["model"] = model_name
                run_results.append(r)
            all_results.extend(run_results)
            metrics_by_run.append({"model": model_name, "run_index": run_index,
                                   "metrics": calculate_metrics(run_results)})
    if before != snapshot([ROOT / "backend", ROOT / "eval" / "datasets"]):
        raise SystemExit("unexpected business file write detected")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"routine-results-{ts}.jsonl"
    report_path = RESULTS_DIR / f"routine-report-{ts}.md"
    result_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n", encoding="utf-8")
    prompt = load_prompt_asset("routine_agent")
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "view": args.views,
        "cases": args.cases or "all", "run_mode": args.run_mode,
        "model": ", ".join(client_model(llm) for llm in clients), "runs": args.runs,
        "git_commit": git_commit(), "prompt_version": prompt.version, "prompt_hash": prompt.prompt_hash,
        "dataset_hash": sha256_file(DATASET_PATH), "fixtures_hash": sha256_file(FIXTURES_PATH),
        "arm": "v1-default", "temperature": effective_temperature(args),
        "max_rounds": MAX_ROUNDS, "max_tokens": MAX_TOOL_TOKENS, "thinking": THINKING_MODE,
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
