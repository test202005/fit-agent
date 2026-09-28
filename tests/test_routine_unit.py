import json

import pytest

from backend.clock import FrozenClock
from backend.llm import StubLLM, ToolCall, ToolCallResult
from backend.routine import (
    FakeRoutineStore,
    RoutineProtocolError,
    SQLiteRoutineStore,
    validate_order,
)
from backend.routine_agent import MAX_ROUNDS, run_routine_agent
from backend.trace import Tracer
from eval import run_routine_eval as runner
from eval.trace_contract import assert_trace_contract


UNITS = [
    {"unit_id": "chest", "name": "胸日", "status": "active"},
    {"unit_id": "back", "name": "背日", "status": "active"},
    {"unit_id": "mobility", "name": "灵活性恢复", "status": "paused"},
    {"unit_id": "legs", "name": "腿日", "status": "active"},
]
FULL = ["chest", "back", "mobility", "legs"]
LEGS_FIRST = ["legs", "chest", "back", "mobility"]


def call(name, args, id_="c"):
    return ToolCallResult(tool_calls=[ToolCall(name=name, arguments=args, id=id_)])


def run(rounds, store=None, text="把腿日放到最前面"):
    store = store or FakeRoutineStore()
    if not store.read("u"):
        store.seed("u", UNITS)
    llm = StubLLM(tool_rounds=rounds, text="好了")
    tracer = Tracer()
    result = run_routine_agent(text, llm, store, tracer, FrozenClock("2026-09-25T20:00:00+08:00"), "t-1", "u")
    return result, tracer, llm, store


# ---------- 协议 ----------


def test_validate_order_accepts_full_permutation():
    assert validate_order(UNITS, LEGS_FIRST) == LEGS_FIRST


@pytest.mark.parametrize("order, code", [
    (["legs", "chest", "back"], "incomplete_order"),
    (["legs", "chest", "back", "mobility", "swim"], "unknown_unit"),
    (["legs", "legs", "chest", "back"], "duplicate_unit"),
    ("legs,chest", "invalid_order"),
])
def test_validate_order_rejects(order, code):
    with pytest.raises(RoutineProtocolError) as exc:
        validate_order(UNITS, order)
    assert exc.value.code == code


def test_incomplete_order_message_does_not_list_missing_units():
    with pytest.raises(RoutineProtocolError) as exc:
        validate_order(UNITS, ["legs", "chest", "back"])
    assert "mobility" not in exc.value.message


# ---------- 存储 ----------


def test_sqlite_store_roundtrip_and_user_isolation(tmp_path):
    store = SQLiteRoutineStore(tmp_path / "r.sqlite3")
    store.seed("u", UNITS)
    store.seed("other", UNITS[:2])
    store.replace_order("u", LEGS_FIRST, "t-1")
    assert [u["unit_id"] for u in store.read("u")] == LEGS_FIRST
    assert [u["unit_id"] for u in store.read("other")] == ["chest", "back"]
    reopened = SQLiteRoutineStore(tmp_path / "r.sqlite3")
    assert reopened.read("u")[3] == {"unit_id": "mobility", "name": "灵活性恢复", "status": "paused"}


def test_seed_rejects_bad_status():
    with pytest.raises(ValueError):
        FakeRoutineStore().seed("u", [{"unit_id": "a", "name": "A", "status": "done"}])


# ---------- 循环 ----------


def test_default_view_hides_paused_units():
    result, _, _, _ = run([call("get_routine", {})])
    ids = [u["unit_id"] for u in result["attempts"][0]["output"]["units"]]
    assert ids == ["chest", "back", "legs"]


def test_success_path_writes_once():
    result, tracer, _, store = run([
        call("get_routine", {"include_paused": True}),
        call("set_routine_order", {"order": LEGS_FIRST}),
    ])
    assert result["ok"] and result["writes"] == 1 and result["rounds"] == 3
    assert [u["unit_id"] for u in store.read("u")] == LEGS_FIRST
    assert assert_trace_contract(tracer.events, "routine_reorder", "t-1")["first_missing_step"] is None


def test_rejected_attempt_is_fed_back_and_retry_succeeds():
    result, tracer, llm, store = run([
        call("get_routine", {}),
        call("set_routine_order", {"order": ["legs", "chest", "back"]}, "w1"),
        call("get_routine", {"include_paused": True}),
        call("set_routine_order", {"order": LEGS_FIRST}, "w2"),
    ])
    writes = [a for a in result["attempts"] if a["tool"] == "set_routine_order"]
    assert [(w["attempt"], w["ok"], w["error_code"]) for w in writes] == [
        (1, False, "incomplete_order"), (2, True, None)]
    assert result["writes"] == 1
    fed_back = [m for m in llm.received_messages[2] if m["role"] == "tool" and m["tool_call_id"] == "w1"]
    assert json.loads(fed_back[0]["content"])["error_code"] == "incomplete_order"
    assert [e["event"] for e in tracer.events].count("tool_attempt") == 4


def test_rejected_write_does_not_change_state():
    result, _, _, store = run([call("set_routine_order", {"order": ["legs"]})])
    assert result["writes"] == 0
    assert [u["unit_id"] for u in store.read("u")] == FULL


def test_max_rounds_stops_loop():
    result, _, _, _ = run([call("get_routine", {})] * (MAX_ROUNDS + 2))
    assert result["ok"] is False and result["error_code"] == "max_rounds_exceeded"
    assert result["rounds"] == MAX_ROUNDS


def test_llm_timeout_is_error_without_write():
    store = FakeRoutineStore()
    store.seed("u", UNITS)
    result = run_routine_agent("x", StubLLM(fault="llm_timeout"), store, Tracer(),
                               FrozenClock("2026-09-25T20:00:00+08:00"), "t-1", "u")
    assert result["error_code"] == "llm_timeout" and result["writes"] == 0


# ---------- 评测判定（每条断言配反例）----------


CASE = {"case_id": "c", "category": "move-first", "op": {"target": "legs", "position": "first", "anchor": None},
        "expect_write": "yes"}


def judged(rounds, case=CASE):
    result, tracer, _, _ = run(rounds)
    contract = assert_trace_contract(tracer.events, "routine_reorder", "t-1")
    j = runner.judge(case, UNITS, result, contract)
    return j, runner.verdict_of(result, j, False)


def test_order_matches_accepts_any_position_relative_to_paused():
    op = {"target": "legs", "position": "first", "anchor": None}
    assert runner.order_matches(UNITS, ["legs", "chest", "back", "mobility"], op)
    op = {"target": "legs", "position": "before", "anchor": "back"}
    # paused 单元在目标前后均可：用户视图相同，非目标相对顺序也相同
    assert runner.order_matches(UNITS, ["chest", "legs", "back", "mobility"], op)
    assert not runner.order_matches(UNITS, ["legs", "back", "chest", "mobility"],
                                    {"target": "legs", "position": "first", "anchor": None})


def test_clean_pass():
    j, verdict = judged([call("get_routine", {"include_paused": True}), call("set_routine_order", {"order": LEGS_FIRST})])
    assert verdict == "PASS" and j["missing_paused_share"] is None


def test_masked_failure_is_review_not_pass():
    j, verdict = judged([
        call("get_routine", {}),
        call("set_routine_order", {"order": ["legs", "chest", "back"]}),
        call("set_routine_order", {"order": LEGS_FIRST}),
    ])
    assert verdict == "REVIEW"
    assert j["gates"]["L4_final_state"] and not j["observations"]["L2_first_protocol_ok"]
    assert j["missing_paused_share"] == 1.0 and j["retried"]


def test_wrong_but_valid_order_fails_final_state():
    # 协议通过但其余单元被打乱：首次顺序与最终状态都要判错
    j, verdict = judged([call("set_routine_order", {"order": ["legs", "back", "chest", "mobility"]})])
    assert verdict == "FAIL"
    assert j["observations"]["L2_first_protocol_ok"] and not j["observations"]["L2p_first_order_correct"]
    assert not j["gates"]["L4_final_state"]


def test_missing_write_fails_l1():
    j, verdict = judged([])
    assert verdict == "FAIL" and not j["gates"]["L1_write_called"]


def test_unexpected_write_fails_no_write_case():
    case = {**CASE, "op": None, "expect_write": "no"}
    j, verdict = judged([call("set_routine_order", {"order": LEGS_FIRST})], case)
    assert verdict == "FAIL" and not j["gates"]["L1_no_write"]


def test_contract_detects_missing_llm_round():
    _, tracer, _, _ = run([])
    events = [e for e in tracer.events if e["event"] != "llm_round"]
    assert assert_trace_contract(events, "routine_reorder", "t-1")["first_missing_step"] == "llm/llm_round"


def test_dataset_and_fixtures_load():
    cases = runner.load_cases("all")
    assert {v for c in cases for v in c["views"]} == set(runner.VIEWS)
    fixtures = runner.load_fixtures()
    for case in cases:
        if case["op"]:
            ids = [u["unit_id"] for u in fixtures[case["fixture"]]]
            assert runner.apply_move(ids, case["op"]) != ids or case["expect_write"] == "optional"
