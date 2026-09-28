"""统一助手：一个聊天框办完所有事。持有全部工具，多轮循环，错误回灌，有界重试。

不改动 iter-4 run_agent、V8 run_routine_agent、V7 训练计划链路，只复用它们的工具定义与执行器。
不接收 request_id：存储按 (user_id, request_id) 做整请求幂等，同一请求内第二次写入会被当成重放而丢失。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from backend.clock import Clock
from backend.llm import LLMApiError, LLMTimeout, TEMPERATURE, usage_payload
from backend.plan import run_workout_plan
from backend.action_lib_v2 import load_actions, match_actions
from backend.plan_v2 import run_plan_llm_only, run_plan_v2
from backend.weekly import summarize
from backend.prompt_registry import load_prompt_asset
from backend.routine import RoutineStore
# V8 执行器按原样复用；为不改动 V8 被测代码，直接引用其模块内函数
from backend.routine_agent import TOOL_SCHEMAS as ROUTINE_TOOL_SCHEMAS
from backend.routine_agent import _execute as execute_routine_tool
from backend.storage import StorageClient
from backend.tools import TOOL_SCHEMAS as RECORD_TOOL_SCHEMAS
from backend.tools import ToolError, make_executors
from backend.trace import Tracer


PROMPT_NAME = "assistant"
MAX_ROUNDS = 6
MAX_TOOL_CALLS = 8
# 训练计划引擎：v7 为默认（V10 验收前不切换）；v2 为 V10 链路；llm_only 仅评测对照
PLAN_ENGINES = ("v7", "v2", "llm_only")
WRITE_TOOLS = {"create_record", "set_routine_order"}

PLAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_workout_plan",
        "description": (
            "为用户生成一份单次训练计划。request 填用户对这份计划的要求原文，"
            "包括部位、水平（新手或进阶）和时长，不要改写或省略用户说过的条件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
            "additionalProperties": False,
        },
    },
}

REASON_FIELD = "reason"


def _with_reason(schema: dict[str, Any]) -> dict[str, Any]:
    """给工具加必填的 reason：结构化字段保证每次调用都有理由，且不会混进给用户的回复。
    只改助手自己的副本，iter-4 与 V8 共用的工具定义不动；执行前剥掉，不进业务参数。"""
    function = schema["function"]
    parameters = function["parameters"]
    return {"type": "function", "function": {
        **function,
        "parameters": {
            **parameters,
            "properties": {
                REASON_FIELD: {"type": "string", "description": "一句话说明为什么调用这个工具，只用于记录，不展示给用户"},
                **parameters["properties"],
            },
            "required": [REASON_FIELD, *parameters.get("required", [])],
        },
    }}


WEEKLY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "weekly_summary",
        "description": "统计用户本周或上周的训练情况（周一到周日），并给出对照 WHO 与 ACSM 建议的差距。数字以此工具返回为准。",
        "parameters": {
            "type": "object",
            "properties": {"week_offset": {"type": "integer", "description": "0 为本周，-1 为上周"}},
            "required": ["week_offset"],
            "additionalProperties": False,
        },
    },
}

GUIDE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_action_guide",
        "description": "查询动作的练习部位、难度、器械、要点与风险、替代动作。只能讲此工具返回的内容。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "用户问的动作名称"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}

TOOL_SCHEMAS = [_with_reason(s) for s in (*RECORD_TOOL_SCHEMAS, *ROUTINE_TOOL_SCHEMAS, PLAN_TOOL_SCHEMA,
                                          WEEKLY_TOOL_SCHEMA, GUIDE_TOOL_SCHEMA)]
TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
ROUTINE_TOOL_NAMES = {schema["function"]["name"] for schema in ROUTINE_TOOL_SCHEMAS}


def _run_plan_tool(args: dict[str, Any], plan_llm: Any, tracer: Tracer, trace_id: str,
                   engine: str = "v7", compose_llm: Any = None) -> dict[str, Any]:
    request = args.get("request")
    if set(args) != {"request"} or not isinstance(request, str) or not request.strip():
        return {"ok": False, "error_code": "invalid_args", "message": "generate_workout_plan 只接受非空字符串 request"}
    if engine == "v2":
        return run_plan_v2(request.strip(), plan_llm, tracer, trace_id, compose_llm)
    if engine == "llm_only":
        return run_plan_llm_only(request.strip(), plan_llm, tracer, trace_id)
    # 子链路用同一 trace_id：V7 的 planner / tool / generator 事件与助手循环串在一条链路里
    result = run_workout_plan(request.strip(), plan_llm, tracer, trace_id)
    if not result.get("ok"):
        return {"ok": False, "error_code": result.get("error_code", "plan_failed"),
                "message": "训练计划生成失败"}
    workout = result["workout"]
    return {"ok": True, "plan": workout["plan"], "note": workout.get("note"),
            "target_duration_min": workout.get("target_duration_min")}


def _action_guide(args: dict[str, Any]) -> dict[str, Any]:
    """动作指导（V11.2）：要点只来自动作库；匹配多个时返回候选让助手反问，库外动作不教。"""
    name = args.get("name")
    if set(args) != {"name"} or not isinstance(name, str) or not name.strip():
        return {"ok": False, "error_code": "invalid_args", "message": "get_action_guide 只接受非空字符串 name"}
    matched = match_actions(name)
    if not matched:
        return {"ok": True, "match": "none", "message": "动作库里没有这个动作，暂不提供指导"}
    if len(matched) > 1:
        return {"ok": True, "match": "multiple", "candidates": [a["name"] for a in matched]}
    action = matched[0]
    substitute = load_actions().get(action["substitute"]) if action["substitute"] else None
    return {"ok": True, "match": "single", "action": {
        "name": action["name"], "parts": action["parts"], "difficulty": action["difficulty"],
        "equipment": action["equipment"] or ["徒手"], "cue": action["cue"],
        "substitute": substitute["name"] if substitute else None}}


def _round_reason(decision: Any) -> str | None:
    reasons = [c.arguments.get(REASON_FIELD) for c in decision.tool_calls]
    reasons = [r.strip() for r in reasons if isinstance(r, str) and r.strip()]
    return "；".join(reasons) or (decision.text.strip() or None)


def _input_view(message: dict[str, Any], tool_names: dict[str, str]) -> dict[str, Any]:
    if message["role"] == "tool":
        try:
            content: Any = json.loads(message["content"])
        except (TypeError, json.JSONDecodeError):
            content = message["content"]
        return {"role": "tool", "tool": tool_names.get(message.get("tool_call_id")),
                "tool_call_id": message.get("tool_call_id"), "content": content}
    return {"role": message["role"], "content": message["content"]}


def run_assistant(
    text: str,
    llm: Any,
    plan_llm: Any,
    storage: StorageClient,
    routine_store: RoutineStore,
    tracer: Tracer,
    clock: Clock,
    trace_id: str | None = None,
    user_id: str = "demo-user",
    context: dict[str, Any] | None = None,
    tool_names: set[str] | None = None,
    plan_engine: str = "v7",
    plan_compose_llm: Any = None,
) -> dict[str, Any]:
    """tool_names 只给评测的「同源工具」对照组用：限制可见工具，其余逻辑不变。
    plan_engine 选择训练计划引擎；plan_llm 需与引擎匹配（v2 编排输出较长，需更高的输出上限）。"""
    if plan_engine not in PLAN_ENGINES:
        raise ValueError(f"unknown plan_engine {plan_engine!r}")
    trace_id = trace_id or f"t-{uuid.uuid4()}"
    schemas = [s for s in TOOL_SCHEMAS if tool_names is None or s["function"]["name"] in tool_names]
    prompt = load_prompt_asset(PROMPT_NAME)
    now = clock.now()
    tracer.emit(trace_id, "request", {"text": text, "user_id": user_id}, node="assistant")
    tracer.emit(
        trace_id,
        "assistant_request",
        {
            "model": llm.model,
            "prompt_name": prompt.name,
            "prompt_version": prompt.version,
            "prompt_hash": prompt.prompt_hash,
            "temperature": getattr(llm, "temperature", TEMPERATURE),
            "tools": sorted(s["function"]["name"] for s in schemas),
            "max_rounds": MAX_ROUNDS,
            "max_tool_calls": MAX_TOOL_CALLS,
            "plan_engine": plan_engine,
            "now": now.isoformat(),
            **(context or {}),
        },
        node="assistant",
    )
    tracer.emit(trace_id, "state_before", {"units": routine_store.read(user_id)}, node="state")

    # request_id=None：每次写入由存储生成独立 id，避免同一请求内的多次写入互相吞掉
    record_executors = make_executors(storage, clock, trace_id, user_id, None)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt.content},
        {"role": "user", "content": (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}"
            f"（星期{'一二三四五六日'[now.weekday()]}）\n用户输入：{text}"
        )},
    ]
    attempts: list[dict[str, Any]] = []
    tool_counts: dict[str, int] = {}
    writes = 0
    final_text = ""
    error_code = None
    rounds = 0
    sent = 1  # 已记录到 Trace 的消息位置；系统提示词只在 assistant_request 记名称与 hash

    while error_code is None:
        if rounds >= MAX_ROUNDS:
            error_code = "max_rounds_exceeded"
            break
        rounds += 1
        # 本轮模型新看到的输入：首轮是用户消息，之后是上一轮工具的返回；模型自己的上一轮输出不重复记
        names = {tc["id"]: tc["function"]["name"] for m in messages[sent:] if m["role"] == "assistant"
                 for tc in m.get("tool_calls", [])}
        new_inputs = [_input_view(m, names) for m in messages[sent:] if m["role"] != "assistant"]
        sent = len(messages)
        started = time.perf_counter()
        try:
            decision = llm.complete_with_messages(messages, schemas)
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
                "new_inputs": new_inputs,
                "tool_calls": [{"name": c.name, "args": c.arguments} for c in decision.tool_calls],
                "text": decision.text,
                # 模型给出的说明，不等于真实推理过程，只帮助阅读链路；优先取工具的 reason 字段
                "reason": _round_reason(decision) if decision.tool_calls else None,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "usage": usage_payload(decision.usage),
            },
            node="llm",
        )
        if not decision.tool_calls:
            final_text = decision.text
            break
        if len(attempts) + len(decision.tool_calls) > MAX_TOOL_CALLS:
            error_code = "too_many_tool_calls"
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
            reason = call.arguments.get(REASON_FIELD)
            args = {k: v for k, v in call.arguments.items() if k != REASON_FIELD}
            if call.name not in {s["function"]["name"] for s in schemas}:
                output = {"ok": False, "error_code": "unknown_tool", "message": f"没有工具 {call.name}"}
            elif call.name in ROUTINE_TOOL_NAMES:
                output, _ = execute_routine_tool(call.name, args, routine_store, user_id, trace_id)
            elif call.name == "generate_workout_plan":
                output = _run_plan_tool(args, plan_llm, tracer, trace_id, plan_engine, plan_compose_llm)
            elif call.name == "weekly_summary":
                offset = args.get("week_offset")
                if set(args) != {"week_offset"} or isinstance(offset, bool) or not isinstance(offset, int):
                    output = {"ok": False, "error_code": "invalid_args", "message": "week_offset 必须是整数 0 或 -1"}
                else:
                    try:
                        output = summarize(storage, user_id, now, offset)
                    except ValueError as exc:
                        output = {"ok": False, "error_code": "invalid_args", "message": str(exc)}
            elif call.name == "get_action_guide":
                output = _action_guide(args)
            else:
                try:
                    output = {"ok": True, **record_executors[call.name](args)}
                except ToolError as exc:
                    output = {"ok": False, "error_code": "invalid_args", "message": str(exc)}
            if output["ok"] and call.name in WRITE_TOOLS:
                writes += 1
            attempt = {
                "round": rounds,
                "attempt": tool_counts[call.name],
                "tool": call.name,
                "args": args,
                "reason": reason if isinstance(reason, str) else None,
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

    tracer.emit(trace_id, "state_after", {"units": routine_store.read(user_id)}, node="state")
    ok = error_code is None
    tracer.emit(
        trace_id,
        "result",
        {"ok": ok, "error_code": error_code, "rounds": rounds, "writes": writes,
         "attempt_count": len(attempts), "text": final_text},
        node="assistant",
    )
    result: dict[str, Any] = {
        "ok": ok, "trace_id": trace_id, "stage": "assistant",
        "rounds": rounds, "attempts": attempts, "writes": writes, "text": final_text,
        "actions": [{"tool": a["tool"], "ok": a["ok"]} for a in attempts],
    }
    if error_code:
        result["error_code"] = error_code
    return result
