import pytest

from backend.clock import FrozenClock, LOCAL_TZ
from backend.llm import StubLLM
from backend.pipeline import handle_message
from backend.query import execute_plan, parse_plan, plan_query, week_start
from backend.storage import FakeStorage
from backend.trace import Tracer


NOW = "2026-09-03T20:00:00+08:00"  # 星期四


def seeded_storage(*rows):
    """按 (动作, 本地时间字符串) 预置记录。"""
    storage = FakeStorage()
    for index, (exercise, ts) in enumerate(rows, start=1):
        storage.rows.append(
            {
                "id": f"r-{index:03d}",
                "ts": ts,
                "state": "complete",
                "trace_id": "t-seed",
                "exercise": exercise,
                "weight_kg": None,
                "sets": None,
                "reps": None,
                "duration_min": None,
                "distance_km": None,
            }
        )
    return storage


# ---------- 计划解析契约 ----------


def test_parse_list_by_date():
    assert parse_plan('{"type":"list_by_date","date":"2026-09-03"}') == {
        "type": "list_by_date",
        "date": "2026-09-03",
    }


def test_parse_unsupported():
    assert parse_plan('{"type":"unsupported"}') == {"type": "unsupported"}


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"type":"list_all"}',
        '{"type":"list_by_date"}',
        '{"type":"list_by_date","date":"2026-9-3"}',
        '{"type":"list_by_date","date":"09/03/2026"}',
        '{"type":"list_by_date","date":"2026-09-03","extra":1}',
        '{"type":"unsupported","date":"2026-09-03"}',
        '{"type":"count_by_exercise","exercise":"卧推","from":"2026-09-05","to":"2026-09-01"}',
        '{"type":"count_by_exercise","exercise":"","from":"2026-09-01","to":"2026-09-03"}',
    ],
)
def test_parse_invalid_plans(raw):
    with pytest.raises(ValueError):
        parse_plan(raw)


# ---------- 时间边界：只有冻结时间才写得出来 ----------


def test_today_lower_boundary_included():
    """正好 00:00:00 的记录属于今天。"""
    storage = seeded_storage(("卧推", "2026-09-03T00:00:00+08:00"))
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, storage)
    assert out["count"] == 1


def test_yesterday_last_second_excluded():
    """昨天 23:59:59 差一秒，必须被排除。"""
    storage = seeded_storage(("卧推", "2026-09-02T23:59:59+08:00"))
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, storage)
    assert out["count"] == 0


def test_utc_stored_record_maps_to_local_day():
    """存的是 UTC，业务日期要按本地时区算，否则跨零点会错位。"""
    # UTC 2026-09-02T17:30 == 本地 2026-09-03T01:30
    storage = seeded_storage(("卧推", "2026-09-02T17:30:00+00:00"))
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, storage)
    assert out["count"] == 1


def test_week_start_is_monday_not_rolling_seven_days():
    """口径 3：这周 = 周一起算。"""
    thursday = FrozenClock(NOW).now()
    assert week_start(thursday) == "2026-08-31"


def test_week_start_on_monday_is_today():
    monday = FrozenClock("2026-08-31T09:00:00+08:00").now()
    assert week_start(monday) == "2026-08-31"


def test_count_range_excludes_out_of_range():
    """第三问：不该出现的不能混进来。"""
    storage = seeded_storage(
        ("卧推", "2026-08-30T10:00:00+08:00"),  # 上周日，范围外
        ("卧推", "2026-08-31T10:00:00+08:00"),  # 本周一，范围内
        ("卧推", "2026-09-03T10:00:00+08:00"),  # 今天，范围内
    )
    out = execute_plan(
        {"type": "count_by_exercise", "exercise": "卧推", "from": "2026-08-31", "to": "2026-09-03"},
        storage,
    )
    assert out["count"] == 2


def test_count_matches_exercise_exactly():
    """口径 8：精确匹配，不做同义词归一。"""
    storage = seeded_storage(
        ("卧推", "2026-09-03T10:00:00+08:00"),
        ("杠铃卧推", "2026-09-03T11:00:00+08:00"),
    )
    out = execute_plan(
        {"type": "count_by_exercise", "exercise": "卧推", "from": "2026-09-01", "to": "2026-09-03"},
        storage,
    )
    assert out["count"] == 1


# ---------- 结果口径 ----------


def test_results_sorted_newest_first():
    storage = seeded_storage(
        ("卧推", "2026-09-03T09:00:00+08:00"),
        ("深蹲", "2026-09-03T18:00:00+08:00"),
    )
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, storage)
    assert [row["exercise"] for row in out["records"]] == ["深蹲", "卧推"]


def test_empty_result_is_not_an_error():
    """口径 7：查无结果返回空数组，不是错误。"""
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, FakeStorage())
    assert out["count"] == 0
    assert out["records"] == []
    assert out["supported"] is True


def test_unsupported_returns_flag_not_error():
    out = execute_plan({"type": "unsupported"}, seeded_storage(("卧推", NOW)))
    assert out["supported"] is False
    assert out["count"] == 0


def test_incomplete_records_are_included_with_state():
    """口径 6：incomplete 计入结果，但带标记。"""
    storage = seeded_storage(("胸", "2026-09-03T10:00:00+08:00"))
    storage.rows[0]["state"] = "incomplete"
    out = execute_plan({"type": "list_by_date", "date": "2026-09-03"}, storage)
    assert out["count"] == 1
    assert out["records"][0]["state"] == "incomplete"


# ---------- Planner 与链路 ----------


def test_planner_receives_current_time():
    """相对日期要靠"今天是几号"，当前时间必须喂给模型。"""
    captured = {}

    class SpyLLM:
        model = "spy"

        def complete(self, system_prompt, user_text):
            captured["user_text"] = user_text
            from backend.llm import LLMResult

            return LLMResult(raw_text='{"type":"list_by_date","date":"2026-09-03"}')

    plan_query("今天练了什么", SpyLLM(), Tracer(), "t-1", FrozenClock(NOW))
    assert "2026-09-03" in captured["user_text"]
    assert "四" in captured["user_text"]  # 星期四


def test_planner_bad_json_returns_parse_error():
    result = plan_query("今天练了什么", StubLLM(raw_text="not-json"), Tracer(), "t-1", FrozenClock(NOW))
    assert result["error_code"] == "llm_parse_error"


def test_query_pipeline_end_to_end():
    storage = seeded_storage(("卧推", "2026-09-03T10:00:00+08:00"))
    tracer = Tracer()
    router = StubLLM(raw_text='{"intent":"query","confidence":0.9}')
    planner = StubLLM(raw_text='{"type":"list_by_date","date":"2026-09-03"}')
    result = handle_message(
        "今天练了什么", router, planner, storage, tracer,
        query_llm=planner, clock=FrozenClock(NOW),
    )
    assert result["ok"] is True
    assert result["stage"] == "executor"
    assert result["count"] == 1
    assert result["query"]["type"] == "list_by_date"
    # 同一 trace_id 贯穿 router / planner / executor
    assert {event["trace_id"] for event in tracer.events} == {result["trace_id"]}
    assert {event["node"] for event in tracer.events} == {"router", "planner", "executor"}


def test_query_result_is_stable_across_days():
    """换一天跑，只要注入同一个 now，结果必须不变——证明时间已解耦。"""
    storage = seeded_storage(("卧推", "2026-09-03T10:00:00+08:00"))
    outs = []
    for _ in range(2):
        router = StubLLM(raw_text='{"intent":"query","confidence":0.9}')
        planner = StubLLM(raw_text='{"type":"list_by_date","date":"2026-09-03"}')
        outs.append(
            handle_message(
                "今天练了什么", router, planner, storage, Tracer(),
                query_llm=planner, clock=FrozenClock(NOW),
            )["count"]
        )
    assert outs == [1, 1]
