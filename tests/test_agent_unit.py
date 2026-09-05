import pytest

from eval.run_tool_eval import build_trace_id
from backend.agent import MAX_TOOL_CALLS, run_agent
from backend.clock import FrozenClock
from backend.llm import StubLLM, ToolCall
from backend.storage import FakeStorage
from backend.tools import ToolError, make_executors, validate_create_args
from backend.trace import Tracer


NOW = "2026-09-03T20:00:00+08:00"


def call(name, **args):
    return ToolCall(name=name, arguments=args)


def run(tool_calls, storage=None, text=""):
    storage = storage if storage is not None else FakeStorage()
    tracer = Tracer()
    llm = StubLLM(tool_calls=tool_calls, text=text)
    result = run_agent("...", llm, storage, tracer, FrozenClock(NOW), "t-1")
    return result, storage, tracer


# ---------- 参数校验 ----------


def test_validate_accepts_minimal_record():
    record = validate_create_args({"exercise": "卧推"})
    assert record["exercise"] == "卧推"
    assert record["weight_kg"] is None


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"exercise": ""},
        {"exercise": "卧推", "weight_kg": "60"},
        {"exercise": "卧推", "sets": -1},
        {"exercise": "卧推", "sets": True},
        {"exercise": "卧推", "unknown": 1},
    ],
)
def test_validate_rejects_bad_args(args):
    with pytest.raises(ToolError):
        validate_create_args(args)


def test_query_tool_rejects_bad_date():
    executors = make_executors(FakeStorage(), FrozenClock(NOW), "t-1")
    with pytest.raises(ToolError):
        executors["query_records"]({"date": "2026-9-3"})


def test_count_tool_rejects_reversed_range():
    executors = make_executors(FakeStorage(), FrozenClock(NOW), "t-1")
    with pytest.raises(ToolError):
        executors["count_exercise"]({"exercise": "卧推", "from": "2026-09-05", "to": "2026-09-01"})


# ---------- 四类失败模式（PRD 第 5 节）----------


def test_wrong_tool_is_visible_in_trajectory():
    """选错工具：调了写记录，但用户是要查询——trajectory 里看得见调的是谁。"""
    result, storage, _ = run([call("create_record", exercise="卧推")])
    assert [step["tool"] for step in result["trajectory"]] == ["create_record"]


def test_no_tool_call_when_model_declines():
    """该调不调：模型只给文本，不调工具——写入必须为零。"""
    result, storage, _ = run([], text="暂不支持按部位统计")
    assert result["tool_count"] == 0
    assert storage.read_all() == []
    assert result["text"] == "暂不支持按部位统计"


def test_unknown_tool_is_marked_failed():
    result, storage, _ = run([call("delete_everything", target="all")])
    step = result["trajectory"][0]
    assert step["ok"] is False
    assert step["error"] == "unknown_tool"
    assert storage.read_all() == []


def test_tool_error_is_captured_not_raised():
    """工具报错不能崩，要落进 trajectory 供断言。"""
    result, storage, _ = run([call("create_record", exercise="卧推", sets=-3)])
    step = result["trajectory"][0]
    assert step["ok"] is False
    assert storage.read_all() == []


def test_first_failure_step_is_reported():
    """早期错误会级联，必须定位到第一个出错的步骤。"""
    result, _, _ = run(
        [
            call("create_record", exercise="卧推", sets=4),
            call("create_record", exercise="", sets=1),
            call("unknown_tool"),
        ]
    )
    assert result["failed_steps"] == [2, 3]
    assert result["first_failure_step"] == 2


# ---------- 单轮上限 ----------


def test_exceeding_tool_call_limit_aborts_without_executing():
    calls = [call("create_record", exercise=f"动作{i}") for i in range(MAX_TOOL_CALLS + 1)]
    result, storage, _ = run(calls)
    assert result["ok"] is False
    assert result["error_code"] == "too_many_tool_calls"
    # 超限直接终止，一条都不许写
    assert storage.read_all() == []


# ---------- 复合请求（本轮核心价值）----------


def test_compound_request_runs_multiple_tools():
    """固定链路做不到的部分：一句话既查又记。"""
    storage = FakeStorage()
    result, storage, _ = run(
        [
            call("query_records", date="2026-09-03"),
            call("create_record", exercise="跑步", distance_km=3),
        ],
        storage=storage,
    )
    assert [step["tool"] for step in result["trajectory"]] == ["query_records", "create_record"]
    assert all(step["ok"] for step in result["trajectory"])
    assert len(storage.read_all()) == 1


def test_multiple_records_in_one_turn():
    result, storage, _ = run(
        [
            call("create_record", exercise="硬拉", weight_kg=100, sets=3),
            call("create_record", exercise="划船", sets=4),
        ]
    )
    assert len(storage.read_all()) == 2
    assert result["tool_count"] == 2


# ---------- trace ----------


def test_trace_covers_agent_and_tool_nodes():
    result, _, tracer = run([call("create_record", exercise="卧推", sets=4)])
    assert {event["trace_id"] for event in tracer.events} == {"t-1"}
    assert {event["node"] for event in tracer.events} == {"agent", "tool"}
    events = [event["event"] for event in tracer.events]
    assert events == ["agent_request", "agent_response", "tool_result", "result"]
    request = next(event for event in tracer.events if event["event"] == "agent_request")
    assert request["payload"]["prompt_name"] == "agent_system"
    assert request["payload"]["prompt_version"] == "v1"


def test_written_record_carries_trace_id():
    _, storage, _ = run([call("create_record", exercise="卧推", sets=4)])
    assert storage.read_all()[0]["trace_id"] == "t-1"


def test_llm_failure_returns_error_code():
    tracer = Tracer()
    result = run_agent(
        "...", StubLLM(fault="llm_timeout"), FakeStorage(), tracer, FrozenClock(NOW), "t-1"
    )
    assert result["ok"] is False
    assert result["error_code"] == "llm_timeout"


def test_tool_eval_trace_id_is_unique_per_trial():
    first = build_trace_id("tl-001")
    second = build_trace_id("tl-001")
    assert first.startswith("t-tl-001-")
    assert second.startswith("t-tl-001-")
    assert first != second
