"""训练计划 v2（V10）：需求解析 → 前置判定 → 候选筛选 → 模型编排 → 代码校验与核算 → 重生成 1 次。

另含对照组 llm_only：模型直接写计划，不给动作库、不做校验，只在评测中使用。
V7 链路（backend/plan.py）不变。
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend.action_lib_v2 import (
    DURATION_TOLERANCE,
    LIMITS,
    all_equipment,
    candidates,
    enrich,
    fit_duration,
    load_actions,
    validate_plan,
)
from backend.llm import LLMApiError, LLMTimeout, usage_payload
from backend.prompt_registry import load_prompt_asset
from backend.trace import Tracer


NODE = "plan_v2"
GOALS = ("减脂", "增肌", "核心", "灵活", "未指定")
PARTS = ("下肢", "臀", "胸", "背", "肩", "手臂", "核心", "全身")
LEVELS = ("新手", "进阶", "未说明")
EQUIPMENT_WORDS = ("哑铃", "弹力带", "壶铃", "凳子", "跳绳")
DEFAULT_LEVEL = "新手"
DEFAULT_DURATION = 30
MAX_ATTEMPTS = 2  # 首次 + 重生成 1 次


class NeedError(ValueError):
    pass


def parse_need(raw_text: str) -> dict[str, Any]:
    """严格解析；取值不在词表内一律拒绝，不在这里猜。"""
    try:
        data = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NeedError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise NeedError("need must be an object")
    if data.get("goal") not in GOALS or data.get("level") not in LEVELS:
        raise NeedError("invalid goal or level")
    parts, exclusions = data.get("parts"), data.get("exclusions")
    if not isinstance(parts, list) or any(p not in PARTS for p in parts):
        raise NeedError("invalid parts")
    if not isinstance(exclusions, list) or any(e not in (*PARTS, "跳跃") for e in exclusions):
        raise NeedError("invalid exclusions")
    duration = data.get("duration_min")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 180):
        raise NeedError("invalid duration_min")
    equipment = data.get("equipment")
    if equipment is not None and (not isinstance(equipment, list) or any(e not in EQUIPMENT_WORDS for e in equipment)):
        raise NeedError("invalid equipment")
    flags = data.get("health_flags")
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        raise NeedError("invalid health_flags")
    if not isinstance(data.get("gym"), bool):
        raise NeedError("invalid gym")
    return {"goal": data["goal"], "parts": parts, "level": data["level"], "duration_min": duration,
            "gym": data["gym"], "equipment": equipment, "exclusions": exclusions, "health_flags": flags}


def resolve(need: dict[str, Any]) -> dict[str, Any]:
    """缺省值：水平按新手、时长按 30 分钟（PRD 第 9 节）。器械不设缺省（红线 R6）。"""
    resolved = dict(need)
    defaults = []
    if need["level"] == "未说明":
        resolved["level"] = DEFAULT_LEVEL
        defaults.append(f"未说明水平，按{DEFAULT_LEVEL}安排")
    if need["duration_min"] is None:
        resolved["duration_min"] = DEFAULT_DURATION
        defaults.append(f"未说明时长，按 {DEFAULT_DURATION} 分钟安排")
    resolved["defaults"] = defaults
    return resolved


def available_equipment(need: dict[str, Any]) -> set[str] | None:
    if need["gym"]:
        return all_equipment()
    if need["equipment"] is None:
        return None
    return set(need["equipment"])


def _llm_json(llm: Any, prompt_name: str, user_text: str, tracer: Tracer, trace_id: str,
              event: str, extra: dict[str, Any]) -> tuple[str | None, str | None]:
    prompt = load_prompt_asset(prompt_name)
    started = time.perf_counter()
    try:
        result = llm.complete(prompt.content, user_text)
    except LLMTimeout:
        return None, "llm_timeout"
    except LLMApiError:
        return None, "llm_api_error"
    tracer.emit(trace_id, event, {
        **extra, "model": llm.model, "temperature": getattr(llm, "temperature", None), "prompt_name": prompt.name, "prompt_version": prompt.version,
        "prompt_hash": prompt.prompt_hash, "raw_text": result.raw_text,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2), "usage": usage_payload(result.usage),
    }, node=NODE)
    return result.raw_text, None


def _fail(tracer: Tracer, trace_id: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    tracer.emit(trace_id, "result", {"ok": False, "error_code": code, "message": message, **extra}, node=NODE)
    return {"ok": False, "engine": "v2", "error_code": code, "message": message, **extra}


def run_plan_v2(request: str, llm: Any, tracer: Tracer, trace_id: str,
                compose_llm: Any = None) -> dict[str, Any]:
    """llm 用于需求解析；compose_llm 用于编排，缺省同 llm。产品入口给编排更高温度，评测不传则全程同一温度。"""
    compose_llm = compose_llm or llm
    tracer.emit(trace_id, "request", {"text": request, "engine": "v2"}, node=NODE)

    raw, error = _llm_json(llm, "plan_need", request, tracer, trace_id, "need_response", {})
    if error:
        return _fail(tracer, trace_id, error, "需求解析调用失败")
    try:
        need = resolve(parse_need(raw))
    except NeedError as exc:
        tracer.emit(trace_id, "need_parsed", {"ok": False, "error": str(exc)}, node=NODE)
        return _fail(tracer, trace_id, "need_parse_error", "没能理解训练需求，请换个说法")
    tracer.emit(trace_id, "need_parsed", {"ok": True, "need": need}, node=NODE)

    # 前置判定：红线 R5（健康状况）与 R6（未说明器械）
    if need["health_flags"]:
        return _fail(tracer, trace_id, "health_referral",
                     "提到了健康状况，不提供针对性训练计划，建议先咨询医生或专业人士",
                     health_flags=need["health_flags"])
    available = available_equipment(need)
    if available is None:
        return _fail(tracer, trace_id, "need_clarification", "需要先确认在哪里练、有哪些器械",
                     missing=["equipment"])

    pool = candidates(available, need["level"], need["exclusions"])
    tracer.emit(trace_id, "candidates", {"count": len(pool), "ids": [a["id"] for a in pool],
                                          "available_equipment": sorted(available)}, node=NODE)
    if not pool:
        return _fail(tracer, trace_id, "no_candidates", "在当前器械与条件下没有可用动作")

    target = need["duration_min"]
    base = {
        "need": {k: need[k] for k in ("goal", "parts", "level", "duration_min", "exclusions")},
        "duration_range_min": [round(target * (1 - DURATION_TOLERANCE), 1), round(target * (1 + DURATION_TOLERANCE), 1)],
        "limits": LIMITS[need["level"]] | {"seconds": [10, 600], "rest_sec": [0, 180]},
        "candidates": [{k: a[k] for k in ("id", "name", "type", "parts", "equipment", "difficulty",
                                          "measure", "seconds", "unilateral", "goals")} for a in pool],
        "user_request": request,
    }
    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        payload = dict(base)
        if feedback:
            payload["previous_output"] = feedback["raw"]
            payload["violations"] = [v["detail"] for v in feedback["violations"]]
            payload["instruction"] = "上一版被代码退回，请针对 violations 逐条修正后重新输出完整计划"
        raw, error = _llm_json(compose_llm, "plan_compose", json.dumps(payload, ensure_ascii=False),
                               tracer, trace_id, "compose_response", {"attempt": attempt})
        if error:
            return _fail(tracer, trace_id, error, "编排调用失败")
        try:
            plan = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            plan = None
        checked = validate_plan(plan, need, available)
        adjusted = None
        if checked["violations"] and {v["rule"] for v in checked["violations"]} == {"D1"}:
            # 动作与结构都合格、只差时长：由代码在 D2 范围内调数字，不必重生成
            fit = fit_duration(plan, need)
            if fit is not None:
                adjusted = {"from_min": checked["computed_min"], "changes": fit["changes"]}
                plan = fit["plan"]
                checked = validate_plan(plan, need, available)
        tracer.emit(trace_id, "compose_checked", {"attempt": attempt, **checked, "adjusted": adjusted}, node=NODE)
        if not checked["violations"]:
            note = "；".join([*need["defaults"], plan.get("note") or ""]).strip("；")
            if adjusted:
                note = "；".join(filter(None, [note, "组数、轮数或休息已由代码按目标时长微调"]))
            result = {"ok": True, "engine": "v2", "target_duration_min": target,
                      "computed_duration_min": checked["computed_min"], "segments": enrich(plan),
                      "note": note, "attempts": attempt, "duration_adjusted": adjusted is not None}
            tracer.emit(trace_id, "result", {"ok": True, "attempts": attempt,
                                              "computed_duration_min": checked["computed_min"]}, node=NODE)
            return result
        feedback = {"raw": raw, "violations": checked["violations"]}
    return _fail(tracer, trace_id, "plan_invalid", "计划未通过安全与数据校验，本次不交付",
                 violations=feedback["violations"], attempts=MAX_ATTEMPTS)


# ---------- 对照组：纯大模型生成 ----------


def match_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    key = name.replace(" ", "").replace("（V7）", "")
    for action in load_actions().values():
        if action["name"].replace(" ", "") == key:
            return action["id"]
    return None


def run_plan_llm_only(request: str, llm: Any, tracer: Tracer, trace_id: str) -> dict[str, Any]:
    """不给动作库、不做校验：看模型自由发挥时会出现什么。动作按名称匹配动作库，匹配不上保留原名。"""
    tracer.emit(trace_id, "request", {"text": request, "engine": "llm_only"}, node=NODE)
    raw, error = _llm_json(llm, "plan_llm_only", request, tracer, trace_id, "compose_response", {"attempt": 1})
    if error:
        return _fail(tracer, trace_id, error, "生成调用失败")
    try:
        plan = json.loads(raw)
        segments = [{"segment": s.get("segment"), "items": [
            {**{k: i.get(k) for k in ("name", "sets", "reps", "seconds", "rest_sec")}, "id": match_name(i.get("name"))}
            for i in s.get("items", [])]} for s in plan["segments"]]
    except (TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return _fail(tracer, trace_id, "llm_parse_error", "输出不是合法计划")
    result = {"ok": True, "engine": "llm_only", "claimed_duration_min": plan.get("duration_min"),
              "segments": segments, "note": plan.get("note")}
    tracer.emit(trace_id, "result", {"ok": True, "unmatched": [i["name"] for s in segments for i in s["items"]
                                                              if i["id"] is None]}, node=NODE)
    return result
