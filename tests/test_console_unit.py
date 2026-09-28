import json

import pytest

from backend.app import create_app
from backend.clock import FrozenClock
from backend.console import CAPABILITIES_PATH, parse_capabilities, reply_text
from backend.llm import LLMResult, StubLLM, ToolCall, ToolCallResult
from backend.routine import FakeRoutineStore
from backend.storage import FakeStorage


NOW = "2026-09-25T20:00:00+08:00"
BASE7_ACTIVE = ["胸日", "背日", "腿日", "肩日", "核心"]


class RouterRecordLLM:
    """自动路由链路：意图判为 record，抽取出一条完整记录。"""

    model = "scripted"

    def complete(self, system_prompt, user_text):
        if "意图分类器" in system_prompt:
            return LLMResult(raw_text=json.dumps({"intent": "record", "confidence": 0.9}))
        return LLMResult(raw_text=json.dumps({"records": [
            {"exercise": "卧推", "weight_kg": 60, "sets": 5, "reps": 8,
             "duration_min": None, "distance_km": None}]}))


@pytest.fixture
def env(tmp_path):
    def build(llm):
        store, storage = FakeRoutineStore(), FakeStorage()
        app = create_app(llm=llm, storage=storage, clock=FrozenClock(NOW),
                         routine_store=store, trace_path=tmp_path / "trace.jsonl")
        app.config.update(TESTING=True)
        return app.test_client(), store, storage
    return build


def call(name, args):
    return ToolCallResult(tool_calls=[ToolCall(name=name, arguments=args, id=name)])


def test_page_and_static_assets_are_served(env):
    client, _, _ = env(StubLLM())
    assert "调试台" in client.get("/console").get_data(as_text=True)
    assert client.get("/console/static/trace_render.js").status_code == 200
    assert client.get("/console/static/trace.css").status_code == 200


def test_panel_before_and_after_reset(env):
    client, _, _ = env(StubLLM())
    empty = client.get("/console/api/panel?user_id=u").get_json()
    assert empty["routine_initialized"] is False and empty["routine"] == []
    assert client.post("/console/api/reset", json={"user_id": "u"}).get_json()["ok"]
    panel = client.get("/console/api/panel?user_id=u").get_json()
    # 用户视角只看 active：暂停单元不出现
    assert [u["name"] for u in panel["routine"]] == BASE7_ACTIVE


def test_routine_run_returns_reply_trace_and_updates_panel(env):
    order = ["legs", "chest", "back", "mobility", "shoulders", "cardio", "core"]
    llm = StubLLM(tool_rounds=[call("get_routine", {"include_paused": True}),
                               call("set_routine_order", {"order": order})], text="已把腿日放到最前面")
    client, _, _ = env(llm)
    client.post("/console/api/reset", json={"user_id": "u"})
    body = client.post("/console/api/run", json={"chain": "routine", "text": "把腿日放到最前面", "user_id": "u"}).get_json()
    assert body["ok"] and body["reply"] == "已把腿日放到最前面"
    panel = client.get("/console/api/panel?user_id=u").get_json()
    assert [u["name"] for u in panel["routine"]][:2] == ["腿日", "胸日"]

    detail = client.get(f"/console/api/traces/{body['trace_id']}").get_json()
    events = [(e["node"], e["event"]) for e in detail["events"]]
    assert ("tool", "tool_attempt") in events and ("state", "state_after") in events
    listed = client.get("/console/api/traces").get_json()["traces"]
    assert listed[0]["id"] == body["trace_id"]


def test_auto_chain_record_shows_in_panel(env):
    client, _, _ = env(RouterRecordLLM())
    body = client.post("/console/api/run", json={"chain": "auto", "text": "今天卧推60公斤5组每组8个", "user_id": "u"}).get_json()
    assert body["ok"] and body["reply"].startswith("已记录：卧推 60kg 5组 8次")
    records = client.get("/console/api/panel?user_id=u").get_json()["records"]
    assert records[0]["text"] == "卧推 60kg 5组 8次"


@pytest.mark.parametrize("payload", [
    {}, {"text": "  "}, {"text": "x", "chain": "nope"}, {"text": "x", "user_id": " "},
])
def test_run_rejects_bad_requests(env, payload):
    client, _, _ = env(StubLLM())
    response = client.post("/console/api/run", json=payload)
    assert response.status_code == 400 and response.get_json()["error_code"] == "bad_request"


def test_unknown_trace_is_404(env):
    client, _, _ = env(StubLLM())
    assert client.get("/console/api/traces/t-missing").status_code == 404


def test_llm_failure_is_reported_not_raised(env):
    client, _, _ = env(StubLLM(fault="llm_timeout"))
    client.post("/console/api/reset", json={"user_id": "u"})
    body = client.post("/console/api/run", json={"chain": "routine", "text": "x", "user_id": "u"}).get_json()
    assert body["ok"] is False and "llm_timeout" in body["reply"]


def test_reply_text_for_other_chains():
    assert reply_text("auto", {"ok": True, "intent": "reject"}).startswith("这个我暂时帮不了")
    assert reply_text("auto", {"ok": True, "intent": "query", "supported": True, "count": 0, "records": []}).startswith("没有查到")
    plan = {"ok": True, "workout": {"plan": [{"name": "深蹲", "sets": 3, "reps": 10, "rest_sec": 60}], "note": "注意热身"}}
    assert reply_text("plan", plan) == "深蹲 3组×10次，休息60秒\n注意热身"


def test_capabilities_come_from_doc_with_valid_chains(env):
    client, _, _ = env(StubLLM())
    caps = client.get("/console/api/capabilities").get_json()["capabilities"]
    assert len(caps) >= 8 and all(c["examples"] for c in caps)
    assert {c["chain"] for c in caps} == {"assistant"}
    assert caps == parse_capabilities(CAPABILITIES_PATH.read_text(encoding="utf-8"))
