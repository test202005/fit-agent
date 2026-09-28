import json

import pytest

from backend import action_lib_v2 as lib
from backend.assistant import run_assistant
from backend.clock import FrozenClock
from backend.llm import StubLLM, ToolCall, ToolCallResult
from backend.plan_v2 import NeedError, parse_need, resolve, run_plan_llm_only, run_plan_v2
from backend.routine import FakeRoutineStore
from backend.storage import FakeStorage
from backend.trace import Tracer
from tests.action_draft_parser import DATA_PATH, build


NEED = {"goal": "减脂", "parts": [], "level": "新手", "duration_min": 30, "gym": False,
        "equipment": [], "exclusions": [], "health_flags": []}
PLAN = {"segments": [
    {"segment": "warmup", "format": "straight", "items": [
        {"id": "march_in_place", "sets": 1, "seconds": 60, "rest_sec": 0},
        {"id": "inchworm", "sets": 1, "reps": 5, "rest_sec": 0}]},
    {"segment": "main", "format": "circuit", "rounds": 3, "rest_between_rounds": 120, "items": [
        {"id": "bodyweight_squat", "reps": 12, "rest_sec": 30},
        {"id": "knee_push_up", "reps": 10, "rest_sec": 30},
        {"id": "glute_bridge", "reps": 12, "rest_sec": 30},
        {"id": "mountain_climber", "seconds": 30, "rest_sec": 30},
        {"id": "jumping_jack", "seconds": 30, "rest_sec": 30},
        {"id": "plank", "seconds": 30, "rest_sec": 30}]},
    {"segment": "cooldown", "format": "straight", "items": [
        {"id": "quad_stretch", "sets": 1, "seconds": 30, "rest_sec": 0},
        {"id": "hamstring_stretch", "sets": 1, "seconds": 30, "rest_sec": 0},
        {"id": "childs_pose", "sets": 1, "seconds": 45, "rest_sec": 0}]},
], "note": "循环三轮"}


def with_items(segment_index, items):
    plan = json.loads(json.dumps(PLAN))
    plan["segments"][segment_index]["items"] = items
    return plan


def rules(plan, need=NEED, available=frozenset()):
    return [v["rule"] for v in lib.validate_plan(plan, need, set(available))["violations"]]


# ---------- 动作库（V10.1） ----------


def test_library_data_matches_draft():
    # 草稿是唯一内容源：数据文件必须与草稿表格逐项一致
    assert json.loads(DATA_PATH.read_text(encoding="utf-8")) == build()


def test_library_invariants():
    actions = lib.load_actions()
    assert len(actions) == 82
    assert sum(a["unilateral"] for a in actions.values()) == 13
    assert sum(a["v7"] for a in actions.values()) == 12
    rank = {"徒手": 0, "家用": 1, "健身房": 2}
    for action in actions.values():
        if lib.tier(action) == "徒手":
            assert action["substitute"] is None
        elif action["substitute"] is not None:
            assert rank[lib.tier(actions[action["substitute"]])] < rank[lib.tier(action)], action["id"]


# ---------- 时长（D1 公式） ----------


def test_plan_seconds_formula():
    # 热身 60 + 5×6；循环 3 × (36+20+36+30+30+30 + 6×30) + 2×120；
    # 放松 股四头肌单侧 30×2 + 30 + 45；6 块切换 5×15
    assert lib.plan_seconds(PLAN) == 90 + (3 * 362 + 240) + 135 + 75


def test_unilateral_reps_and_gym_setup():
    plan = {"segments": [{"segment": "main", "format": "straight", "items": [
        {"id": "single_leg_glute_bridge", "sets": 2, "reps": 10, "rest_sec": 60},
        {"id": "leg_press", "sets": 2, "reps": 10, "rest_sec": 60}]}]}
    # 单腿臀桥 2×(10×3×2)+60；腿举 2×30+60；切换 15；健身房准备 30
    assert lib.plan_seconds(plan) == (120 + 60) + (60 + 60) + 15 + 30


# ---------- 校验：每条规则配通过与违反 ----------


def test_valid_plan_passes():
    checked = lib.validate_plan(PLAN, NEED, set())
    assert checked["violations"] == [] and checked["computed_min"] == 27.1


def test_r1_unknown_action():
    assert rules(with_items(1, [{"id": "bench_jump_360", "reps": 10, "rest_sec": 30}])) == ["R1"]


def test_r2_equipment_not_available():
    plan = with_items(1, PLAN["segments"][1]["items"][:5] + [{"id": "goblet_squat", "reps": 10, "rest_sec": 30}])
    assert "R2" in rules(plan)
    assert "R2" not in rules(plan, available={"哑铃"})


def test_r3_too_hard_for_beginner():
    plan = with_items(1, PLAN["segments"][1]["items"][:5] + [{"id": "burpee", "reps": 8, "rest_sec": 30}])
    assert "R3" in rules(plan)
    assert "R3" not in rules(plan, need={**NEED, "level": "进阶"})


def test_r4_exclusion_and_part_coverage():
    assert "R4" in rules(PLAN, need={**NEED, "exclusions": ["跳跃"]})
    assert "R4" in rules(PLAN, need={**NEED, "parts": ["背"]})
    assert "R4" not in rules(PLAN, need={**NEED, "parts": ["下肢", "胸"]})


def test_d1_duration_out_of_range():
    assert rules(PLAN, need={**NEED, "duration_min": 45}) == ["D1"]


def test_d2_beginner_caps():
    too_many = with_items(0, [{"id": "march_in_place", "sets": 4, "seconds": 60, "rest_sec": 0}])
    assert "D2" in rules(too_many)
    reps = with_items(1, [{"id": "bodyweight_squat", "reps": 20, "rest_sec": 30}] + PLAN["segments"][1]["items"][1:])
    assert "D2" in rules(reps)
    assert "D2" not in rules(reps, need={**NEED, "level": "进阶"})


def test_d4_structure():
    assert rules({"segments": []}) == ["D4"]
    assert rules({"segments": [PLAN["segments"][0]]}) == ["D4"]
    assert rules({"segments": [{"segment": "stretch", "items": []}]})[0] == "D4"


def test_enrich_fills_attributes_from_library():
    segments = lib.enrich(PLAN)
    assert [s["label"] for s in segments] == ["热身", "训练", "拉伸"]
    item = segments[1]["items"][0]
    assert item["name"] == "徒手深蹲" and item["parts"] == ["下肢", "臀"] and item["equipment"] == []


# ---------- 需求解析 ----------


def test_parse_need_rejects_out_of_vocabulary():
    with pytest.raises(NeedError):
        parse_need(json.dumps({**NEED, "parts": ["腿"]}, ensure_ascii=False))
    with pytest.raises(NeedError):
        parse_need(json.dumps({**NEED, "equipment": ["杠铃"]}, ensure_ascii=False))


def test_resolve_defaults_but_never_equipment():
    need = resolve({**NEED, "level": "未说明", "duration_min": None, "equipment": None})
    assert need["level"] == "新手" and need["duration_min"] == 30 and need["equipment"] is None
    assert len(need["defaults"]) == 2


# ---------- 链路 ----------


def chain(need, *composes):
    llm = StubLLM(raw_texts=[json.dumps(need, ensure_ascii=False), *[json.dumps(c, ensure_ascii=False) for c in composes]])
    tracer = Tracer()
    return run_plan_v2("三十分钟减脂，新手，没器械", llm, tracer, "t-1"), tracer


def events(tracer, name):
    return [e["payload"] for e in tracer.events if e["event"] == name]


def test_chain_success():
    result, tracer = chain(NEED, PLAN)
    assert result["ok"] and result["computed_duration_min"] == 27.1 and result["attempts"] == 1
    assert result["segments"][1]["items"][0]["name"] == "徒手深蹲"
    assert events(tracer, "candidates")[0]["available_equipment"] == []


def test_chain_health_referral_skips_compose():
    result, tracer = chain({**NEED, "health_flags": ["膝盖疼"]})
    assert result["error_code"] == "health_referral" and not events(tracer, "compose_response")


def test_chain_missing_equipment_asks():
    result, tracer = chain({**NEED, "equipment": None})
    assert result["error_code"] == "need_clarification" and not events(tracer, "compose_response")


def test_chain_regenerates_once_with_specific_feedback():
    bad = with_items(1, [{"id": "burpee", "reps": 8, "rest_sec": 30}] + PLAN["segments"][1]["items"][1:])
    result, tracer = chain(NEED, bad, PLAN)
    assert result["ok"] and result["attempts"] == 2
    first = events(tracer, "compose_checked")[0]
    assert any(v["rule"] == "R3" and "波比跳" in v["detail"] for v in first["violations"])


def test_chain_gives_up_after_second_failure():
    bad = with_items(1, [{"id": "burpee", "reps": 8, "rest_sec": 30}])
    result, _ = chain(NEED, bad, bad)
    assert result["ok"] is False and result["error_code"] == "plan_invalid"
    assert {v["rule"] for v in result["violations"]} >= {"R3"}


def test_llm_only_maps_names_and_keeps_unknown():
    free = {"segments": [{"segment": "main", "items": [
        {"name": "徒手深蹲", "sets": 3, "reps": 12, "seconds": None, "rest_sec": 60},
        {"name": "药球砸地", "sets": 3, "reps": 10, "seconds": None, "rest_sec": 60}]}], "duration_min": 30}
    result = run_plan_llm_only("x", StubLLM(raw_text=json.dumps(free, ensure_ascii=False)), Tracer(), "t-1")
    assert [i["id"] for i in result["segments"][0]["items"]] == ["bodyweight_squat", None]


# ---------- 接入统一助手 ----------


def test_assistant_dispatches_to_v2_engine():
    rounds = [ToolCallResult(tool_calls=[ToolCall(name="generate_workout_plan",
                                                  arguments={"reason": "要计划", "request": "三十分钟减脂"}, id="p")])]
    plan_llm = StubLLM(raw_texts=[json.dumps(NEED, ensure_ascii=False), json.dumps(PLAN, ensure_ascii=False)])
    result = run_assistant("x", StubLLM(tool_rounds=rounds, text="好"), plan_llm, FakeStorage(), FakeRoutineStore(),
                           Tracer(), FrozenClock("2026-09-25T20:00:00+08:00"), "t-1", "u", plan_engine="v2")
    output = result["attempts"][0]["output"]
    assert output["engine"] == "v2" and output["computed_duration_min"] == 27.1


def test_assistant_rejects_unknown_engine():
    with pytest.raises(ValueError):
        run_assistant("x", StubLLM(), StubLLM(), FakeStorage(), FakeRoutineStore(), Tracer(),
                      FrozenClock("2026-09-25T20:00:00+08:00"), "t-1", "u", plan_engine="v9")


# ---------- 对照评测的判定器（每类结论配反例）----------

from eval import run_plan_v2_eval as v2eval  # noqa: E402

CASE = {"case_id": "c", "input": "x", "expect": "plan", "category": "减脂-徒手",
        "truth": {"level": "新手", "duration_min": 30, "available": [], "parts": [], "exclusions": []}}


def v2_ok(plan=PLAN):
    return {"ok": True, "engine": "v2", "segments": lib.enrich(plan), "attempts": 1}


def test_judge_v2_pass_and_guidance_review():
    assert v2eval.judge(CASE, "v2", v2_ok())["verdict"] == "PASS"
    # 热身里放静态拉伸（与原地踏步同为 60 秒，时长不变）：只违反指引 G2，计 REVIEW 不判 FAIL
    stretch_warmup = with_items(0, [{"id": "quad_stretch", "sets": 1, "seconds": 30, "rest_sec": 0},
                                    PLAN["segments"][0]["items"][1]])
    judged = v2eval.judge(CASE, "v2", v2_ok(stretch_warmup))
    assert judged["verdict"] == "REVIEW" and judged["misses"] == ["G2_no_static_stretch_in_warmup"]
    assert "G1_three_segments" in v2eval.guidance(CASE, {"segments": PLAN["segments"][1:]})


def test_judge_uses_truth_not_model_parse():
    # 模型以为用户有哑铃并用了高脚杯深蹲：按 truth（徒手）判 R2
    plan = with_items(1, PLAN["segments"][1]["items"][:5] + [{"id": "goblet_squat", "reps": 10, "rest_sec": 30}])
    assert "R2" in [v["rule"] for v in v2eval.judge(CASE, "v2", v2_ok(plan))["violations"]]


def test_judge_llm_only_unknown_action_is_r1():
    free = {"ok": True, "engine": "llm_only", "segments": [{"segment": "main", "items": [
        {"name": "药球砸地", "id": None, "sets": 3, "reps": 10, "rest_sec": 60}]}]}
    judged = v2eval.judge(CASE, "llm_only", free)
    assert judged["verdict"] == "FAIL" and judged["violations"][0]["rule"] == "R1"


def test_judge_v7_output_is_normalized():
    v7 = {"ok": True, "workout": {"plan": [{"name": "平板支撑", "sets": 3, "reps": 12, "rest_sec": 60}]}}
    judged = v2eval.judge(CASE, "v7", v7)
    assert judged["delivered"] and "D1" in [v["rule"] for v in judged["violations"]]


def test_judge_refer_and_clarify():
    refer = {**CASE, "expect": "refer"}
    assert v2eval.judge(refer, "v2", {"ok": False, "error_code": "health_referral"})["verdict"] == "PASS"
    assert "R5" in [v["rule"] for v in v2eval.judge(refer, "v2", v2_ok())["violations"]]
    clarify = {**CASE, "expect": "clarify", "truth": {**CASE["truth"], "available": None}}
    assert v2eval.judge(clarify, "v2", {"ok": False, "error_code": "need_clarification"})["verdict"] == "PASS"
    gear = with_items(1, PLAN["segments"][1]["items"][:5] + [{"id": "goblet_squat", "reps": 10, "rest_sec": 30}])
    assert "R6" in [v["rule"] for v in v2eval.judge(clarify, "v2", v2_ok(gear))["violations"]]
    # 没说器械但只给徒手计划：允许
    assert "R6" not in [v["rule"] for v in v2eval.judge(clarify, "v2", v2_ok())["violations"]]


def test_judge_not_delivered_is_fail_and_llm_error_is_error():
    assert v2eval.judge(CASE, "v2", {"ok": False, "error_code": "plan_invalid"})["verdict"] == "FAIL"
    assert v2eval.judge(CASE, "v2", {"ok": False, "error_code": "llm_timeout"})["verdict"] == "ERROR"


# ---------- 时长微调（C7）----------


def short_plan():
    plan = json.loads(json.dumps(PLAN))
    plan["segments"][1]["rest_between_rounds"] = 0
    plan["segments"][1]["rounds"] = 2
    return plan


def test_fit_duration_only_changes_numbers_within_limits():
    plan = short_plan()
    assert rules(plan) == ["D1"]
    fit = lib.fit_duration(plan, NEED)
    assert fit is not None and rules(fit["plan"]) == []
    ids = lambda p: [i["id"] for s in p["segments"] for i in s["items"]]  # noqa: E731
    assert ids(fit["plan"]) == ids(plan)
    assert {c["field"] for c in fit["changes"]} <= {"rounds", "rest_between_rounds", "rest_sec", "sets"}


def test_fit_duration_gives_up_when_unreachable():
    assert lib.fit_duration(short_plan(), {**NEED, "duration_min": 120}) is None


def test_chain_adjusts_duration_without_regenerating():
    result, tracer = chain(NEED, short_plan())
    assert result["ok"] and result["attempts"] == 1 and result["duration_adjusted"]
    assert events(tracer, "compose_checked")[0]["adjusted"]["from_min"] < 25.5


def test_compose_can_use_separate_llm():
    parse_llm = StubLLM(raw_text=json.dumps(NEED, ensure_ascii=False))
    compose_llm = StubLLM(raw_text=json.dumps(PLAN, ensure_ascii=False))
    compose_llm.temperature = 0.7
    tracer = Tracer()
    result = run_plan_v2("x", parse_llm, tracer, "t-1", compose_llm=compose_llm)
    assert result["ok"]
    assert events(tracer, "compose_response")[0]["temperature"] == 0.7

