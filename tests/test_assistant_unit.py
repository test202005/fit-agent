import json

import pytest

from backend.app import create_app
from backend.assistant import MAX_ROUNDS, MAX_TOOL_CALLS, TOOL_NAMES, run_assistant
from backend.clock import FrozenClock
from backend.llm import StubLLM, ToolCall, ToolCallResult
from backend.routine import FakeRoutineStore
from backend.storage import FakeStorage, SQLiteStorage
from backend.trace import Tracer


NOW = "2026-09-25T12:30:00+08:00"
UNITS = [
    {"unit_id": "chest", "name": "胸日", "status": "active"},
    {"unit_id": "mobility", "name": "灵活性恢复", "status": "paused"},
    {"unit_id": "legs", "name": "腿日", "status": "active"},
]
NEED = {"muscle": "下肢", "level": "新手", "difficulty": "简单", "duration_min": 30}
WORKOUT = {"target_duration_min": 30, "estimated_duration_min": None,
           "plan": [{"name": "徒手深蹲", "sets": 3, "reps": 12, "rest_sec": 60}], "note": "注意热身"}


def calls(*items):
    return ToolCallResult(tool_calls=[ToolCall(name=n, arguments=a, id=f"{n}-{i}") for i, (n, a) in enumerate(items)])


def run(rounds, storage=None, plan_llm=None, tool_names=None, text="x"):
    storage = storage or FakeStorage()
    store = FakeRoutineStore()
    store.seed("u", UNITS)
    llm = StubLLM(tool_rounds=rounds, text="好了")
    tracer = Tracer()
    result = run_assistant(text, llm, plan_llm or StubLLM(), storage, store, tracer,
                           FrozenClock(NOW), "t-1", "u", tool_names=tool_names)
    return result, tracer, llm, storage, store


def test_has_all_tools():
    assert TOOL_NAMES == {"create_record", "query_records", "count_exercise",
                          "get_routine", "set_routine_order", "generate_workout_plan",
                          "weekly_summary", "get_action_guide"}


def test_record_then_reply():
    result, _, llm, storage, _ = run([calls(("create_record", {"exercise": "俯卧撑", "reps": 100}))])
    assert result["ok"] and result["writes"] == 1 and result["text"] == "好了"
    assert storage.read_all("u")[0]["exercise"] == "俯卧撑"
    # 当前时间注入到用户消息，相对日期才能换算
    assert "当前时间：2026-09-25 12:30（星期五）" in llm.received_messages[0][1]["content"]


def test_two_records_in_one_request_are_both_written(tmp_path):
    # 回归：若按整请求 request_id 幂等，第二条会被当成重放而丢失
    storage = SQLiteStorage(tmp_path / "r.sqlite3")
    result, _, _, _, _ = run([calls(("create_record", {"exercise": "卧推", "sets": 5}),
                                    ("create_record", {"exercise": "跑步", "distance_km": 5}))], storage=storage)
    assert result["writes"] == 2
    assert [r["exercise"] for r in storage.read_all("u")] == ["卧推", "跑步"]
    assert not any(a["output"].get("idempotent_replay") for a in result["attempts"])


def test_cross_capability_record_and_routine():
    result, _, _, storage, store = run([
        calls(("create_record", {"exercise": "俯卧撑", "reps": 100}), ("get_routine", {"include_paused": True})),
        calls(("set_routine_order", {"order": ["legs", "chest", "mobility"]})),
    ])
    assert [a["tool"] for a in result["actions"]] == ["create_record", "get_routine", "set_routine_order"]
    assert result["writes"] == 2
    assert [u["unit_id"] for u in store.read("u")] == ["legs", "chest", "mobility"]


def test_invalid_args_are_fed_back_for_retry():
    result, _, llm, storage, _ = run([
        calls(("create_record", {"exercise": "俯卧撑", "reps": -1})),
        calls(("create_record", {"exercise": "俯卧撑", "reps": 100})),
    ])
    first, second = result["attempts"]
    assert (first["ok"], first["error_code"], second["ok"]) == (False, "invalid_args", True)
    tool_msg = [m for m in llm.received_messages[1] if m["role"] == "tool"][0]
    assert json.loads(tool_msg["content"])["error_code"] == "invalid_args"
    assert len(storage.read_all("u")) == 1


def test_plan_tool_runs_v7_subchain_in_same_trace():
    plan_llm = StubLLM(raw_texts=[json.dumps(NEED, ensure_ascii=False), json.dumps(WORKOUT, ensure_ascii=False)])
    result, tracer, _, _, _ = run([calls(("generate_workout_plan", {"request": "30 分钟下肢训练，新手"}))], plan_llm=plan_llm)
    output = result["attempts"][0]["output"]
    assert output["ok"] and output["plan"][0]["name"] == "徒手深蹲"
    nodes = {e["node"] for e in tracer.events}
    assert {"planner", "generator", "assistant"} <= nodes
    assert {e["trace_id"] for e in tracer.events} == {"t-1"}


def test_plan_subchain_failure_is_reported_not_raised():
    result, _, _, _, _ = run([calls(("generate_workout_plan", {"request": "x"}))], plan_llm=StubLLM(fault="llm_timeout"))
    assert result["attempts"][0]["error_code"] == "llm_timeout" and result["ok"]


def test_no_tool_reply():
    result, _, _, storage, _ = run([])
    assert result["ok"] and result["actions"] == [] and storage.read_all("u") == []


def test_restricted_tools_reject_hidden_tool():
    result, tracer, _, storage, _ = run([calls(("create_record", {"exercise": "卧推", "sets": 1}))],
                                        tool_names={"get_routine", "set_routine_order"})
    assert result["attempts"][0]["error_code"] == "unknown_tool" and storage.read_all("u") == []
    request = next(e for e in tracer.events if e["event"] == "assistant_request")
    assert request["payload"]["tools"] == ["get_routine", "set_routine_order"]


def test_tool_call_cap():
    many = calls(*[("get_routine", {})] * (MAX_TOOL_CALLS + 1))
    result, _, _, _, _ = run([many])
    assert result["error_code"] == "too_many_tool_calls" and result["attempts"] == []


def test_round_cap():
    result, _, _, _, _ = run([calls(("get_routine", {}))] * (MAX_ROUNDS + 1))
    assert result["error_code"] == "max_rounds_exceeded" and result["rounds"] == MAX_ROUNDS


# ---------- /api/assistant ----------


@pytest.fixture
def client(tmp_path):
    def build(llm):
        app = create_app(llm=llm, storage=FakeStorage(), clock=FrozenClock(NOW),
                         routine_store=FakeRoutineStore(), trace_path=tmp_path / "trace.jsonl")
        # 默认引擎为 v2：计划解析与编排客户端同样注入替身，测试不触达真实模型
        app.config.update(TESTING=True, PLAN_LLM=StubLLM(), PLAN_V2_LLM=StubLLM(), PLAN_COMPOSE_LLM=StubLLM())
        return app.test_client()
    return build


def test_api_assistant_success(client):
    c = client(StubLLM(tool_rounds=[calls(("create_record", {"exercise": "俯卧撑", "reps": 100}))], text="记好了"))
    body = c.post("/api/assistant", json={"text": "今天中午做了一百个俯卧撑"}).get_json()
    assert body == {"ok": True, "trace_id": body["trace_id"], "reply": "记好了",
                    "actions": [{"tool": "create_record", "ok": True}]}


@pytest.mark.parametrize("payload", [{}, {"text": " "}, {"text": "x", "user_id": ""}])
def test_api_assistant_bad_request(client, payload):
    response = client(StubLLM()).post("/api/assistant", json=payload)
    assert response.status_code == 400 and response.get_json()["error_code"] == "bad_request"


def test_api_assistant_llm_failure(client):
    body = client(StubLLM(fault="llm_api_error")).post("/api/assistant", json={"text": "x"}).get_json()
    assert body["ok"] is False and body["error_code"] == "llm_api_error"


def test_trace_records_new_inputs_and_reason_per_round():
    rounds = [ToolCallResult(tool_calls=[ToolCall(name="create_record", arguments={"exercise": "深蹲", "reps": 20}, id="c1")],
                             text="用户说了已完成的训练，需要记录")]
    _, tracer, _, _, _ = run(rounds, text="今天上午做了 20 个深蹲")
    first, second = [e["payload"] for e in tracer.events if e["event"] == "llm_round"]
    # 首轮只看到用户消息；系统提示词不重复记
    assert [m["role"] for m in first["new_inputs"]] == ["user"]
    assert "今天上午做了 20 个深蹲" in first["new_inputs"][0]["content"]
    assert first["reason"] == "用户说了已完成的训练，需要记录"
    # 次轮只看到上一步工具结果，不重复模型自己的上一轮输出
    assert [(m["role"], m["tool"]) for m in second["new_inputs"]] == [("tool", "create_record")]
    assert second["new_inputs"][0]["content"]["written"] == 1
    assert second["reason"] is None and second["text"] == "好了"


def test_reason_arg_is_recorded_and_stripped_before_execution():
    rounds = [calls(("create_record", {"reason": "用户说了已完成的训练", "exercise": "深蹲", "reps": 20}),
                    ("set_routine_order", {"reason": "调整顺序", "order": ["legs", "chest", "mobility"]}))]
    result, tracer, _, storage, store = run(rounds)
    # 业务参数校验拒收未知字段：reason 必须在执行前剥掉，否则两个写入都会失败
    assert [a["ok"] for a in result["attempts"]] == [True, True]
    assert result["attempts"][0]["args"] == {"exercise": "深蹲", "reps": 20}
    assert result["attempts"][0]["reason"] == "用户说了已完成的训练"
    first = next(e["payload"] for e in tracer.events if e["event"] == "llm_round")
    assert first["reason"] == "用户说了已完成的训练；调整顺序"


def test_reason_is_required_only_on_assistant_copies():
    from backend.assistant import TOOL_SCHEMAS
    from backend.routine_agent import TOOL_SCHEMAS as V8_SCHEMAS
    from backend.tools import TOOL_SCHEMAS as ITER4_SCHEMAS
    assert all("reason" in s["function"]["parameters"]["required"] for s in TOOL_SCHEMAS)
    assert all("reason" not in s["function"]["parameters"]["properties"] for s in [*V8_SCHEMAS, *ITER4_SCHEMAS])
