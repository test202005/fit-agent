"""V10 训练计划三组对照：v7 / llm_only / v2，同一批需求，按红线与数据断言判定。

判定以 Case 的 truth（真实器械、水平、时长、部位、排除项）为准，不用模型自己的解析结果，
解析错了一样会在红线上暴露。红线与数据违反为 FAIL，指引不满足为 REVIEW。
只支持 live：各引擎的编排逻辑已由 tests/test_plan_v2_unit.py 用 Stub 覆盖。
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

from backend import action_lib_v2 as lib  # noqa: E402
from backend.deps import MAX_PLAN_V2_TOKENS  # noqa: E402
from backend.llm import MAX_PLAN_TOKENS, LiveLLM  # noqa: E402
from backend.plan import run_workout_plan  # noqa: E402
from backend.plan_v2 import match_name, run_plan_llm_only, run_plan_v2  # noqa: E402
from backend.prompt_registry import load_prompt_asset  # noqa: E402
from backend.trace import Tracer  # noqa: E402

from eval.run_intent_eval import RESULTS_DIR, TRACE_PATH, git_commit, load_dotenv, sha256_file  # noqa: E402


DATASET_PATH = ROOT / "eval" / "datasets" / "plan-v2-dataset.jsonl"
ARMS = ("v7", "llm_only", "v2")
ERROR_CODES = {"llm_timeout", "llm_api_error"}
MAJOR_PARTS = {"下肢", "臀", "胸", "背", "肩", "核心", "全身"}


def load_cases(selected: str) -> list[dict[str, Any]]:
    cases = [json.loads(l) for l in DATASET_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    for case in cases:
        if case["expect"] not in {"plan", "refer", "clarify"}:
            raise ValueError(f"{case['case_id']} has invalid expect")
    if selected:
        wanted = {c.strip() for c in selected.split(",")}
        cases = [c for c in cases if c["case_id"] in wanted]
    return cases


# ---------- 统一三种引擎的输出 ----------


def normalize(arm: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """转成 v2 的计划结构；库里没有的动作 id 记为 unknown:<名称>，校验时即为 R1。"""
    if not result.get("ok"):
        return None

    def item(raw: dict[str, Any]) -> dict[str, Any]:
        action_id = raw.get("id") or match_name(raw.get("name"))
        clean = {k: v for k, v in raw.items() if k in ("sets", "reps", "seconds", "rest_sec") and v is not None}
        return {"id": action_id or f"unknown:{raw.get('name')}", **clean}

    if arm == "v7":
        return {"segments": [{"segment": "main", "format": "straight",
                              "items": [item(a) for a in result["workout"]["plan"]]}]}
    segments = []
    for segment in result["segments"]:
        fmt = segment.get("format", "straight")
        normalized = {"segment": segment.get("segment"), "format": fmt,
                      "items": [item(i) for i in segment["items"]]}
        if fmt == "straight":
            for i in normalized["items"]:
                i.setdefault("sets", 1)
        else:
            normalized["rounds"] = segment.get("rounds")
            normalized["rest_between_rounds"] = segment.get("rest_between_rounds", 0)
        segments.append(normalized)
    return {"segments": segments}


def truth_need(case: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    truth = case["truth"]
    available = truth["available"]
    equipment = lib.all_equipment() if available == "gym" else set(available or [])
    need = {"level": truth["level"], "duration_min": truth["duration_min"],
            "parts": truth["parts"], "exclusions": truth["exclusions"]}
    return need, equipment


def guidance(case: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """指引 G1、G2、G5 的可规则检查部分；不满足只计 REVIEW。"""
    actions = lib.load_actions()
    known = lambda i: actions.get(i["id"])  # noqa: E731
    segments = {s["segment"] for s in plan["segments"]}
    misses = []
    if not {"warmup", "main", "cooldown"} <= segments:
        misses.append("G1_three_segments")
    warmup = [known(i) for s in plan["segments"] if s["segment"] == "warmup" for i in s["items"]]
    if any(a and a["type"] == "拉伸" for a in warmup):
        misses.append("G2_no_static_stretch_in_warmup")
    if not case["truth"]["parts"] and case["category"] != "灵活-徒手":
        main_parts = {p for s in plan["segments"] if s["segment"] == "main" for i in s["items"]
                      if known(i) for p in known(i)["parts"]}
        if len(main_parts & MAJOR_PARTS) < 3 and "全身" not in main_parts:
            misses.append("G5_full_body_coverage")
    return misses


def judge(case: dict[str, Any], arm: str, result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error_code")
    plan = normalize(arm, result)
    need, available = truth_need(case)
    violations: list[dict[str, Any]] = []
    misses: list[str] = []
    computed = None
    if error in ERROR_CODES:
        return {"verdict": "ERROR", "violations": [], "misses": [], "computed_min": None, "delivered": False}

    if case["expect"] == "refer":
        if error != "health_referral":
            violations.append({"rule": "R5", "detail": "提到健康状况仍给出计划" if plan else f"未转介（{error}）"})
    elif case["expect"] == "clarify" and plan is None:
        if error != "need_clarification":
            violations.append({"rule": "R6", "detail": f"未说明器械时既没反问也没给徒手计划（{error}）"})
    elif plan is None:
        violations.append({"rule": "NOT_DELIVERED", "detail": f"未交付（{error}）"})

    if plan is not None:
        if case["expect"] == "clarify":
            # 没说器械时可以反问，也可以只给徒手动作；出现任何器械动作即违反 R6
            actions = lib.load_actions()
            if any(actions.get(i["id"]) is None or actions[i["id"]]["equipment"]
                   for s in plan["segments"] for i in s["items"]):
                violations.append({"rule": "R6", "detail": "未说明器械却安排了器械动作或库外动作"})
        checked = lib.validate_plan(plan, need, available)
        violations.extend(checked["violations"])
        computed = checked["computed_min"]
        if not checked["violations"]:
            misses = guidance(case, plan)

    if violations:
        verdict = "FAIL"
    elif misses:
        verdict = "REVIEW"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "violations": violations, "misses": misses,
            "computed_min": computed, "delivered": plan is not None}


def run_case(case: dict[str, Any], arm: str, clients: dict[str, Any]) -> dict[str, Any]:
    tracer = Tracer(TRACE_PATH)
    trace_id = f"t-{case['case_id']}-{arm}-{uuid.uuid4()}"
    if arm == "v7":
        result = run_workout_plan(case["input"], clients["v7"], tracer, trace_id)
    elif arm == "llm_only":
        result = run_plan_llm_only(case["input"], clients["v2"], tracer, trace_id)
    else:
        result = run_plan_v2(case["input"], clients["v2"], tracer, trace_id)
    judged = judge(case, arm, result)
    tokens = sum((e["payload"].get("usage") or {}).get("total_tokens", 0)
                 for e in tracer.events if isinstance(e.get("payload"), dict))
    tracer.emit(trace_id, "evaluation", {
        "verdict": judged["verdict"],
        "failures": [{"step": v["rule"], "check": f"{v['rule']}:{v['detail']}"} for v in judged["violations"]]
                    + [{"step": m.split("_")[0], "check": m} for m in judged["misses"]],
        "plan_output": result,
    }, node="eval")
    return {"case_id": case["case_id"], "arm": arm, "input": case["input"], "expect": case["expect"],
            "category": case["category"], "truth_available": bool(case["truth"]["available"]), **judged, "error_code": result.get("error_code"),
            "attempts": result.get("attempts") or (2 if result.get("error_code") == "plan_invalid" else 1),
            "tokens": tokens, "trace_id": trace_id, "output": result}


def _gear_used(rows: list[dict[str, Any]]) -> str:
    actions = lib.load_actions()
    with_gear = [r for r in rows if r["expect"] == "plan" and r["delivered"] and r["truth_available"]]
    used = [r for r in with_gear if any(
        actions.get(i["id"], {}).get("equipment") for s in normalize(r["arm"], r["output"])["segments"]
        if s["segment"] == "main" for i in s["items"])]
    return f"{len(used)}/{len(with_gear)}"


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(r["verdict"] for r in rows)
    rules = Counter(v["rule"] for r in rows for v in r["violations"])
    plans = [r for r in rows if r["expect"] == "plan"]
    d1_ok = [r for r in plans if r["computed_min"] is not None and not any(v["rule"] == "D1" for v in r["violations"])]
    return {
        "verdicts": {k: verdicts.get(k, 0) for k in ("PASS", "REVIEW", "FAIL", "ERROR")},
        "violations_by_rule": dict(sorted(rules.items())),
        "delivered_plans": f"{sum(r['delivered'] for r in plans)}/{len(plans)}",
        "duration_ok": f"{len(d1_ok)}/{len(plans)}",
        "first_attempt_ok": f"{sum(1 for r in rows if r['attempts'] == 1 and r['verdict'] in ('PASS', 'REVIEW'))}/{len(rows)}",
        "guidance_misses": dict(Counter(m for r in rows for m in r["misses"])),
        "duration_adjusted": sum(1 for r in rows if r["output"].get("duration_adjusted")),
        # 观测：用户有器械时，主训练是否用上了器械（指引，不作门禁）
        "gear_used_when_available": _gear_used(rows),
        "avg_tokens": round(sum(r["tokens"] for r in rows) / len(rows)) if rows else 0,
    }


def render(metadata: dict[str, Any], by_arm: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    lines = ["# Plan v2 Comparison Report", "", *[f"- {k}: {v}" for k, v in metadata.items()], "",
             "> 判定以 Case truth 为准。红线（R*）与数据（D*）违反、未交付为 FAIL；指引（G*）不满足为 REVIEW。", "",
             "## 各组汇总", "", "| 指标 | " + " | ".join(by_arm) + " |", "|---|" + "---|" * len(by_arm)]
    for key in ("verdicts", "violations_by_rule", "delivered_plans", "duration_ok", "first_attempt_ok",
                "duration_adjusted", "gear_used_when_available", "guidance_misses", "avg_tokens"):
        lines.append(f"| {key} | " + " | ".join(
            json.dumps(m[key], ensure_ascii=False) if isinstance(m[key], dict) else str(m[key]) for m in by_arm.values()) + " |")
    lines += ["", "## 逐条结果", "", "| case | 期望 | 组 | verdict | 违反 / 未满足 | 计算时长 | 尝试 | token | trace_id |",
              "|---|---|---|:---:|---|---:|:---:|---:|---|"]
    for r in rows:
        detail = "；".join(f"{v['rule']} {v['detail']}" for v in r["violations"]) or "、".join(r["misses"]) or "-"
        lines.append(f"| {r['case_id']} {r['input']} | {r['expect']} | {r['arm']} | {r['verdict']} | "
                     f"{detail.replace('|', '/')} | {r['computed_min'] if r['computed_min'] is not None else '-'} | "
                     f"{r['attempts']} | {r['tokens']} | {r['trace_id']} |")
    lines += ["", "单条链路：`.venv/bin/python eval/trace_web.py --trace-id <trace_id>`，或在调试台「全部链路」搜索 trace_id。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--cases", default="")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if any(a not in ARMS for a in arms):
        parser.error(f"arms must be in {ARMS}")
    load_dotenv()
    cases = load_cases(args.cases)
    clients = {"v7": LiveLLM(max_tokens=MAX_PLAN_TOKENS), "v2": LiveLLM(max_tokens=MAX_PLAN_V2_TOKENS)}
    rows = []
    for run_index in range(1, args.runs + 1):
        for case in cases:
            for arm in arms:
                row = run_case(case, arm, clients)
                row["run_index"] = run_index
                rows.append(row)
                print(f"{row['case_id']} {arm}: {row['verdict']} {[v['rule'] for v in row['violations']]} "
                      f"{row['misses']} {row['computed_min']} {row['tokens']}t", flush=True)
    by_arm = {arm: metrics([r for r in rows if r["arm"] == arm]) for arm in arms}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"plan-v2-results-{ts}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "model": clients["v2"].model, "runs": args.runs,
        "arms": ",".join(arms), "cases": len(cases), "git_commit": git_commit(),
        "dataset_hash": sha256_file(DATASET_PATH), "library_hash": sha256_file(lib.DATA_PATH),
        **{f"prompt_{n}": f"{load_prompt_asset(n).version} {load_prompt_asset(n).prompt_hash[:19]}"
           for n in ("plan_need", "plan_compose", "plan_llm_only", "workout_planner", "workout_generator")},
        "total_tokens": sum(r["tokens"] for r in rows),
        "dataset_status": "AI 起草，未经主人确认",
    }
    report = RESULTS_DIR / f"plan-v2-report-{ts}.md"
    report.write_text(render(metadata, by_arm, rows), encoding="utf-8")
    print(json.dumps({"report": str(report.relative_to(ROOT)), "by_arm": by_arm}, ensure_ascii=False, indent=1))
    return 0 if all(r["verdict"] != "ERROR" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
