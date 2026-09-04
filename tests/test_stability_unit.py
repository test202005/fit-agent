"""稳定性口径自身的单测。

评测代码也要被验证：`stability.py` 决定报告里 pass@k / pass^k / flaky 三个数字，
一个永远返回「无波动」的坏实现，在温度 0 的全绿数据上和正确实现输出完全相同。
所以每条口径都配一条「它应该失败」的用例——喂进明确有波动的数据，断言它必须抓到。
"""
from __future__ import annotations

import pytest

from eval.stability import (
    aggregate_stability,
    aggregate_usage,
    case_stability,
    collect_usages,
    group_by_case,
    render_stability,
)


def row(case_id: str, verdict: str, model: str = "m1", risk: str = "normal", **extra):
    return {"case_id": case_id, "verdict": verdict, "model": model, "risk": risk, **extra}


# ---------- case_stability：单条 Case 的跨轮画像 ----------


def test_all_pass_is_stable():
    stat = case_stability([row("c1", "PASS"), row("c1", "PASS"), row("c1", "PASS")])
    assert stat["pass_any"] is True
    assert stat["pass_all"] is True
    assert stat["flaky"] is False


def test_all_fail_is_stable_failure():
    """全挂也是稳定的——flaky 说的是「结果不一致」，不是「有失败」。"""
    stat = case_stability([row("c1", "FAIL"), row("c1", "FAIL")])
    assert stat["pass_any"] is False
    assert stat["pass_all"] is False
    assert stat["flaky"] is False


def test_mixed_verdicts_must_be_flagged_flaky():
    """应该失败的用例：混合结果如果没被判 flaky，口径就是坏的。"""
    stat = case_stability([row("c1", "PASS"), row("c1", "FAIL"), row("c1", "PASS")])
    assert stat["flaky"] is True
    assert stat["pass_any"] is True
    assert stat["pass_all"] is False
    assert stat["verdicts"] == ["PASS", "FAIL", "PASS"]


@pytest.mark.parametrize("noise", ["ERROR", "REVIEW"])
def test_error_and_review_rounds_are_excluded_not_counted_as_failure(noise):
    """环境异常与待复核按轮剔除，不能把它们当成业务失败拉低 pass^k。"""
    stat = case_stability([row("c1", "PASS"), row("c1", noise), row("c1", "PASS")])
    assert stat["evaluable_runs"] == 2
    assert stat["pass_all"] is True
    assert stat["flaky"] is False


def test_case_with_no_evaluable_round_is_unevaluated():
    stat = case_stability([row("c1", "ERROR"), row("c1", "REVIEW")])
    assert stat["unevaluated"] is True
    assert stat["pass_all"] is False


# ---------- aggregate_stability：分模型汇总 ----------


def test_pass_at_k_and_power_k_diverge_on_flaky_data():
    """口径的核心主张：flaky 数据上两个数必须分开，合成一个就丢信息。"""
    results = [
        row("stable", "PASS"), row("stable", "PASS"),
        row("flaky", "PASS"), row("flaky", "FAIL"),
    ]
    agg = aggregate_stability(results)["m1"]
    assert agg["pass_at_k"] == 1.0
    assert agg["pass_power_k"] == 0.5
    assert agg["flaky_count"] == 1
    assert agg["flaky_cases"][0]["case_id"] == "flaky"


def test_models_are_aggregated_separately():
    """多模型跑同一套集时按模型分账，混算会让好模型替差模型背锅。"""
    results = [
        row("c1", "PASS", model="good"), row("c1", "PASS", model="good"),
        row("c1", "PASS", model="bad"), row("c1", "FAIL", model="bad"),
    ]
    agg = aggregate_stability(results)
    assert agg["good"]["pass_power_k"] == 1.0
    assert agg["bad"]["pass_power_k"] == 0.0
    assert agg["bad"]["flaky_count"] == 1


def test_unevaluated_case_leaves_the_denominator():
    results = [
        row("ok", "PASS"), row("ok", "PASS"),
        row("noise", "ERROR"), row("noise", "ERROR"),
    ]
    agg = aggregate_stability(results)["m1"]
    assert agg["evaluated_cases"] == 1
    assert agg["pass_power_k"] == 1.0
    assert agg["unevaluated_cases"] == ["noise"]


def test_empty_denominator_does_not_divide_by_zero():
    agg = aggregate_stability([row("c1", "ERROR")])["m1"]
    assert agg["pass_at_k"] == 0.0
    assert agg["pass_power_k"] == 0.0


# ---------- 高风险纪律 ----------


def test_high_risk_case_must_pass_every_round():
    """关键安全行为不接受「三次里过两次」：一次不过就判不稳。"""
    results = [
        row("hr", "PASS", risk="high"), row("hr", "FAIL", risk="high"),
    ]
    agg = aggregate_stability(results)["m1"]
    assert agg["high_risk_stable"] is False
    assert agg["high_risk_unstable"] == ["hr"]


def test_high_risk_all_pass_is_stable():
    results = [row("hr", "PASS", risk="high"), row("hr", "PASS", risk="high")]
    agg = aggregate_stability(results)["m1"]
    assert agg["high_risk_stable"] is True
    assert agg["high_risk_unstable"] == []


def test_normal_risk_flaky_does_not_trip_high_risk_gate():
    """普通 Case 波动不该污染高风险门禁，否则门禁失去指向性。"""
    results = [row("c1", "PASS"), row("c1", "FAIL")]
    agg = aggregate_stability(results)["m1"]
    assert agg["high_risk_stable"] is True
    assert agg["flaky_count"] == 1


# ---------- 成本口径 ----------


def test_usage_sums_only_rows_that_called_the_model():
    """没调模型的行不进 calls 分母，否则平均 token 会被防御路径稀释。"""
    results = [
        row("c1", "PASS", usages=[{"prompt_tokens": 600, "completion_tokens": 30,
                                   "total_tokens": 630}]),
        row("c2", "PASS", usages=[]),
    ]
    usage = aggregate_usage(results)["m1"]
    assert usage["calls"] == 1
    assert usage["total_tokens"] == 630
    assert usage["tokens_per_call"] == 630.0
    # per_case 分母是全部 Case，包含零调用的那条——这是「一次评测多少钱」的口径
    assert usage["tokens_per_case"] == 315.0


def test_multi_call_case_accumulates():
    results = [row("c1", "PASS", usages=[
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220},
    ])]
    usage = aggregate_usage(results)["m1"]
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 330


def test_collect_usages_reads_trace_and_skips_events_without_usage():
    events = [
        {"event": "input_received", "payload": {"text": "hi"}},
        {"event": "llm_request", "payload": {"model": "m"}},
        {"event": "llm_response", "payload": {"usage": {"total_tokens": 630}}},
        {"event": "result", "payload": {"ok": True}},
    ]
    assert collect_usages(events) == [{"total_tokens": 630}]


def test_collect_usages_ignores_null_usage():
    """stub 模式下 usage 是 None，不能被当成一次零成本调用记进去。"""
    events = [{"event": "llm_response", "payload": {"usage": None}}]
    assert collect_usages(events) == []


# ---------- 归组与渲染 ----------


def test_group_by_case_keys_on_model_and_case():
    grouped = group_by_case([row("c1", "PASS", model="a"), row("c1", "PASS", model="b")])
    assert set(grouped) == {("a", "c1"), ("b", "c1")}


def test_render_reports_no_fluctuation_only_when_truly_stable():
    lines = render_stability(
        aggregate_stability([row("c1", "PASS"), row("c1", "PASS")]),
        aggregate_usage([row("c1", "PASS")]),
        runs=2,
    )
    assert any("无波动" in line for line in lines)


def test_render_names_the_flaky_case():
    """报告必须点名到具体 Case，只给一个 flaky 计数等于没有归因线索。"""
    results = [row("c1", "PASS"), row("c1", "FAIL")]
    lines = render_stability(aggregate_stability(results), aggregate_usage(results), runs=2)
    body = "\n".join(lines)
    assert "c1" in body
    assert "PASS → FAIL" in body
    assert "无波动" not in body
