import json

import pytest

from backend.extractor import (
    aggregate_state,
    decide_record_state,
    extract,
    parse_extractor_output,
)
from backend.llm import StubLLM
from backend.pipeline import handle_message
from backend.storage import FakeStorage, JsonlStorage
from backend.trace import Tracer


def records_json(*records):
    filled = []
    for record in records:
        item = {
            "exercise": None,
            "weight_kg": None,
            "sets": None,
            "reps": None,
            "duration_min": None,
            "distance_km": None,
        }
        item.update(record)
        filled.append(item)
    return json.dumps({"records": filled}, ensure_ascii=False)


# ---------- 解析契约 ----------


def test_parse_valid_multi_record():
    raw = records_json(
        {"exercise": "硬拉", "weight_kg": 100, "sets": 3},
        {"exercise": "划船", "sets": 4},
    )
    parsed = parse_extractor_output(raw)
    assert len(parsed) == 2
    assert parsed[0]["exercise"] == "硬拉"
    assert parsed[1]["sets"] == 4
    assert parsed[1]["weight_kg"] is None


def test_parse_empty_records_is_valid():
    assert parse_extractor_output('{"records":[]}') == []


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"records":{}}',
        '{"records":[],"extra":1}',
        '{"items":[]}',
        '{"records":[{"exercise":"卧推"}]}',  # 字段不全
        '{"records":[{"exercise":"","weight_kg":null,"sets":null,"reps":null,"duration_min":null,"distance_km":null}]}',
        '{"records":[{"exercise":"卧推","weight_kg":"60","sets":null,"reps":null,"duration_min":null,"distance_km":null}]}',
        '{"records":[{"exercise":"卧推","weight_kg":-5,"sets":null,"reps":null,"duration_min":null,"distance_km":null}]}',
        '{"records":[{"exercise":"卧推","weight_kg":true,"sets":null,"reps":null,"duration_min":null,"distance_km":null}]}',
    ],
)
def test_parse_invalid_shapes(raw):
    with pytest.raises(ValueError):
        parse_extractor_output(raw)


# ---------- 三态判定 ----------


def test_state_complete_by_weight():
    assert decide_record_state({"exercise": "卧推", "weight_kg": 60}) == "complete"


def test_state_complete_by_distance_for_cardio():
    """有氧的量化是距离/时长，不要求重量组数。"""
    assert decide_record_state({"exercise": "跑步", "distance_km": 5}) == "complete"
    assert decide_record_state({"exercise": "有氧", "duration_min": 30}) == "complete"


def test_state_incomplete_when_no_quantity():
    record = {"exercise": "胸", "weight_kg": None, "sets": None, "reps": None,
              "duration_min": None, "distance_km": None}
    assert decide_record_state(record) == "incomplete"


def test_aggregate_takes_most_conservative():
    assert aggregate_state([]) == "invalid"
    assert aggregate_state([{"state": "complete"}, {"state": "complete"}]) == "complete"
    assert aggregate_state([{"state": "complete"}, {"state": "incomplete"}]) == "incomplete"


# ---------- 抽取节点 ----------


def test_extract_uses_caller_trace_id():
    """trace_id 必须由上游传入，不允许各层各生成一个。"""
    tracer = Tracer()
    result = extract("今天卧推60kg4组", StubLLM(raw_text=records_json(
        {"exercise": "卧推", "weight_kg": 60, "sets": 4})), tracer, "t-fixed")
    assert result["trace_id"] == "t-fixed"
    assert {event["trace_id"] for event in tracer.events} == {"t-fixed"}
    assert {event["node"] for event in tracer.events} == {"extractor"}


def test_extract_bad_json_returns_parse_error():
    result = extract("今天练了胸", StubLLM(raw_text="not-json"), Tracer(), "t-1")
    assert result["error_code"] == "llm_parse_error"


# ---------- 写入规则（PRD 退出标准 3 与 4）----------


def build_llms(intent, extractor_raw):
    router = StubLLM(raw_text=json.dumps({"intent": intent, "confidence": 0.9}))
    return router, StubLLM(raw_text=extractor_raw)


def test_invalid_writes_nothing():
    """退出标准 3：invalid 必须零写入。"""
    storage = FakeStorage()
    router, extractor = build_llms("record", '{"records":[]}')
    result = handle_message("今天练得挺爽", router, extractor, storage, Tracer())
    assert result["state"] == "invalid"
    assert result["written_ids"] == []
    assert storage.read_all() == []


def test_incomplete_is_written_and_marked():
    """决策 3：incomplete 也写入，但必须带 state 标记。"""
    storage = FakeStorage()
    router, extractor = build_llms("record", records_json({"exercise": "胸"}))
    result = handle_message("今天练了胸", router, extractor, storage, Tracer())
    assert result["state"] == "incomplete"
    rows = storage.read_all()
    assert len(rows) == 1
    assert rows[0]["state"] == "incomplete"


def test_multi_exercise_writes_multiple_rows():
    """决策 4：一句多动作拆成多条。"""
    storage = FakeStorage()
    raw = records_json(
        {"exercise": "硬拉", "weight_kg": 100, "sets": 3},
        {"exercise": "划船", "sets": 4},
    )
    router, extractor = build_llms("record", raw)
    result = handle_message("硬拉100kg三组，然后划船做了四组", router, extractor, storage, Tracer())
    rows = storage.read_all()
    assert len(rows) == 2
    assert [row["exercise"] for row in rows] == ["硬拉", "划船"]
    assert len(result["written_ids"]) == 2


def test_non_record_intent_never_writes():
    """query / reject 不得产生任何写入。"""
    for intent in ("query", "reject"):
        storage = FakeStorage()
        router, extractor = build_llms(intent, records_json({"exercise": "卧推", "sets": 4}))
        result = handle_message("这周卧推了几次", router, extractor, storage, Tracer())
        assert result["stage"] == "router"
        assert storage.read_all() == []


def test_extractor_failure_writes_nothing():
    storage = FakeStorage()
    router, extractor = build_llms("record", "not-json")
    result = handle_message("今天卧推60kg", router, extractor, storage, Tracer())
    assert result["error_code"] == "llm_parse_error"
    assert storage.read_all() == []


def test_trace_id_spans_all_three_layers():
    storage = FakeStorage()
    tracer = Tracer()
    router, extractor = build_llms("record", records_json({"exercise": "卧推", "weight_kg": 60}))
    result = handle_message("今天卧推60kg", router, extractor, storage, tracer)
    assert {event["trace_id"] for event in tracer.events} == {result["trace_id"]}
    assert {event["node"] for event in tracer.events} == {"router", "extractor", "storage"}
    # 落盘记录必须带 trace_id，便于从脏数据反查调用
    assert storage.read_all()[0]["trace_id"] == result["trace_id"]


def test_jsonl_storage_roundtrip(tmp_path):
    storage = JsonlStorage(tmp_path / "records.jsonl")
    ids = storage.append([{"exercise": "卧推", "weight_kg": 60, "state": "complete"}], "t-1")
    assert len(ids) == 1
    rows = storage.read_all()
    assert rows[0]["exercise"] == "卧推"
    assert rows[0]["trace_id"] == "t-1"
    assert rows[0]["id"] == ids[0]


def test_jsonl_storage_appends_without_truncating(tmp_path):
    storage = JsonlStorage(tmp_path / "records.jsonl")
    storage.append([{"exercise": "卧推", "state": "incomplete"}], "t-1")
    storage.append([{"exercise": "深蹲", "state": "incomplete"}], "t-2")
    assert len(storage.read_all()) == 2
