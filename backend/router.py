from __future__ import annotations

import json
import time
import uuid
from typing import Any

from backend.llm import LLMApiError, LLMClient, LLMTimeout, TEMPERATURE, usage_payload
from backend.prompt_registry import load_prompt_asset, prompt_path
from backend.trace import Tracer


PROMPT_NAME = "intent_router"
PROMPT_PATH = prompt_path(PROMPT_NAME)
INTENTS = {"record", "query", "reject"}


def load_prompt() -> tuple[str, str]:
    prompt = load_prompt_asset(PROMPT_NAME)
    return prompt.content, prompt.prompt_hash


def _parse_router_output_details(raw_text: str) -> tuple[str, float, bool]:
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"intent", "confidence"}:
        raise ValueError("invalid fields")
    intent = parsed["intent"]
    confidence = parsed["confidence"]
    confidence_normalized = False
    if intent not in INTENTS:
        raise ValueError("invalid intent")
    if isinstance(confidence, str):
        try:
            confidence = float(confidence)
        except ValueError as exc:
            raise ValueError("invalid confidence type") from exc
        confidence_normalized = True
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("invalid confidence type")
    if not 0 <= confidence <= 1:
        raise ValueError("invalid confidence range")
    return intent, float(confidence), confidence_normalized


def parse_router_output(raw_text: str) -> tuple[str, float]:
    intent, confidence, _ = _parse_router_output_details(raw_text)
    return intent, confidence


def route(text: str, llm: LLMClient, tracer: Tracer) -> dict[str, Any]:
    trace_id = "t-" + str(uuid.uuid4())
    text_len = len(text) if isinstance(text, str) else 0
    tracer.emit(trace_id, "input_received", {"text": text, "text_len": text_len})

    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        result = {"ok": False, "trace_id": trace_id, "error_code": "bad_request"}
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "bad_request"})
        return result

    prompt = load_prompt_asset(PROMPT_NAME)
    system_prompt, prompt_hash = prompt.content, prompt.prompt_hash
    tracer.emit(
        trace_id,
        "llm_request",
        {
            "model": llm.model,
            "prompt_name": prompt.name,
            "prompt_version": prompt.version,
            "prompt_hash": prompt_hash,
            "temperature": TEMPERATURE,
        },
    )
    started = time.perf_counter()
    try:
        llm_result = llm.complete(system_prompt, text)
    except LLMTimeout:
        result = {"ok": False, "trace_id": trace_id, "error_code": "llm_timeout"}
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"})
        return result
    except LLMApiError:
        result = {"ok": False, "trace_id": trace_id, "error_code": "llm_api_error"}
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"})
        return result

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    tracer.emit(
        trace_id,
        "llm_response",
        {
            "raw_text": llm_result.raw_text,
            "duration_ms": duration_ms,
            "usage": usage_payload(llm_result.usage),
        },
    )
    try:
        intent, confidence, confidence_normalized = _parse_router_output_details(
            llm_result.raw_text
        )
    except ValueError:
        tracer.emit(trace_id, "parse_result", {"ok": False})
        result = {"ok": False, "trace_id": trace_id, "error_code": "llm_parse_error"}
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_parse_error"})
        return result

    tracer.emit(
        trace_id,
        "parse_result",
        {
            "ok": True,
            "intent": intent,
            "confidence": confidence,
            "confidence_normalized": confidence_normalized,
        },
    )
    result = {
        "ok": True,
        "trace_id": trace_id,
        "intent": intent,
        "confidence": confidence,
        "source": "llm",
    }
    tracer.emit(trace_id, "result", {"ok": True, "intent": intent})
    return result
