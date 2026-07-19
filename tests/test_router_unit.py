import json

import pytest

from backend.llm import StubLLM
from backend.router import parse_router_output, route
from backend.trace import Tracer
from eval.run_intent_eval import render_report


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"intent":"record","confidence":0.9}', ("record", 0.9)),
        ('{"intent":"query","confidence":0}', ("query", 0.0)),
        ('{"intent":"reject","confidence":1}', ("reject", 1.0)),
        ('{"intent":"record","confidence":"0.9"}', ("record", 0.9)),
    ],
)
def test_parse_router_output_valid(raw, expected):
    assert parse_router_output(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '```json\n{"intent":"record","confidence":0.9}\n```',
        '{"intent":"other","confidence":0.9}',
        '{"intent":"record"}',
        '{"intent":"record","confidence":true}',
        '{"intent":"record","confidence":"high"}',
        '{"intent":"record","confidence":1.1}',
        '{"intent":"record","confidence":0.9,"extra":1}',
    ],
)
def test_parse_router_output_invalid(raw):
    with pytest.raises(ValueError):
        parse_router_output(raw)


@pytest.mark.parametrize("text", [None, "", "   ", "x" * 501])
def test_bad_request_does_not_call_llm(text):
    tracer = Tracer()
    result = route(text, StubLLM(raw_text="not-used"), tracer)
    assert result["error_code"] == "bad_request"
    assert "llm_request" not in {event["event"] for event in tracer.events}


def test_500_characters_is_allowed():
    tracer = Tracer()
    raw = json.dumps({"intent": "reject", "confidence": 0.8})
    result = route("x" * 500, StubLLM(raw_text=raw), tracer)
    assert result["ok"] is True


def test_stub_success_has_complete_trace():
    tracer = Tracer()
    raw = json.dumps({"intent": "record", "confidence": 0.9})
    result = route("今天练了胸", StubLLM(raw_text=raw), tracer)
    assert result["intent"] == "record"
    assert {event["event"] for event in tracer.events} == {
        "input_received",
        "llm_request",
        "llm_response",
        "parse_result",
        "result",
    }


@pytest.mark.parametrize(
    "fault", ["llm_timeout", "llm_api_error", "llm_parse_error"]
)
def test_stub_faults(fault):
    result = route("今天练了胸", StubLLM(fault=fault), Tracer())
    assert result["error_code"] == fault


def test_report_contains_case_trace_and_badcase():
    metadata = {
        "generated_at": "2026-07-19T00:00:00+00:00",
        "view": "discovery",
        "run_mode": "live",
        "model": "test-model",
        "git_commit": "test-commit",
        "prompt_hash": "sha256:prompt",
        "dataset_hash": "sha256:dataset",
        "temperature": 0,
        "max_tokens": 100,
        "thinking": "disabled",
        "timeout_seconds": 30.0,
        "max_retries": 0,
    }
    metrics = {
        "end_to_end_pass_rate": 0.0,
        "macro_f1": 0.0,
        "present_labels_macro_f1": 0.0,
        "parse_errors": 1,
        "high_risk_into_record": 0,
        "per_class": {
            label: {"precision": 0.0, "recall": 0.0, "f1": 0.0}
            for label in ("record", "query", "reject")
        },
        "confusion_matrix": {},
    }
    result = {
        "run_index": 1,
        "case_id": "case-1",
        "input": "今天练了胸",
        "expected": "record",
        "actual": "llm_parse_error",
        "pass": False,
        "trace_id": "t-test",
    }

    report = render_report(metadata, [metrics], [result])

    assert "case-1" in report
    assert "t-test" in report
    assert "git_commit: test-commit" in report
    assert "temperature: 0" in report
    assert "## Badcases" in report
