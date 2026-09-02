from __future__ import annotations

from typing import Any

from backend.extractor import extract
from backend.llm import LLMClient
from backend.router import route
from backend.storage import StorageClient
from backend.trace import Tracer


def handle_message(
    text: str,
    router_llm: LLMClient,
    extractor_llm: LLMClient,
    storage: StorageClient,
    tracer: Tracer,
) -> dict[str, Any]:
    """router -> extractor -> storage 编排。同一 trace_id 贯穿三层。"""
    routed = route(text, router_llm, tracer)
    trace_id = routed["trace_id"]
    if not routed["ok"]:
        return {**routed, "stage": "router"}
    if routed["intent"] != "record":
        # query / reject 本迭代不处理下游，原样返回
        return {**routed, "stage": "router"}

    extracted = extract(text, extractor_llm, tracer, trace_id)
    if not extracted["ok"]:
        return {**extracted, "stage": "extractor"}

    state = extracted["state"]
    # invalid 一律不写：抽不出动作的内容进库就是脏数据
    to_write = extracted["records"] if state != "invalid" else []
    tracer.emit(
        trace_id,
        "write_attempted",
        {"state": state, "count": len(to_write)},
        node="storage",
    )
    written_ids = storage.append(to_write, trace_id)
    tracer.emit(
        trace_id,
        "write_result",
        {"written": len(written_ids), "ids": written_ids},
        node="storage",
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
