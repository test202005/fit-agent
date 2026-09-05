import json

import pytest

from backend.llm import StubLLM
from backend.router import parse_router_output, route
from backend.trace import Tracer
from eval.run_intent_eval import (
    assert_contract,
    assert_trace_consistency,
    decide_verdict,
    exit_code_for_results,
    expected_trace_events,
    render_report,
)


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["PASS"], 0),
        (["PASS", "REVIEW"], 0),
        (["FAIL"], 1),
        (["ERROR"], 1),
        (["PASS", "REVIEW", "FAIL"], 1),
    ],
)
def test_exit_code_follows_four_state_contract(verdicts, expected):
    results = [{"verdict": verdict} for verdict in verdicts]
    assert exit_code_for_results(results) == expected


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
        '{"intent":"record","confidence":"1.1"}',
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
    request = next(event for event in tracer.events if event["event"] == "llm_request")
    assert request["payload"]["prompt_name"] == "intent_router"
    assert request["payload"]["prompt_version"] == "v1"


def test_numeric_string_confidence_is_visible_in_trace():
    tracer = Tracer()
    result = route(
        "今天练了胸",
        StubLLM(raw_text='{"intent":"record","confidence":"0.9"}'),
        tracer,
    )
    parse_event = next(event for event in tracer.events if event["event"] == "parse_result")
    assert result["ok"] is True
    assert parse_event["payload"]["confidence_normalized"] is True


@pytest.mark.parametrize(
    "fault", ["llm_timeout", "llm_api_error", "llm_parse_error"]
)
def test_stub_faults(fault):
    result = route("今天练了胸", StubLLM(fault=fault), Tracer())
    assert result["error_code"] == fault


def failed_checks(result):
    return {check["name"] for check in assert_contract(result) if not check["pass"]}


def test_contract_accepts_valid_shapes():
    ok = {
        "ok": True,
        "trace_id": "t-1",
        "intent": "record",
        "confidence": 0.9,
        "source": "llm",
    }
    err = {"ok": False, "trace_id": "t-1", "error_code": "llm_timeout"}
    assert failed_checks(ok) == set()
    assert failed_checks(err) == set()


def test_contract_catches_business_leak_on_failure():
    """失败响应泄漏了 intent —— 第三问：不该出现的不许出现。"""
    leaked = {
        "ok": False,
        "trace_id": "t-1",
        "error_code": "llm_parse_error",
        "intent": "record",
    }
    assert failed_checks(leaked) == {"structure_failure", "no_business_leak"}


def test_contract_catches_undefined_error_code():
    unknown = {"ok": False, "trace_id": "t-1", "error_code": "weird_code"}
    assert failed_checks(unknown) == {"error_code_enum"}


def test_contract_catches_out_of_range_confidence():
    bad = {
        "ok": True,
        "trace_id": "t-1",
        "intent": "record",
        "confidence": 1.7,
        "source": "llm",
    }
    assert failed_checks(bad) == {"confidence_range"}


def test_contract_catches_extra_field():
    extra = {
        "ok": True,
        "trace_id": "t-1",
        "intent": "record",
        "confidence": 0.9,
        "source": "llm",
        "debug": "leaked",
    }
    assert failed_checks(extra) == {"structure_success"}


def test_defense_path_rejects_extra_llm_call():
    """防御路径若偷偷调了模型，事件集会多出 llm_request —— 精确比对必须判失败。"""
    result = {"ok": False, "trace_id": "t-1", "error_code": "bad_request"}
    honest = {"input_received", "result"}
    sneaky = {"input_received", "llm_request", "result"}

    assert expected_trace_events(result) == honest
    assert expected_trace_events(result) != sneaky
    # 旧写法用 issubset，多出的事件不会失败——这正是被修掉的漏洞
    assert expected_trace_events(result).issubset(sneaky)


def verdict_for(case, result, **kwargs):
    flags = {
        "assertion_pass": True,
        "contract_pass": True,
        "trace_pass": True,
        "trace_write_failed": False,
    }
    flags.update(kwargs)
    return decide_verdict(case, result, **flags)


def test_unexpected_timeout_is_error_not_fail():
    """分类 Case 撞上超时是环境问题，不该算业务失败。"""
    case = {"case_id": "c1", "expected_intent": "record"}
    result = {"ok": False, "trace_id": "t-1", "error_code": "llm_timeout"}
    assert verdict_for(case, result, assertion_pass=False) == "ERROR"


def test_injected_timeout_is_pass():
    """故障注入 Case 期望超时，拿到超时就是通过。"""
    case = {"case_id": "c2", "expected_error_code": "llm_timeout"}
    result = {"ok": False, "trace_id": "t-1", "error_code": "llm_timeout"}
    assert verdict_for(case, result) == "PASS"


def test_parse_error_is_fail_not_error():
    """解析失败是真实的 prompt/模型问题，必须计入业务失败。"""
    case = {"case_id": "c3", "expected_intent": "record"}
    result = {"ok": False, "trace_id": "t-1", "error_code": "llm_parse_error"}
    assert verdict_for(case, result, assertion_pass=False) == "FAIL"


def test_trace_write_failure_is_error():
    case = {"case_id": "c4", "expected_intent": "record"}
    result = {"ok": True, "trace_id": "t-1", "intent": "record"}
    assert verdict_for(case, result, trace_write_failed=True) == "ERROR"


def test_consistency_catches_trace_result_mismatch():
    """trace 里记的结论与返回值不一致 —— 第四问必须抓到。"""
    result = {
        "ok": True,
        "trace_id": "t-1",
        "intent": "record",
        "confidence": 0.9,
        "source": "llm",
    }
    events = [
        {"event": "parse_result", "payload": {"ok": True, "intent": "record"}},
        {"event": "result", "payload": {"ok": True, "intent": "query"}},
    ]
    failed = {c["name"] for c in assert_trace_consistency(result, events) if not c["pass"]}
    assert failed == {"trace_intent_matches"}


def test_consistency_passes_on_matching_trace():
    result = {
        "ok": True,
        "trace_id": "t-1",
        "intent": "record",
        "confidence": 0.9,
        "source": "llm",
    }
    events = [
        {"event": "parse_result", "payload": {"ok": True, "intent": "record"}},
        {"event": "result", "payload": {"ok": True, "intent": "record"}},
    ]
    assert all(c["pass"] for c in assert_trace_consistency(result, events))


def test_report_contains_case_trace_and_badcase():
    metadata = {
        "generated_at": "2026-07-19T00:00:00+00:00",
        "view": "discovery",
        "run_mode": "live",
        "model": "test-model",
        "git_commit": "test-commit",
        "prompt_name": "intent_router",
        "prompt_version": "v1",
        "prompt_hash": "sha256:prompt",
        "dataset_hash": "sha256:dataset",
        "temperature": 0,
        "max_tokens": 100,
        "thinking": "disabled",
        "timeout_seconds": 30.0,
        "max_retries": 0,
        "runs": 1,
    }
    metrics = {
        "end_to_end_pass_rate": 0.0,
        "verdicts": {"PASS": 0, "FAIL": 1, "REVIEW": 0, "ERROR": 2},
        "case_pass_rate": 0.0,
        "assertion_pass_rate": 0.75,
        "assertion_total": 8,
        "assertion_passed": 6,
        "macro_f1": 0.0,
        "present_labels_macro_f1": 0.0,
        "parse_errors": 1,
        "confidence_normalized_count": 1,
        "high_risk_into_record": 0,
        "per_class": {
            label: {"precision": 0.0, "recall": 0.0, "f1": 0.0}
            for label in ("record", "query", "reject")
        },
        "confusion_matrix": {},
    }
    result = {
        "run_index": 1,
        "model": "test-model",
        "case_id": "case-1",
        "input": "今天练了胸",
        "expected": "record",
        "actual": "llm_parse_error",
        "pass": False,
        "verdict": "FAIL",
        "failed_checks": ["structure_failure"],
        "trace_id": "t-test",
        "usages": [{"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}],
    }

    report = render_report(
        metadata,
        [{"model": "test-model", "run_index": 1, "metrics": metrics}],
        [result],
    )

    assert "case-1" in report
    assert "t-test" in report
    assert "git_commit: test-commit" in report
    assert "temperature: 0" in report
    assert "confidence_normalized_count: 1" in report
    assert "## Badcases" in report
    # 双口径与四态必须都出现在报告里
    assert "Case 级" in report
    assert "断言级" in report
    assert "ERROR 2" in report
    assert "structure_failure" in report
    # 多轮口径与成本也必须落到报告里
    assert "pass@k" in report
    assert "test-model · Run 1" in report
    assert "40" in report
