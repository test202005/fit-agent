from __future__ import annotations

import time
from typing import Any

from backend.clock import Clock
from backend.llm import LLMApiError, LLMTimeout, TEMPERATURE, usage_payload
from backend.prompt_registry import load_prompt_asset, prompt_path
from backend.storage import StorageClient
from backend.tools import TOOL_NAMES, TOOL_SCHEMAS, ToolError, make_executors
from backend.trace import Tracer


PROMPT_NAME = "agent_system"
PROMPT_PATH = prompt_path(PROMPT_NAME)
MAX_TOOL_CALLS = 3  # PRD 决策 3：单轮上限，超出视为异常


def load_prompt() -> tuple[str, str]:
    prompt = load_prompt_asset(PROMPT_NAME)
    return prompt.content, prompt.prompt_hash


def run_agent(
    text: str,
    llm: Any,
    storage: StorageClient,
    tracer: Tracer,
    clock: Clock,
    trace_id: str,
    user_id: str = "demo-user",
    request_id: str | None = None,
) -> dict[str, Any]:
    """单轮工具调用：模型选工具 → 执行 → 回执。不做多步循环（留给 iter-5）。"""
    prompt = load_prompt_asset(PROMPT_NAME)
    system_prompt, prompt_hash = prompt.content, prompt.prompt_hash
    now = clock.now()
    user_text = (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}"
        f"（星期{'一二三四五六日'[now.weekday()]}）\n用户输入：{text}"
    )

    tracer.emit(
        trace_id,
        "agent_request",
        {
            "model": llm.model,
            "prompt_name": prompt.name,
            "prompt_version": prompt.version,
            "prompt_hash": prompt_hash,
            "temperature": TEMPERATURE,
            "tools": sorted(TOOL_NAMES),
            "now": now.isoformat(),
        },
        node="agent",
    )
    started = time.perf_counter()
    try:
        decision = llm.complete_with_tools(system_prompt, user_text, TOOL_SCHEMAS)
    except LLMTimeout:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"}, node="agent")
        return {"ok": False, "trace_id": trace_id, "stage": "agent", "error_code": "llm_timeout"}
    except LLMApiError:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"}, node="agent")
        return {"ok": False, "trace_id": trace_id, "stage": "agent", "error_code": "llm_api_error"}

    tracer.emit(
        trace_id,
        "agent_response",
        {
            "tool_calls": [
                {"name": call.name, "arguments": call.arguments} for call in decision.tool_calls
            ],
            "text_len": len(decision.text),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "usage": usage_payload(decision.usage),
        },
        node="agent",
    )

    if len(decision.tool_calls) > MAX_TOOL_CALLS:
        tracer.emit(
            trace_id,
            "result",
            {"ok": False, "error_code": "too_many_tool_calls"},
            node="agent",
        )
        return {
            "ok": False,
            "trace_id": trace_id,
            "stage": "agent",
            "error_code": "too_many_tool_calls",
        }

    executors = make_executors(storage, clock, trace_id, user_id, request_id)
    trajectory: list[dict[str, Any]] = []
    for index, call in enumerate(decision.tool_calls, start=1):
        step: dict[str, Any] = {
            "step": index,
            "tool": call.name,
            "arguments": call.arguments,
        }
        if call.name not in executors:
            # 模型选了不存在的工具
            step.update(ok=False, error="unknown_tool")
            trajectory.append(step)
            tracer.emit(trace_id, "tool_result", step, node="tool")
            continue
        try:
            output = executors[call.name](call.arguments)
        except ToolError as exc:
            step.update(ok=False, error=str(exc))
        else:
            step.update(ok=True, output=output)
        trajectory.append(step)
        tracer.emit(trace_id, "tool_result", step, node="tool")

    failed = [step for step in trajectory if not step["ok"]]
    tracer.emit(
        trace_id,
        "result",
        {"ok": True, "tool_count": len(trajectory), "failed": len(failed)},
        node="agent",
    )
    return {
        "ok": True,
        "trace_id": trace_id,
        "stage": "agent",
        "trajectory": trajectory,
        "tool_count": len(trajectory),
        "failed_steps": [step["step"] for step in failed],
        # 早期错误会级联，归因看第一个出错的步骤
        "first_failure_step": failed[0]["step"] if failed else None,
        "text": decision.text,
    }
