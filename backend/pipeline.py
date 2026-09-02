from __future__ import annotations

from typing import Any

from backend.clock import Clock, SystemClock
from backend.extractor import extract
from backend.llm import LLMClient
from backend.query import execute_plan, plan_query
from backend.router import route
from backend.storage import StorageClient
from backend.trace import Tracer


def handle_message(
    text: str,
    router_llm: LLMClient,
    extractor_llm: LLMClient,
    storage: StorageClient,
    tracer: Tracer,
    query_llm: LLMClient | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """router -> extractor/planner -> storage 编排。同一 trace_id 贯穿全链路。"""
    clock = clock or SystemClock()
    query_llm = query_llm or extractor_llm

    routed = route(text, router_llm, tracer)
    trace_id = routed["trace_id"]
    if not routed["ok"]:
        return {**routed, "stage": "router"}

    if routed["intent"] == "record":
        return _handle_record(text, extractor_llm, storage, tracer, trace_id, routed, clock)
    if routed["intent"] == "query":
        return _handle_query(text, query_llm, storage, tracer, trace_id, routed, clock)
    return {**routed, "stage": "router"}


def _handle_record(
    text: str,
    llm: LLMClient,
    storage: StorageClient,
    tracer: Tracer,
    trace_id: str,
    routed: dict[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    extracted = extract(text, llm, tracer, trace_id)
    if not extracted["ok"]:
        return {**extracted, "stage": "extractor"}

    state = extracted["state"]
    # invalid 一律不写：抽不出动作的内容进库就是脏数据
    to_write = extracted["records"] if state != "invalid" else []
    tracer.emit(
        trace_id, "write_attempted", {"state": state, "count": len(to_write)}, node="storage"
    )
    written_ids = storage.append(to_write, trace_id, clock.now())
    tracer.emit(
        trace_id, "write_result", {"written": len(written_ids), "ids": written_ids}, node="storage"
    )
    return {
        "ok": True,
        "trace_id": trace_id,
        "stage": "storage",
        "intent": routed["intent"],
        "state": state,
        "records": extracted["records"],
        "written_ids": written_ids,
    }


def _handle_query(
    text: str,
    llm: LLMClient,
    storage: StorageClient,
    tracer: Tracer,
    trace_id: str,
    routed: dict[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    planned = plan_query(text, llm, tracer, trace_id, clock)
    if not planned["ok"]:
        return {**planned, "stage": "planner"}

    plan = planned["plan"]
    # 执行是纯代码：查询错了要能分清是没听懂用户，还是查错了数据
    outcome = execute_plan(plan, storage)
    tracer.emit(
        trace_id,
        "query_executed",
        {"type": plan["type"], "count": outcome["count"], "supported": outcome["supported"]},
        node="executor",
    )
    tracer.emit(trace_id, "result", {"ok": True, "count": outcome["count"]}, node="executor")
    return {
        "ok": True,
        "trace_id": trace_id,
        "stage": "executor",
        "intent": routed["intent"],
        "query": plan,
        "records": outcome["records"],
        "count": outcome["count"],
        "supported": outcome["supported"],
    }
