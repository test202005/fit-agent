"""训练安排助手：多轮工具循环，工具结果与错误回灌给模型，有界重试。

与 run_agent（单轮工具调用）并存，不改动其行为。每次工具尝试单独记一条 Trace，
「首次被拒、重试成功」在端到端结果里看不出来，只能在逐次尝试里看到。
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend.clock import Clock
from backend.llm import LLMApiError, LLMTimeout, TEMPERATURE, usage_payload
from backend.prompt_registry import load_prompt_asset
from backend.routine import RoutineProtocolError, RoutineStore, validate_order
from backend.trace import Tracer


PROMPT_NAME = "routine_agent"
MAX_ROUNDS = 5  # 单次请求模型调用上限，超出不再执行

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_routine",
            "description": (
                "读取用户当前的训练安排（按顺序轮换执行的训练单元）。"
                "默认只返回进行中（active）的单元；include_paused=true 时同时返回已暂停（paused）的单元。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_paused": {"type": "boolean", "description": "是否包含已暂停的单元，默认 false"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_routine_order",
            "description": (
                "用新的顺序整体替换训练安排。order 为 unit_id 列表，按新顺序排列，"
                "必须包含全部训练单元。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}


def _execute(
    name: str, args: dict[str, Any], store: RoutineStore, user_id: str, trace_id: str,
) -> tuple[dict[str, Any], bool]:
    """返回 (回给模型的结果, 是否写入)。协议错误以结构化结果返回，不抛给循环。"""
    if name == "get_routine":
        include_paused = args.get("include_paused", False)
        if set(args) - {"include_paused"} or not isinstance(include_paused, bool):
            return {"ok": False, "error_code": "invalid_args",
                    "message": "get_routine 只接受布尔参数 include_paused"}, False
        units = store.read(user_id)
        if not include_paused:
            units = [u for u in units if u["status"] == "active"]
        return {"ok": True, "units": [
            {"position": i, "unit_id": u["unit_id"], "name": u["name"], "status": u["status"]}
            for i, u in enumerate(units, start=1)
        ]}, False
    if name == "set_routine_order":
        if set(args) != {"order"}:
            return {"ok": False, "error_code": "invalid_args",
                    "message": "set_routine_order 只接受参数 order"}, False
        try:
            order = validate_order(store.read(user_id), args["order"])
        except RoutineProtocolError as exc:
            return {"ok": False, "error_code": exc.code, "message": exc.message}, False
        store.replace_order(user_id, order, trace_id)
        return {"ok": True, "written": True}, True
    return {"ok": False, "error_code": "unknown_tool", "message": f"没有工具 {name}"}, False


def run_routine_agent(
    text: str,
    llm: Any,
    store: RoutineStore,
    tracer: Tracer,
    clock: Clock,
    trace_id: str,
    user_id: str = "demo-user",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """context 只进 Trace（如 Fixture id、实验组），不影响执行。"""
    prompt = load_prompt_asset(PROMPT_NAME)
    now = clock.now()
    tracer.emit(trace_id, "request", {"text": text, "user_id": user_id}, node="routine")
    tracer.emit(
        trace_id,
        "routine_request",
        {
            "model": llm.model,
            "prompt_name": prompt.name,
            "prompt_version": prompt.version,
            "prompt_hash": prompt.prompt_hash,
            "temperature": getattr(llm, "temperature", TEMPERATURE),
            "tools": sorted(TOOL_NAMES),
            "max_rounds": MAX_ROUNDS,
            "now": now.isoformat(),
            **(context or {}),
        },
        node="routine",
    )
    before = store.read(user_id)
    tracer.emit(trace_id, "state_before", {"units": before}, node="state")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt.content},
        {"role": "user", "content": f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}\n用户输入：{text}"},
    ]
    attempts: list[dict[str, Any]] = []
    tool_counts: dict[str, int] = {}
    writes = 0
    final_text = ""
    error_code = None
    rounds = 0

    while True:
        if rounds >= MAX_ROUNDS:
            error_code = "max_rounds_exceeded"
            break
        rounds += 1
        started = time.perf_counter()
        try:
            decision = llm.complete_with_messages(messages, TOOL_SCHEMAS)
        except LLMTimeout:
            error_code = "llm_timeout"
            break
        except LLMApiError:
            error_code = "llm_api_error"
            break
        tracer.emit(
            trace_id,
            "llm_round",
            {
                "round": rounds,
                "message_count": len(messages),
                "tool_calls": [{"name": c.name, "args": c.arguments} for c in decision.tool_calls],
                "text": decision.text,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "usage": usage_payload(decision.usage),
            },
            node="llm",
        )
        if not decision.tool_calls:
            final_text = decision.text
            break

        messages.append({
            "role": "assistant",
            "content": decision.text or None,
            "tool_calls": [
                {
                    "id": call.id or f"call-{rounds}-{i}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for i, call in enumerate(decision.tool_calls)
            ],
        })
        for i, call in enumerate(decision.tool_calls):
            tool_counts[call.name] = tool_counts.get(call.name, 0) + 1
            started = time.perf_counter()
            output, wrote = _execute(call.name, call.arguments, store, user_id, trace_id)
            writes += int(wrote)
            attempt = {
                "round": rounds,
                "attempt": tool_counts[call.name],
                "tool": call.name,
                "args": call.arguments,
                "raw_args": call.raw_arguments,
                "ok": output["ok"],
                "error_code": output.get("error_code"),
                "message": output.get("message"),
                "output": output,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            attempts.append(attempt)
            tracer.emit(trace_id, "tool_attempt", attempt, node="tool")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id or f"call-{rounds}-{i}",
                "content": json.dumps(output, ensure_ascii=False),
            })

    after = store.read(user_id)
    tracer.emit(trace_id, "state_after", {"units": after}, node="state")
    ok = error_code is None
    tracer.emit(
        trace_id,
        "result",
        {"ok": ok, "error_code": error_code, "rounds": rounds, "writes": writes,
         "attempt_count": len(attempts), "text": final_text},
        node="routine",
    )
    result: dict[str, Any] = {
        "ok": ok, "trace_id": trace_id, "stage": "routine",
        "rounds": rounds, "attempts": attempts, "writes": writes,
        "final_units": after, "text": final_text,
    }
    if error_code:
        result["error_code"] = error_code
    return result
