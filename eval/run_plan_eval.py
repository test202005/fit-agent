from __future__ import annotations

import argparse
import json
import hashlib
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.action_lib import DIFFICULTIES, MUSCLES, action_names, lookup_action  # noqa: E402
from backend.llm import (  # noqa: E402
    MAX_RETRIES,
    MAX_PLAN_TOKENS,
    TEMPERATURE,
    THINKING_MODE,
    TIMEOUT_SECONDS,
    LiveLLM,
    StubLLM,
)
from backend.plan import (  # noqa: E402
    GENERATOR_PROMPT_NAME,
    PLANNER_PROMPT_NAME,
    TOOL_NAME,
    run_workout_plan,
)
from backend.prompt_registry import load_prompt_asset  # noqa: E402
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
from eval.trace_contract import CONTRACTS, assert_trace_contract  # noqa: E402


DATASET_PATH = ROOT / "eval" / "datasets" / "plan-dataset.jsonl"
CONTRACT = "workout_plan"


def build_trace_id(case_id: str) -> str:
    """每个 trial 必须唯一，否则多轮报告无法精确回溯单次执行。"""
    return f"p-{case_id}-{uuid.uuid4()}"


def load_cases(view: str) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for case in cases:
        for field in ("input", "expected", "category", "source"):
            if field not in case:
                raise ValueError(f"{case['case_id']} is missing required field {field!r}")
        expected = case["expected"]
        if set(expected) != {"muscle", "level", "difficulty", "duration_min"}:
            raise ValueError(f"{case['case_id']} has invalid expected fields")
        if expected["muscle"] not in MUSCLES or expected["difficulty"] not in DIFFICULTIES:
            raise ValueError(f"{case['case_id']} uses an unknown label")
    if view == "all":
        return cases
    return [case for case in cases if view in case["views"]]


def stub_need(case: dict[str, Any]) -> str:
    """stub 按剧本返回解析结果：陷阱 case 用 stub_need 故意演「理解错」。

    模型的真实解析能力只在 live 下验证；stub 负责让断言本身可被证伪。
    """
    need = case.get("stub_need") or case["expected"]
    return json.dumps(need, ensure_ascii=False)


def stub_workout(case: dict[str, Any]) -> str:
    """stub 生成按「动作库真返回了什么」铺，逐条引用，不编造。"""
    from backend.action_lib import query_action_lib

    need = case.get("stub_need") or case["expected"]
    observation = query_action_lib(need["muscle"], need["difficulty"])
    names = action_names(observation)
    duration = need["duration_min"]
    sets, reps = {"简单": (3, 12), "中等": (3, 10), "中高": (4, 8)}[need["difficulty"]]
    plan = [
        {"name": name, "sets": sets, "reps": reps, "rest_sec": 60}
        for name in names
    ]
    note = "缺少计时依据，无法估计计划耗时，无法保证满足目标时长。"
    if not plan:
        note = "没有匹配动作，未生成可执行计划。" + note
    return json.dumps({"target_duration_min": duration, "estimated_duration_min": None,
                       "plan": plan, "note": note}, ensure_ascii=False)


def known_names() -> set[str]:
    from backend.action_lib import ACTION_LIB

    return {action["name"] for action in ACTION_LIB}


def assert_blackbox(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """黑盒断言：只看最终计划。这是 iter-4 之前评测的天花板。

    「该有动作」和「该为空」都由 case 的期望给死：黑盒自己没有能力区分
    「查不到所以为空」和「生成失败所以为空」，这正是它看不见的那一层。
    """
    workout = result["workout"]
    plan = workout["plan"]
    target = case["expected"]["duration_min"]
    names = known_names()
    expect_empty = bool(case.get("expect_empty_plan"))
    return [
        {"name": "plan_presence_matches_expectation",
         "pass": (not plan) if expect_empty else bool(plan)},
        {"name": "all_actions_in_lib", "pass": all(item["name"] in names for item in plan)},
        {"name": "target_duration_preserved", "pass": workout["target_duration_min"] == target},
        {"name": "duration_estimate_unknown", "pass": workout["estimated_duration_min"] is None},
        {"name": "duration_limitation_present", "pass": bool(workout["note"].strip())},
        {"name": "item_fields_complete",
         "pass": all(set(item) == {"name", "sets", "reps", "rest_sec"} for item in plan)},
        {"name": "plan_actions_unique",
         "pass": len({item["name"] for item in plan}) == len(plan)},
        {"name": "plan_muscle_matches_intent",
         "pass": all((lookup_action(item["name"]) or {}).get("muscle_target")
                     == case["expected"]["muscle"] for item in plan)},
    ]


def assert_whitebox(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """白盒断言：看中间层。第一条就是工具入参——它才是「理解对不对」的落点。"""
    expected = case["expected"]
    arguments = result["tool_call"]["arguments"]
    observation = result["observation"]
    plan = result["workout"]["plan"]
    returned = set(action_names(observation))
    return [
        {"name": "tool_args_match_intent", "pass": arguments == {
            "muscle": expected["muscle"], "difficulty": expected["difficulty"]}},
        {"name": "need_difficulty_matches_level", "pass": _difficulty_ok(result["need"])},
        {"name": "plan_actions_from_observation",
         "pass": all(item["name"] in returned for item in plan)},
        {"name": "empty_observation_yields_empty_plan",
         "pass": observation["count"] > 0 or not plan},
        # 计划条目只能来自观察：一条观察最多支撑一个计划条目，不复制动作凑数。
        # 工具给了 1 条、计划排出 4 条时，多出来的 3 条是生成器自己编的。
        {"name": "plan_size_within_observation",
         "pass": len(plan) <= observation["count"]},
    ]


def _difficulty_ok(need: dict[str, Any]) -> bool:
    from backend.action_lib import LEVEL_DIFFICULTY

    return need["difficulty"] in LEVEL_DIFFICULTY.get(need["level"], ())


def collect_events(trace_id: str) -> list[dict[str, Any]]:
    """契约校验要读磁盘上的原始事件，与 Runner 内存里的是同一份。"""
    if not TRACE_PATH.is_file():
        return []
    events = []
    for line in TRACE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if record["trace_id"] == trace_id:
                events.append(record)
    return events


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    tracer = Tracer(TRACE_PATH)
    trace_id = build_trace_id(case["case_id"])
    llm = (
        StubLLM(raw_texts=[stub_need(case), stub_workout(case)])
        if run_mode == "stub"
        else live_llm
    )
    if llm is None:
        raise RuntimeError("live LLM is not initialized")

    result = run_workout_plan(case["input"], llm, tracer, trace_id)
    contract = assert_trace_contract(tracer.events, CONTRACT, trace_id)

    blackbox: list[dict[str, Any]] = []
    whitebox: list[dict[str, Any]] = []
    if result.get("ok"):
        blackbox = assert_blackbox(case, result)
        whitebox = assert_whitebox(case, result)

    all_checks = contract["checks"] + blackbox + whitebox
    failed = [c["name"] for c in all_checks if not c["pass"]]
    blackbox_failed = [c["name"] for c in blackbox if not c["pass"]]
    whitebox_failed = [c["name"] for c in whitebox if not c["pass"]]

    if tracer.write_failed:
        verdict = "ERROR"
    elif not result.get("ok") and result.get("error_code") in {"llm_timeout", "llm_api_error"}:
        verdict = "ERROR"
    elif case.get("tier") == "observation":
        verdict = "REVIEW"
    else:
        verdict = "PASS" if not failed else "FAIL"

    injected = run_mode == "stub" and "stub_need" in case
    expected_failures = {
        "pl-201": {"tool_args_match_intent", "plan_muscle_matches_intent"},
        "pl-202": {"tool_args_match_intent", "need_difficulty_matches_level"},
    }.get(case["case_id"], set())
    detector_pass = injected and verdict == "FAIL" and set(failed) == expected_failures
    locations = {
        "tool_args_match_intent": "tool/tool_call",
        "need_difficulty_matches_level": "planner/parse_result",
        "single_trace_id": "trace",
        "trace_contract_sequence": contract["first_missing_step"] or "trace",
        "trace_contract_fields": "trace",
    }
    failures = [{"check": name, "step": locations.get(name, "generator/parse_result")}
                for name in failed]
    tracer.emit(trace_id, "evaluation", {
        "verdict": verdict, "failures": failures,
        "suite": "fault_injection" if injected else "regression",
        "detector_pass": detector_pass if injected else None,
    }, node="eval")
    if tracer.write_failed:
        verdict = "ERROR"
        detector_pass = False

    return {
        "suite": "fault_injection" if injected else "regression",
        "detector_pass": detector_pass if injected else None,
        "failures": failures,
        "case_id": case["case_id"], "input": case["input"], "category": case["category"],
        "risk": case.get("risk", "normal"), "views": case["views"],
        "expected_need": case["expected"],
        "actual_need": result.get("need"),
        "tool_arguments": result.get("tool_call", {}).get("arguments"),
        "observation_count": (result.get("observation") or {}).get("count"),
        "observation_reason": (result.get("observation") or {}).get("reason"),
        "plan_names": [item["name"] for item in (result.get("workout") or {}).get("plan", [])],
        "target_duration_min": (result.get("workout") or {}).get("target_duration_min"),
        "estimated_duration_min": (result.get("workout") or {}).get("estimated_duration_min"),
        "note": (result.get("workout") or {}).get("note"),
        "duration_semantic_review": "PENDING" if result.get("ok") else "NOT_APPLICABLE",
        "first_missing_step": contract["first_missing_step"],
        "missing_fields": contract["missing_fields"],
        "invalid_fields": contract["invalid_fields"],
        "verdict": verdict, "pass": verdict == "PASS",
        "failed_checks": failed,
        "blackbox_checks": len(blackbox), "blackbox_passed":
            sum(1 for c in blackbox if c["pass"]),
        "whitebox_checks": len(whitebox), "whitebox_passed":
            sum(1 for c in whitebox if c["pass"]),
        "assertion_total": len(all_checks),
        "assertion_passed": sum(1 for c in all_checks if c["pass"]),
        "trace_id": result.get("trace_id"),
        "usages": collect_usages(tracer.events),
    }


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    injections = [r for r in results if r.get("suite") == "fault_injection"]
    results = [r for r in results if r.get("suite") != "fault_injection"]
    verdicts = Counter(r["verdict"] for r in results)
    ev = [r for r in results if r["verdict"] in {"PASS", "FAIL"}]
    total = sum(r["assertion_total"] for r in ev)
    passed = sum(r["assertion_passed"] for r in ev)
    # 黑盒全绿但白盒挂 = 深层错误的直接计数，本迭代核心指标
    blind_spots = [
        r for r in ev if r["blackbox_passed"] == r["blackbox_checks"] and r["whitebox_passed"] < r["whitebox_checks"]
    ]
    return {
        "fault_injection_total": len(injections),
        "fault_injection_passed": sum(r["detector_pass"] for r in injections),
        "total": len(results),
        "verdicts": {s: verdicts.get(s, 0) for s in ("PASS", "FAIL", "REVIEW", "ERROR")},
        "case_pass_rate": round(safe_div(verdicts.get("PASS", 0), len(ev)), 4),
        "assertion_pass_rate": round(safe_div(passed, total), 4),
        "assertion_total": total, "assertion_passed": passed,
        "blackbox_blind_spots": len(blind_spots),
        "blind_spot_cases": [r["case_id"] for r in blind_spots],
        "tool_arg_mismatches": sum(1 for r in ev if "tool_args_match_intent" in r["failed_checks"]),
        "library_violations": sum(
            1 for r in ev if "plan_actions_from_observation" in r["failed_checks"]
        ),
        "duration_violations": sum(1 for r in ev if any(c in r["failed_checks"] for c in
            ("target_duration_preserved", "duration_estimate_unknown", "duration_limitation_present"))),
    }


def render_report(metadata, metrics_by_run, results) -> str:
    planner = load_prompt_asset(PLANNER_PROMPT_NAME)
    generator = load_prompt_asset(GENERATOR_PROMPT_NAME)
    lines = [
        "# Plan Generation Eval Report", "",
        f"- generated_at: {metadata['generated_at']}",
        f"- view: {metadata['view']}", f"- run_mode: {metadata['run_mode']}",
        f"- model: {metadata['model']}", f"- git_commit: {metadata['git_commit']}",
        f"- working_tree_dirty: {metadata['working_tree_dirty']}",
        f"- source_hash: {metadata['source_hash']}",
        f"- source_snapshot: {metadata['source_snapshot']}",
        f"- planner_prompt_version: {planner.version}",
        f"- planner_prompt_hash: {planner.prompt_hash}",
        f"- generator_prompt_version: {generator.version}",
        f"- generator_prompt_hash: {generator.prompt_hash}",
        f"- dataset_hash: {metadata['dataset_hash']}",
        f"- trace_contract: {CONTRACT}（{len(CONTRACTS[CONTRACT])} 步）",
        f"- temperature: {metadata['temperature']}",
        f"- runs: {metadata['runs']}", "",
        "> 黑盒 = 只看最终计划；白盒 = 看中间层（需求解析 + 工具入参 + 观察回灌）。",
        "> stub 会按剧本演「理解错」，用来证明白盒断言在该红的时候红了。", "",
    ]
    for entry in metrics_by_run:
        m = entry["metrics"]
        blind = "、".join(m["blind_spot_cases"]) or "-"
        lines.extend([
            f"## {entry['model']} · Run {entry['run_index']}", "", "### 结果口径", "",
            f"- 【核心结果】Case 级 {m['case_pass_rate']:.4f}"
            f"（PASS {m['verdicts']['PASS']} / FAIL {m['verdicts']['FAIL']}）",
            f"- 【断言明细】断言级 {m['assertion_pass_rate']:.4f}"
            f"（{m['assertion_passed']} / {m['assertion_total']}）",
            f"- 【待复核】REVIEW {m['verdicts']['REVIEW']}",
            f"- 【检测器自测】{m['fault_injection_passed']} / {m['fault_injection_total']}（独立于回归通过率）",
            f"- 【执行异常】ERROR {m['verdicts']['ERROR']}（不计入通过率）", "",
            "### 当前断言集的检出差异（不代表黑盒能力上限）", "",
            f"- 黑盒全绿 / 白盒抓出问题：{m['blackbox_blind_spots']} 条 -- {blind}",
            f"- 工具入参与意图不符：{m['tool_arg_mismatches']}",
            f"- 计划用了库外动作：{m['library_violations']}",
            f"- 时长结构检查失败：{m['duration_violations']}",
            "- 时长说明语义需人工复核；结构 PASS 不代表已满足目标时长。", "",
        ])
    lines.extend(
        render_stability(
            aggregate_stability([r for r in results if r.get("suite") != "fault_injection"]),
            aggregate_usage([r for r in results if r.get("suite") != "fault_injection"]), metadata["runs"]
        )
    )
    lines.extend(["", "## Case results", "",
        "| model | run | case_id | 测试集/检测器结果 | 期望肌群/难度 | 工具入参 | 观察条数 | 计划动作 | 黑盒 | 白盒 | verdict | 失败断言 | trace_id |",
        "|---|---:|---|---|---|---|---|---|:---:|:---:|---|---|---|"])
    for r in results:
        exp = r["expected_need"]
        args = r["tool_arguments"] or {}
        lines.append(
            f"| {r['model']} | {r['run_index']} | {r['case_id']} | "
            f"{r['suite']}{('/PASS' if r['detector_pass'] else '/FAIL') if r['suite'] == 'fault_injection' else ''} | "
            f"{exp['muscle']}/{exp['difficulty']} | "
            f"{args.get('muscle', '-')}/{args.get('difficulty', '-')} | "
            f"{r['observation_count'] if r['observation_count'] is not None else '-'} | "
            f"{', '.join(r['plan_names']) or '无'} | "
            f"{r['blackbox_passed']}/{r['blackbox_checks']} | "
            f"{r['whitebox_passed']}/{r['whitebox_checks']} | "
            f"{r['verdict']} | {', '.join(r['failed_checks']) or '-'} | {r['trace_id']} |"
        )
    bad = [r for r in results if r["verdict"] != "PASS"
           and r.get("suite") != "fault_injection"]
    lines.extend(["", "## Badcases", ""])
    if not bad:
        lines.append("None.")
    else:
        lines.extend(["| model | run | case_id | input | 工具入参 | 失败断言 | trace_id |",
                      "|---|---:|---|---|---|---|---|"])
        for r in bad:
            args = r["tool_arguments"] or {}
            lines.append(
                f"| {r['model']} | {r['run_index']} | {r['case_id']} | {r['input']} | "
                f"{args.get('muscle', '-')}/{args.get('difficulty', '-')} | "
                f"{', '.join(r['failed_checks']) or '-'} | {r['trace_id']} |"
            )
    return "\n".join(lines)


def source_evidence(root: Path) -> dict[str, Any]:
    paths = sorted({p for folder in ("backend", "eval", "tests")
                    for p in (root / folder).rglob("*")
                    if p.is_file() and p.suffix in {".py", ".txt", ".json", ".jsonl"}
                    and not {"logs", "results", "__pycache__"} & set(p.relative_to(root).parts)})
    files = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in paths}
    for name in ("requirements.txt", "pyproject.toml", "uv.lock"):
        if (root / name).is_file():
            files[name] = (root / name).read_text(encoding="utf-8")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, check=True)
    return {"working_tree_dirty": bool(status.stdout), "source_hash": "sha256:" + digest,
            "files": files}


def evaluation_passes(results) -> bool:
    return all(r["detector_pass"] if r.get("suite") == "fault_injection"
               else r["verdict"] in {"PASS", "REVIEW"} for r in results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--views", default="discovery",
                        choices=["discovery", "regression", "all"])
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    parser.add_argument("--max-tokens", type=int, default=MAX_PLAN_TOKENS)
    add_matrix_args(parser)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    cases = load_cases(args.views)
    if not cases:
        raise SystemExit("no cases selected")
    clients = build_clients(args.run_mode, args.models, args.temperature)
    # 计划生成输出比分类长，显式抬上限，不共用分类的 100
    for llm in clients:
        if llm is not None:
            llm.max_tokens = args.max_tokens

    evidence = source_evidence(ROOT)
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
    result_path = RESULTS_DIR / f"plan-results-{ts}.jsonl"
    report_path = RESULTS_DIR / f"plan-report-{ts}.md"
    source_path = RESULTS_DIR / f"plan-source-{ts}.json"
    source_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    result_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_results) + "\n", encoding="utf-8")
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "view": args.views,
        "run_mode": args.run_mode,
        "working_tree_dirty": evidence["working_tree_dirty"],
        "source_hash": evidence["source_hash"],
        "source_snapshot": str(source_path.relative_to(ROOT)),
        "model": ", ".join(client_model(llm) for llm in clients),
        "runs": args.runs,
        "git_commit": git_commit(), "dataset_hash": sha256_file(DATASET_PATH),
        "temperature": effective_temperature(args), "max_tokens": args.max_tokens,
        "thinking": THINKING_MODE, "timeout_seconds": TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES, "tool": TOOL_NAME,
    }
    report_path.write_text(render_report(metadata, metrics_by_run, all_results), encoding="utf-8")
    print(json.dumps({
        "result_path": str(result_path.relative_to(ROOT)),
        "report_path": str(report_path.relative_to(ROOT)),
        "metrics": metrics_by_run,
    }, ensure_ascii=False))
    return 0 if evaluation_passes(all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
