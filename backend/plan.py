from __future__ import annotations

import json
import time
from typing import Any

from backend.action_lib import DIFFICULTIES, LEVELS, MUSCLES, query_action_lib
from backend.llm import LLMApiError, LLMClient, LLMTimeout, TEMPERATURE, usage_payload
from backend.prompt_registry import load_prompt_asset
from backend.trace import Tracer


PLANNER_PROMPT_NAME = "workout_planner"
GENERATOR_PROMPT_NAME = "workout_generator"
TOOL_NAME = "query_action_lib"
NEED_FIELDS = ("muscle", "level", "difficulty", "duration_min")
PLAN_FIELDS = ("name", "sets", "reps", "rest_sec")
PLAN_RESULT_FIELDS = ("target_duration_min", "estimated_duration_min", "plan", "note")


# ---------- 节点一：planner，把口语需求解析成结构化字段 ----------


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，True 会被当成 1，必须单独排掉
    return isinstance(value, int) and not isinstance(value, bool)


def parse_need(raw_text: str) -> dict[str, Any]:
    """严格解析需求字段，结构不符一律拒绝。

    这里只判「表达错」（字段缺、标签不在词表内）；难度与水平不匹配属于「理解错」，
    放行到工具入参，由评测断言抓——不然两类错误在 trace 里就分不开了。
    """
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != set(NEED_FIELDS):
        raise ValueError("invalid need fields")
    if parsed["muscle"] not in MUSCLES:
        raise ValueError("unknown muscle")
    if parsed["level"] not in LEVELS:
        raise ValueError("unknown level")
    if parsed["difficulty"] not in DIFFICULTIES:
        raise ValueError("unknown difficulty")
    if not _is_int(parsed["duration_min"]) or not 5 <= parsed["duration_min"] <= 300:
        raise ValueError("invalid duration_min")
    return {
        "muscle": parsed["muscle"],
        "level": parsed["level"],
        "difficulty": parsed["difficulty"],
        "duration_min": parsed["duration_min"],
    }


def parse_workout(raw_text: str) -> dict[str, Any]:
    """严格解析训练计划。空计划是合法结果（动作库确实没有匹配）。"""
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != set(PLAN_RESULT_FIELDS):
        raise ValueError("invalid plan fields")
    if not _is_int(parsed["target_duration_min"]) or not 5 <= parsed["target_duration_min"] <= 300:
        raise ValueError("invalid target_duration_min")
    if parsed["estimated_duration_min"] is not None:
        raise ValueError("duration estimate has no timing basis")
    if not isinstance(parsed["note"], str) or not parsed["note"].strip():
        raise ValueError("invalid note")
    if not isinstance(parsed["plan"], list):
        raise ValueError("invalid plan list")

    plan = []
    for item in parsed["plan"]:
        if not isinstance(item, dict) or set(item) != set(PLAN_FIELDS):
            raise ValueError("invalid plan item fields")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError("invalid plan item name")
        for field in ("sets", "reps", "rest_sec"):
            if not _is_int(item[field]) or item[field] <= 0:
                raise ValueError(f"invalid plan item {field}")
        plan.append(
            {
                "name": item["name"].strip(),
                "sets": item["sets"],
                "reps": item["reps"],
                "rest_sec": item["rest_sec"],
            }
        )
    return {"target_duration_min": parsed["target_duration_min"],
            "estimated_duration_min": None, "plan": plan, "note": parsed["note"]}


# ---------- 三个节点：planner -> tool -> generator ----------


def plan_need(text: str, llm: LLMClient, tracer: Tracer, trace_id: str) -> dict[str, Any]:
    prompt = load_prompt_asset(PLANNER_PROMPT_NAME)
    tracer.emit(
        trace_id,
        "plan_request",
        {"model": llm.model, "prompt_name": prompt.name, "prompt_version": prompt.version,
         "prompt_hash": prompt.prompt_hash, "temperature": TEMPERATURE},
        node="planner",
    )
    started = time.perf_counter()
    try:
        result = llm.complete(prompt.content, text)
    except LLMTimeout:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"}, node="planner")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_timeout"}
    except LLMApiError:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"}, node="planner")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_api_error"}

    # 模型原始输出与最终计划分开记：这条链路里 raw_text 是「理解」的原始证据
    tracer.emit(
        trace_id,
        "plan_response",
        {"raw_text": result.raw_text,
         "duration_ms": round((time.perf_counter() - started) * 1000, 2),
         "usage": usage_payload(result.usage)},
        node="planner",
    )
    try:
        need = parse_need(result.raw_text)
    except ValueError:
        tracer.emit(trace_id, "parse_result", {"ok": False}, node="planner")
        tracer.emit(
            trace_id, "result", {"ok": False, "error_code": "llm_parse_error"}, node="planner"
        )
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_parse_error"}

    tracer.emit(trace_id, "parse_result", {"ok": True, "need": need}, node="planner")
    return {"ok": True, "trace_id": trace_id, "need": need}


def call_action_tool(
    need: dict[str, Any],
    tracer: Tracer,
    trace_id: str,
    query=query_action_lib,
) -> dict[str, Any]:
    """工具节点。入参是这条链路唯一的一等断言对象。

    黑盒只看最终计划，模型把「练腿」听成「练胸」时输出依然完全合法；
    入参进 trace 之后，错误在调工具那一刻就已经定型，不用等计划出来。
    """
    arguments = {"muscle": need["muscle"], "difficulty": need["difficulty"]}
    tracer.emit(
        trace_id, "tool_call", {"tool": TOOL_NAME, "arguments": arguments}, node="tool"
    )
    observation = query(**arguments)
    tracer.emit(
        trace_id,
        "tool_result",
        {
            "count": observation["count"],
            "names": [action["name"] for action in observation["actions"]],
            "reason": observation["reason"],
        },
        node="tool",
    )
    return {"ok": True, "trace_id": trace_id, "arguments": arguments, "observation": observation}


def generate_workout(
    text: str,
    need: dict[str, Any],
    observation: dict[str, Any],
    llm: LLMClient,
    tracer: Tracer,
    trace_id: str,
) -> dict[str, Any]:
    prompt = load_prompt_asset(GENERATOR_PROMPT_NAME)
    # 只喂模型需要的：reason 是评测用来归因的内部分类，不进模型输入
    visible = {"count": observation["count"], "actions": observation["actions"]}
    user_text = (
        f"用户原始需求：{text}\n"
        f"需求解析结果：{json.dumps(need, ensure_ascii=False)}\n"
        f"动作库查询结果：{json.dumps(visible, ensure_ascii=False)}"
    )
    tracer.emit(
        trace_id,
        "generate_request",
        {"model": llm.model, "prompt_name": prompt.name, "prompt_version": prompt.version,
         "prompt_hash": prompt.prompt_hash, "temperature": TEMPERATURE,
         "observation_count": observation["count"], "input": user_text},
        node="generator",
    )
    started = time.perf_counter()
    try:
        result = llm.complete(prompt.content, user_text)
    except LLMTimeout:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"}, node="generator")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_timeout"}
    except LLMApiError:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"}, node="generator")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_api_error"}

    tracer.emit(
        trace_id,
        "generate_response",
        {"raw_text": result.raw_text,
         "duration_ms": round((time.perf_counter() - started) * 1000, 2),
         "usage": usage_payload(result.usage)},
        node="generator",
    )
    try:
        workout = parse_workout(result.raw_text)
        if workout["target_duration_min"] != need["duration_min"]:
            raise ValueError("target duration changed")
    except ValueError:
        tracer.emit(trace_id, "parse_result", {"ok": False}, node="generator")
        tracer.emit(
            trace_id, "result", {"ok": False, "error_code": "llm_parse_error"}, node="generator"
        )
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_parse_error"}

    tracer.emit(
        trace_id,
        "parse_result",
        {"ok": True, "target_duration_min": workout["target_duration_min"],
         "estimated_duration_min": workout["estimated_duration_min"],
         "action_count": len(workout["plan"])},
        node="generator",
    )
    tracer.emit(trace_id, "result", {"ok": True, "action_count": len(workout["plan"])}, node="generator")
    return {"ok": True, "trace_id": trace_id, "workout": workout}


def run_workout_plan(text: str, llm: LLMClient, tracer: Tracer, trace_id: str) -> dict[str, Any]:
    """解析 -> 调工具 -> 编排。同一 trace_id 贯穿，观察结果回灌给模型再生成。"""
    tracer.emit(trace_id, "request", {"text": text}, node="planner")

    planned = plan_need(text, llm, tracer, trace_id)
    if not planned["ok"]:
        return {**planned, "stage": "planner"}

    need = planned["need"]
    called = call_action_tool(need, tracer, trace_id)
    observation = called["observation"]

    generated = generate_workout(text, need, observation, llm, tracer, trace_id)
    if not generated["ok"]:
        return {**generated, "stage": "generator"}

    return {
        "ok": True,
        "trace_id": trace_id,
        "stage": "generator",
        "need": need,
        "tool_call": {"name": TOOL_NAME, "arguments": called["arguments"]},
        "observation": observation,
        "workout": generated["workout"],
    }
