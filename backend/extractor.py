from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.llm import LLMApiError, LLMClient, LLMTimeout, TEMPERATURE
from backend.storage import RECORD_FIELDS
from backend.trace import Tracer


PROMPT_PATH = Path(__file__).parent / "prompts" / "extractor_v1.txt"
QUANT_FIELDS = ("weight_kg", "sets", "reps", "duration_min", "distance_km")
STATES = ("complete", "incomplete", "invalid")


def load_prompt() -> tuple[str, str]:
    content = PROMPT_PATH.read_text(encoding="utf-8")
    return content, "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_extractor_output(raw_text: str) -> list[dict[str, Any]]:
    """严格解析：结构不符一律拒绝，不修补、不猜测。"""
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"records"}:
        raise ValueError("invalid top-level fields")
    records = parsed["records"]
    if not isinstance(records, list):
        raise ValueError("records must be a list")

    cleaned = []
    for record in records:
        if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
            raise ValueError("invalid record fields")
        exercise = record["exercise"]
        if not isinstance(exercise, str) or not exercise.strip():
            raise ValueError("invalid exercise")
        item: dict[str, Any] = {"exercise": exercise.strip()}
        for field in QUANT_FIELDS:
            value = record[field]
            if value is None:
                item[field] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid {field}")
            if value <= 0:
                raise ValueError(f"non-positive {field}")
            item[field] = value
        cleaned.append(item)
    return cleaned


def decide_record_state(record: dict[str, Any]) -> str:
    """单条记录的三态：有动作且有量化才算完整。有氧的量化是时长或距离。"""
    if not record.get("exercise"):
        return "invalid"
    if any(record.get(field) is not None for field in QUANT_FIELDS):
        return "complete"
    return "incomplete"


def aggregate_state(records: list[dict[str, Any]]) -> str:
    """整体状态取最保守的一条：抽不到记录是 invalid，有任一条缺量化即 incomplete。"""
    if not records:
        return "invalid"
    states = {record["state"] for record in records}
    if "incomplete" in states:
        return "incomplete"
    return "complete"


def extract(
    text: str, llm: LLMClient, tracer: Tracer, trace_id: str
) -> dict[str, Any]:
    """trace_id 由上游传入，保证一条链路只有一个 id。"""
    system_prompt, prompt_hash = load_prompt()
    tracer.emit(
        trace_id,
        "extract_request",
        {"model": llm.model, "prompt_hash": prompt_hash, "temperature": TEMPERATURE},
        node="extractor",
    )
    started = time.perf_counter()
    try:
        llm_result = llm.complete(system_prompt, text)
    except LLMTimeout:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"}, node="extractor")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_timeout"}
    except LLMApiError:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"}, node="extractor")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_api_error"}

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    tracer.emit(
        trace_id,
        "extract_response",
        {"raw_text": llm_result.raw_text, "duration_ms": duration_ms},
        node="extractor",
    )
    try:
        records = parse_extractor_output(llm_result.raw_text)
    except ValueError:
        tracer.emit(trace_id, "parse_result", {"ok": False}, node="extractor")
        tracer.emit(
            trace_id, "result", {"ok": False, "error_code": "llm_parse_error"}, node="extractor"
        )
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_parse_error"}

    for record in records:
        record["state"] = decide_record_state(record)
    state = aggregate_state(records)
    tracer.emit(
        trace_id,
        "parse_result",
        {"ok": True, "record_count": len(records)},
        node="extractor",
    )
    tracer.emit(trace_id, "state_decided", {"state": state}, node="extractor")
    tracer.emit(trace_id, "result", {"ok": True, "state": state}, node="extractor")
    return {"ok": True, "trace_id": trace_id, "state": state, "records": records}
