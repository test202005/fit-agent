import json

import pytest

from backend.app import create_app
from backend.clock import FrozenClock
from backend.llm import LLMResult
from backend.storage import FakeStorage


NOW = "2026-09-03T20:00:00+08:00"


class ScriptedLLM:
    """按节点返回不同剧本：靠 system prompt 的特征区分是哪一层在调用。"""

    model = "scripted"

    def __init__(self, intent, extract_raw=None, plan_raw=None):
        self.intent = intent
        self.extract_raw = extract_raw
        self.plan_raw = plan_raw

    def complete(self, system_prompt, user_text):
        if "意图分类器" in system_prompt:
            return LLMResult(raw_text=json.dumps({"intent": self.intent, "confidence": 0.9}))
        if "字段抽取器" in system_prompt:
            return LLMResult(raw_text=self.extract_raw)
        return LLMResult(raw_text=self.plan_raw)


@pytest.fixture
def storage():
    return FakeStorage()


def build_client(llm, storage):
    app = create_app(llm=llm, storage=storage, clock=FrozenClock(NOW))
    app.config.update(TESTING=True)
    return app.test_client()


def test_health():
    client = build_client(ScriptedLLM("reject"), FakeStorage())
    assert client.get("/health").get_json() == {"ok": True}


def test_missing_text_is_bad_request(storage):
    client = build_client(ScriptedLLM("reject"), storage)
    response = client.post("/api/chat", json={})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "bad_request"


def test_non_json_body_is_bad_request(storage):
    client = build_client(ScriptedLLM("reject"), storage)
    response = client.post("/api/chat", data="plain text", content_type="text/plain")
    assert response.status_code == 400


def test_record_then_query_end_to_end(storage):
    """退出标准 6：写入后能读回，一条链路端到端跑通。"""
    write_llm = ScriptedLLM(
        "record",
        extract_raw=json.dumps(
            {
                "records": [
                    {
                        "exercise": "卧推",
                        "weight_kg": 60,
                        "sets": 4,
                        "reps": 8,
                        "duration_min": None,
                        "distance_km": None,
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    write = build_client(write_llm, storage).post(
        "/api/chat", json={"text": "今天卧推60kg做了4组每组8次"}
    )
    assert write.status_code == 200
    assert write.get_json()["state"] == "complete"
    assert len(write.get_json()["written_ids"]) == 1

    read_llm = ScriptedLLM("query", plan_raw='{"type":"list_by_date","date":"2026-09-03"}')
    read = build_client(read_llm, storage).post("/api/chat", json={"text": "今天练了什么"})
    body = read.get_json()
    assert read.status_code == 200
    assert body["count"] == 1
    assert body["records"][0]["exercise"] == "卧推"


def test_internal_error_does_not_leak_stacktrace(storage):
    class ExplodingLLM:
        model = "boom"

        def complete(self, system_prompt, user_text):
            raise RuntimeError("secret internals")

    response = build_client(ExplodingLLM(), storage).post("/api/chat", json={"text": "今天练了胸"})
    assert response.status_code == 500
    body = response.get_json()
    assert body["error_code"] == "internal_error"
    assert "secret internals" not in json.dumps(body)


def test_query_via_http_never_writes(storage):
    llm = ScriptedLLM("query", plan_raw='{"type":"list_by_date","date":"2026-09-03"}')
    build_client(llm, storage).post("/api/chat", json={"text": "今天练了什么"})
    assert storage.read_all() == []
