"""iter-5 链路（planner -> tool -> generator）的确定性单测。

重点不是「跑通」，而是**每条新断言都能被证伪**：
陷阱 Case 让 Stub 故意演「理解错」，验证输出与过程断言的检出能力。
"""

import json

import pytest

from backend.action_lib import query_action_lib
from backend.llm import StubLLM
from backend.plan import (
    call_action_tool,
    parse_need,
    parse_workout,
    run_workout_plan,
)
from backend.trace import Tracer
from eval.run_plan_eval import (
    assert_blackbox,
    assert_whitebox,
    load_cases,
    stub_need,
    stub_workout,
)
from eval.trace_contract import CONTRACTS, assert_trace_contract
from eval.trace_view import render


NEED = {"muscle": "下肢", "level": "新手", "difficulty": "简单", "duration_min": 30}


def need_json(**overrides):
    return json.dumps({**NEED, **overrides}, ensure_ascii=False)


def workout_json(total_min=30, plan=None, note="缺少计时依据，无法估计或保证目标时长。"):
    if plan is None:
        plan = [{"name": "深蹲", "sets": 3, "reps": 12, "rest_sec": 60}]
    return json.dumps({"target_duration_min": total_min, "estimated_duration_min": None,
                       "plan": plan, "note": note}, ensure_ascii=False)


@pytest.mark.parametrize("field,value", [
    ("target_duration_min", True), ("target_duration_min", 4),
    ("target_duration_min", 301), ("estimated_duration_min", 40),
    ("estimated_duration_min", 0), ("estimated_duration_min", "null"),
    ("note", " "), ("note", None),
])
def test_duration_protocol_rejects_invalid_values(field, value):
    payload = json.loads(workout_json())
    payload[field] = value
    with pytest.raises(ValueError):
        parse_workout(json.dumps(payload))


def test_historical_duration_claim_is_rejected():
    historical = {"total_min": 40, "plan": [
        {"name": "单臂哑铃划船", "sets": 4, "reps": 8, "rest_sec": 60}],
        "note": "已补足40分钟"}
    with pytest.raises(ValueError):
        parse_workout(json.dumps(historical))


def test_duration_assertions_detect_independent_mutations():
    import copy
    case = next(c for c in load_cases("all") if c["case_id"] == "pl-004")
    result = {"workout": json.loads(stub_workout(case))}
    assert all(c["pass"] for c in assert_blackbox(case, result))
    for field, value, check in [
        ("target_duration_min", 30, "target_duration_preserved"),
        ("estimated_duration_min", 40, "duration_estimate_unknown"),
        ("note", "", "duration_limitation_present"),
    ]:
        changed = copy.deepcopy(result)
        changed["workout"][field] = value
        assert [c["name"] for c in assert_blackbox(case, changed) if not c["pass"]] == [check]


@pytest.mark.parametrize("target", [5, 20, 40, 60, 300])
def test_target_is_preserved_without_inventing_estimate(tmp_path, target):
    case = dict(next(c for c in load_cases("all") if c["case_id"] == "pl-004"))
    case["expected"] = {**case["expected"], "duration_min": target}
    result, tracer = run_chain(tmp_path, case)
    assert result["ok"]
    assert result["workout"]["target_duration_min"] == target
    assert result["workout"]["estimated_duration_min"] is None
    assert result["workout"]["plan"][0]["sets"] == 4
    event = next(e for e in tracer.events if e["event"] == "generate_request")
    assert "单臂哑铃划船" in event["payload"]["input"]


def test_generator_rejects_changed_target(tmp_path):
    tracer = Tracer(tmp_path / "trace.jsonl")
    result = run_workout_plan("半小时", StubLLM(raw_texts=[need_json(), workout_json(total_min=40)]), tracer, "duration-test")
    assert result["ok"] is False
    assert result["stage"] == "generator"
    assert result["error_code"] == "llm_parse_error"


def test_empty_plan_keeps_target_but_has_no_estimate(tmp_path):
    case = next(c for c in load_cases("all") if c["case_id"] == "pl-005")
    result, _ = run_chain(tmp_path, case)
    assert result["ok"]
    assert result["workout"]["plan"] == []
    assert result["workout"]["target_duration_min"] == 40
    assert result["workout"]["estimated_duration_min"] is None
    assert result["workout"]["note"].strip()


def run_chain(tmp_path, case, trace_id="t-1"):
    tracer = Tracer(tmp_path / "trace.jsonl")
    llm = StubLLM(raw_texts=[stub_need(case), stub_workout(case)])
    return run_workout_plan(case["input"], llm, tracer, trace_id), tracer


# ---------- parse_need：只判「表达错」 ----------


def test_parse_need_accepts_valid_fields():
    assert parse_need(need_json()) == NEED


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"muscle": "下肢", "level": "新手", "difficulty": "简单"}),
        json.dumps({**NEED, "extra": 1}, ensure_ascii=False),
        need_json(muscle="腿"),
        need_json(level="高手"),
        need_json(difficulty="地狱"),
        need_json(duration_min=0),
        need_json(duration_min=301),
        need_json(duration_min=True),
        need_json(duration_min="30"),
    ],
)
def test_parse_need_rejects_expression_errors(raw):
    with pytest.raises(ValueError):
        parse_need(raw)


def test_parse_need_lets_difficulty_level_mismatch_through():
    """难度与水平不匹配是「理解错」，必须在工具入参那一层被抓，不能在这里拦。"""
    need = parse_need(need_json(level="新手", difficulty="中高"))
    assert need["difficulty"] == "中高"


# ---------- parse_workout ----------


def test_parse_workout_accepts_empty_plan():
    assert parse_workout(workout_json(plan=[]))["plan"] == []


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"total_min": 30, "plan": []}),
        json.dumps({"total_min": 0, "plan": [], "note": ""}),
        json.dumps({"total_min": 30, "plan": [], "note": 1}),
        json.dumps({"total_min": 30, "plan": "x", "note": ""}),
        workout_json(plan=[{"name": "深蹲", "sets": 3, "reps": 12}]),
        workout_json(plan=[{"name": "", "sets": 3, "reps": 12, "rest_sec": 60}]),
        workout_json(plan=[{"name": "深蹲", "sets": 0, "reps": 12, "rest_sec": 60}]),
        workout_json(plan=[{"name": "深蹲", "sets": True, "reps": 12, "rest_sec": 60}]),
    ],
)
def test_parse_workout_rejects_expression_errors(raw):
    with pytest.raises(ValueError):
        parse_workout(raw)


# ---------- 工具节点：入参是一等断言对象 ----------


def test_tool_call_records_arguments_even_when_query_is_broken(tmp_path):
    """注入坏工具，证明入参照样进 trace —— 白盒看的是入参，不是工具实现。"""
    tracer = Tracer(tmp_path / "trace.jsonl")

    def broken_query(muscle, difficulty):
        return {"count": 0, "actions": [], "reason": "no_match"}

    out = call_action_tool(NEED, tracer, "t-1", query=broken_query)
    events = [e["event"] for e in tracer.events]
    assert events == ["tool_call", "tool_result"]
    assert out["arguments"] == {"muscle": "下肢", "difficulty": "简单"}
    assert tracer.events[0]["payload"]["arguments"]["muscle"] == "下肢"
    assert tracer.events[1]["payload"]["count"] == 0


def test_tool_call_reason_distinguishes_illegal_label_from_empty_match():
    assert query_action_lib("腿", "简单")["reason"] == "unknown_label"
    assert query_action_lib("肩", "中高")["reason"] == "no_match"


# ---------- trace 契约：声明式断言必须能被证伪 ----------


def test_contract_passes_on_a_full_chain(tmp_path):
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    outcome = assert_trace_contract(tracer.events, "workout_plan")
    assert all(c["pass"] for c in outcome["checks"])


def test_contract_ignores_extra_events(tmp_path):
    """契约是子序列：链路多记事件不算违约。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    noisy = [*tracer.events, {"trace_id": "t-1", "node": "tool", "event": "extra", "payload": {}}]
    assert all(c["pass"] for c in assert_trace_contract(noisy, "workout_plan")["checks"])


def test_contract_goes_red_when_tool_call_is_missing(tmp_path):
    """反证：抽掉工具入参事件，序列断言必须失败并指出缺的是哪一步。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    stripped = [
        e for e in tracer.events
        if not (e["node"] == "tool" and e["event"] == "tool_call")
    ]
    outcome = assert_trace_contract(stripped, "workout_plan")
    checks = {c["name"]: c["pass"] for c in outcome["checks"]}
    assert checks["trace_contract_sequence"] is False
    assert outcome["missing_steps"] == ["tool/tool_call"]
    assert outcome["first_missing_step"] == "tool/tool_call"


def test_contract_goes_red_when_a_required_field_is_missing(tmp_path):
    """反证：字段级断言同样可被证伪。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    stripped = []
    for event in tracer.events:
        if event["node"] == "planner" and event["event"] == "plan_request":
            payload = {k: v for k, v in event["payload"].items() if k != "prompt_hash"}
            event = {**event, "payload": payload}
        stripped.append(event)
    outcome = assert_trace_contract(stripped, "workout_plan")
    checks = {c["name"]: c["pass"] for c in outcome["checks"]}
    assert checks["trace_contract_sequence"] is True
    assert checks["trace_contract_fields"] is False
    assert outcome["missing_fields"] == ["planner/plan_request.prompt_hash"]


def test_contract_rejects_events_from_two_traces(tmp_path):
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    mixed = [*tracer.events, {"trace_id": "other", "node": "planner", "event": "request", "payload": {}}]
    checks = {c["name"]: c["pass"] for c in assert_trace_contract(mixed, "workout_plan")["checks"]}
    assert checks["single_trace_id"] is False


def test_every_declared_contract_step_is_emitted_by_the_chain(tmp_path):
    """契约不是手抄的：链路必须真的走过声明里的每一步。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    _, tracer = run_chain(tmp_path, case)
    emitted = [(e["node"], e["event"]) for e in tracer.events]
    for step in CONTRACTS["workout_plan"]:
        assert (step["node"], step["event"]) in emitted


# ---------- 故障注入：检查输出与过程的检出差异 ----------


def test_wrong_muscle_detected_in_output_and_tool_args(tmp_path):
    """pl-201：练腿生成练胸，输出肌群和工具入参均能检出。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-201"][0]
    result, _ = run_chain(tmp_path, case)
    assert result["ok"]

    blackbox = assert_blackbox(case, result)
    assert [c["name"] for c in blackbox if not c["pass"]] == ["plan_muscle_matches_intent"]

    whitebox = assert_whitebox(case, result)
    failed = [c["name"] for c in whitebox if not c["pass"]]
    assert "tool_args_match_intent" in failed
    assert result["tool_call"]["arguments"]["muscle"] == "胸"


def test_trap_difficulty_case_is_a_blackbox_blind_spot(tmp_path):
    """pl-202：难度听错，当前输出断言未检出，过程断言检出。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-202"][0]
    result, _ = run_chain(tmp_path, case)
    assert all(c["pass"] for c in assert_blackbox(case, result))
    failed = [c["name"] for c in assert_whitebox(case, result) if not c["pass"]]
    assert "tool_args_match_intent" in failed
    assert "need_difficulty_matches_level" in failed


def test_honest_case_passes_both_sides(tmp_path):
    """对照组：不演戏的 case，黑盒白盒都得绿，否则白盒断言是「永远红」。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    result, _ = run_chain(tmp_path, case)
    assert all(c["pass"] for c in assert_blackbox(case, result))
    assert all(c["pass"] for c in assert_whitebox(case, result))


def test_empty_observation_yields_empty_plan(tmp_path):
    """pl-005 打在「肩/中高」空格子上：查不到就得给空计划，不能编。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-005"][0]
    result, _ = run_chain(tmp_path, case)
    assert result["observation"]["count"] == 0
    assert result["workout"]["plan"] == []
    assert all(c["pass"] for c in assert_whitebox(case, result))


def test_duplicated_plan_items_detected_by_uniqueness_and_size(tmp_path):
    """工具只给 1 条动作，生成器抄成 4 行，去重和数量断言均应失败。

    这是真实模型在 pl-004 上跑出来的形状（背/中高只有 1 条动作）。动作名条条来自
    观察，`plan_actions_from_observation` 抓不到；加量该加在 sets 上，不是复制条目。
    """
    case = [c for c in load_cases("all") if c["case_id"] == "pl-004"][0]
    repeated = [{"name": "单臂哑铃划船", "sets": 3, "reps": 12, "rest_sec": 60}] * 4
    tracer = Tracer(tmp_path / "trace.jsonl")
    llm = StubLLM(raw_texts=[
        need_json(muscle="背", level="进阶", difficulty="中高", duration_min=40),
        workout_json(total_min=40, plan=repeated),
    ])
    result = run_workout_plan(case["input"], llm, tracer, "t-1")
    assert result["observation"]["count"] == 1

    assert [c["name"] for c in assert_blackbox(case, result) if not c["pass"]] == ["plan_actions_unique"]
    failed = [c["name"] for c in assert_whitebox(case, result) if not c["pass"]]
    assert failed == ["plan_size_within_observation"]


def test_plan_size_assertion_stays_green_within_observation(tmp_path):
    """对照组：正常条数下这条断言不能是「永远红」。"""
    case = [c for c in load_cases("all") if c["case_id"] == "pl-001"][0]
    result, _ = run_chain(tmp_path, case)
    assert all(c["pass"] for c in assert_whitebox(case, result))


# ---------- 链路还原：白盒结论要人读得出来 ----------


def test_trace_view_renders_tool_arguments_and_first_error(tmp_path):
    case = [c for c in load_cases("all") if c["case_id"] == "pl-201"][0]
    result, tracer = run_chain(tmp_path, case)
    text = render(result["trace_id"], tracer.events)
    assert "* [     tool] tool_call" in text
    assert "muscle=胸 difficulty=简单" in text
    assert "执行异常步骤" in text
    assert "评测结果：未关联" in text


def test_duplicate_within_observation_count_is_rejected(tmp_path):
    case = load_cases("all")[0]
    result, _ = run_chain(tmp_path, case)
    result["observation"]["count"] = 2
    result["workout"]["plan"] *= 2
    checks = {c["name"]: c["pass"] for c in assert_blackbox(case, result)}
    assert checks["plan_actions_unique"] is False
    assert checks["plan_muscle_matches_intent"] is True


@pytest.mark.parametrize("trace_id", [None, "", "wrong"])
def test_contract_requires_expected_nonempty_trace_id(tmp_path, trace_id):
    _, tracer = run_chain(tmp_path, load_cases("all")[0])
    for event in tracer.events:
        event["trace_id"] = trace_id
    checks = assert_trace_contract(tracer.events, "workout_plan", "t-1")["checks"]
    assert checks[0]["pass"] is False


@pytest.mark.parametrize("arguments", [None, {}, {"muscle": "", "difficulty": "简单"}, "invalid"])
def test_contract_rejects_invalid_arguments(tmp_path, arguments):
    _, tracer = run_chain(tmp_path, load_cases("all")[0])
    for event in tracer.events:
        if event["event"] == "tool_call":
            event["payload"]["arguments"] = arguments
    result = assert_trace_contract(tracer.events, "workout_plan", "t-1")
    assert result["invalid_fields"] == ["tool/tool_call.arguments"]
    assert result["checks"][2]["pass"] is False


def test_injection_is_separate_and_visible_in_trace(tmp_path, monkeypatch):
    import eval.run_plan_eval as runner
    monkeypatch.setattr(runner, "TRACE_PATH", tmp_path / "trace.jsonl")
    case = next(c for c in load_cases("all") if c["case_id"] == "pl-201")
    result = runner.run_case(case, "stub", None)
    assert result["verdict"] == "FAIL"
    assert result["detector_pass"] is True
    assert runner.evaluation_passes([result])
    metrics = runner.calculate_metrics([result])
    assert metrics["total"] == 0
    assert metrics["fault_injection_passed"] == 1
    from eval.trace_view import load_events
    text = render(result["trace_id"], load_events(runner.TRACE_PATH))
    assert "评测结果：FAIL" in text
    assert "tool/tool_call" in text
    result["detector_pass"] = False
    assert not runner.evaluation_passes([result])


def test_source_evidence_includes_untracked_code(tmp_path):
    import subprocess
    from eval.run_plan_eval import source_evidence
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "backend").mkdir()
    source = tmp_path / "backend" / "new.py"
    source.write_text("x = 1")
    first = source_evidence(tmp_path)
    source.write_text("x = 2")
    second = source_evidence(tmp_path)
    assert first["working_tree_dirty"]
    assert first["files"]["backend/new.py"] == "x = 1"
    assert first["source_hash"] != second["source_hash"]
