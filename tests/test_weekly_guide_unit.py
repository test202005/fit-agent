from datetime import datetime

from backend.action_lib_v2 import infer_parts, match_actions
from backend.assistant import _action_guide, run_assistant
from backend.clock import FrozenClock
from backend.llm import StubLLM, ToolCall, ToolCallResult
from backend.routine import FakeRoutineStore
from backend.storage import FakeStorage
from backend.trace import Tracer
from backend.weekly import summarize, week_range


FRIDAY = datetime.fromisoformat("2026-09-25T20:00:00+08:00")


def seed(storage, when, **record):
    base = {"exercise": None, "weight_kg": None, "sets": None, "reps": None,
            "duration_min": None, "distance_km": None, "state": "complete"}
    storage.append([{**base, **record}], "t-seed", datetime.fromisoformat(when), "u")


# ---------- 周范围 ----------


def test_week_is_monday_to_sunday():
    assert [d.isoformat() for d in week_range(FRIDAY, 0)] == ["2026-09-21", "2026-09-27"]
    assert [d.isoformat() for d in week_range(FRIDAY, -1)] == ["2026-09-14", "2026-09-20"]
    monday = datetime.fromisoformat("2026-09-21T00:10:00+08:00")
    sunday = datetime.fromisoformat("2026-09-27T23:50:00+08:00")
    assert week_range(monday, 0) == week_range(sunday, 0) == week_range(FRIDAY, 0)


# ---------- 统计口径 ----------


def weekly_storage():
    s = FakeStorage()
    seed(s, "2026-09-21T08:00:00+08:00", exercise="深蹲", reps=20)                  # 周一 力量
    seed(s, "2026-09-21T19:00:00+08:00", exercise="跑步", duration_min=30)          # 周一 有时长
    seed(s, "2026-09-24T19:00:00+08:00", exercise="俯卧撑", sets=3, reps=12)        # 周四 力量
    seed(s, "2026-09-24T20:00:00+08:00", exercise="游泳", duration_min=20.5)        # 周四 未识别部位
    seed(s, "2026-09-25T07:00:00+08:00", exercise="胸", state="incomplete")         # 不完整
    seed(s, "2026-09-20T09:00:00+08:00", exercise="卧推", sets=5, reps=8)           # 上周日
    return s


def test_summary_counts_are_computed_by_code():
    out = summarize(weekly_storage(), "u", FRIDAY, 0)
    assert (out["training_days"], out["record_count"], out["incomplete_count"]) == (2, 4, 1)
    assert out["strength_days"] == 2 and out["timed_minutes"] == 50.5
    assert out["guideline"]["strength_days_short"] == 0
    assert out["guideline"]["timed_minutes_short"] == 99.5
    assert out["parts"]["下肢"] == 2 and out["parts"]["胸"] == 1  # 深蹲、跑步都推到下肢；俯卧撑→标准俯卧撑含胸
    assert out["unrecognized_exercises"] == ["游泳"]


def test_last_week_and_user_isolation():
    storage = weekly_storage()
    last = summarize(storage, "u", FRIDAY, -1)
    assert last["record_count"] == 1 and last["records"][0]["exercise"] == "卧推"
    assert summarize(storage, "other", FRIDAY, 0)["record_count"] == 0


def test_invalid_offset_rejected():
    import pytest
    with pytest.raises(ValueError):
        summarize(FakeStorage(), "u", FRIDAY, -2)


# ---------- 动作匹配 ----------


def test_alias_beats_contains_match():
    # 只按包含匹配，「俯卧撑」会连派克俯卧撑一起匹配，共同部位只剩手臂
    assert [a["id"] for a in match_actions("俯卧撑")] == ["push_up"]
    assert "胸" in infer_parts("俯卧撑")
    assert infer_parts("游泳") is None


def test_guide_single_multiple_none():
    single = _action_guide({"name": "平板支撑"})
    assert single["match"] == "single" and single["action"]["cue"].startswith("不要塌腰")
    with_sub = _action_guide({"name": "高脚杯深蹲"})
    assert with_sub["action"]["substitute"] == "徒手深蹲" and with_sub["action"]["equipment"] == ["哑铃"]
    assert _action_guide({"name": "卧推"})["candidates"] == ["哑铃卧推", "杠铃卧推"]
    assert _action_guide({"name": "药球砸地"})["match"] == "none"
    assert _action_guide({"name": " "})["error_code"] == "invalid_args"


# ---------- 接入助手 ----------


def call(name, args):
    return ToolCallResult(tool_calls=[ToolCall(name=name, arguments={"reason": "r", **args}, id=name)])


def run(rounds, storage):
    return run_assistant("x", StubLLM(tool_rounds=rounds, text="好"), StubLLM(), storage, FakeRoutineStore(),
                         Tracer(), FrozenClock("2026-09-25T20:00:00+08:00"), "t-1", "u")


def test_assistant_weekly_and_guide_tools():
    result = run([call("weekly_summary", {"week_offset": 0}), call("get_action_guide", {"name": "深蹲"})],
                 weekly_storage())
    weekly, guide = [a["output"] for a in result["attempts"]]
    assert weekly["training_days"] == 2 and weekly["week"]["start"] == "2026-09-21"
    assert guide["action"]["name"] == "徒手深蹲"
    assert result["writes"] == 0


def test_assistant_weekly_rejects_bad_offset():
    result = run([call("weekly_summary", {"week_offset": 3})], FakeStorage())
    assert result["attempts"][0]["error_code"] == "invalid_args"
