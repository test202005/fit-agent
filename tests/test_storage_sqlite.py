import sqlite3

import pytest

from backend.clock import FrozenClock
from backend.storage import SQLiteStorage


NOW = FrozenClock("2026-09-03T20:00:00+08:00").now()


def record(exercise="卧推"):
    return {"exercise": exercise, "weight_kg": 60, "state": "complete"}


def test_sqlite_roundtrip_and_restart(tmp_path):
    path = tmp_path / "records.sqlite3"
    first = SQLiteStorage(path)
    result = first.append([record()], "trace-1", NOW, "user-a", "req-1")

    assert result.idempotent_replay is False
    assert len(result.written_ids) == 1
    assert first.read_all("user-a")[0]["exercise"] == "卧推"
    assert first.read_all("user-a")[0]["created_at"]

    restarted = SQLiteStorage(path)
    rows = restarted.read_all("user-a")
    assert len(rows) == 1
    assert rows[0]["id"] == result.written_ids[0]


def test_sqlite_user_isolation(tmp_path):
    storage = SQLiteStorage(tmp_path / "records.sqlite3")
    storage.append([record()], "trace-a", NOW, "user-a", "req-a")
    storage.append([record("深蹲")], "trace-b", NOW, "user-b", "req-b")

    assert [row["exercise"] for row in storage.read_all("user-a")] == ["卧推"]
    assert [row["exercise"] for row in storage.read_all("user-b")] == ["深蹲"]


def test_sqlite_same_request_replays_all_rows_without_duplicates(tmp_path):
    storage = SQLiteStorage(tmp_path / "records.sqlite3")
    records = [record("卧推"), record("划船")]
    first = storage.append(records, "trace-1", NOW, "user-a", "req-1")
    replay = storage.append(records, "trace-2", NOW, "user-a", "req-1")

    assert replay.idempotent_replay is True
    assert replay.written_ids == first.written_ids
    assert len(storage.read_all("user-a")) == 2


def test_sqlite_ids_are_unique_with_same_business_time(tmp_path):
    storage = SQLiteStorage(tmp_path / "records.sqlite3")
    first = storage.append([record()], "trace-1", NOW, "user-a", "req-1")
    second = storage.append([record()], "trace-2", NOW, "user-a", "req-2")

    assert first.written_ids[0] != second.written_ids[0]
    assert len(storage.read_all("user-a")) == 2


def test_sqlite_batch_rolls_back_when_one_insert_fails(tmp_path, monkeypatch):
    storage = SQLiteStorage(tmp_path / "records.sqlite3")
    original = __import__("backend.storage", fromlist=["_build_rows"])._build_rows

    def duplicate_rows(records, trace_id, started_at, user_id="demo-user", request_id=None):
        rows = original(records, trace_id, started_at, user_id, request_id)
        rows[1]["id"] = rows[0]["id"]
        return rows

    monkeypatch.setattr("backend.storage._build_rows", duplicate_rows)
    with pytest.raises(sqlite3.IntegrityError):
        storage.append([record("卧推"), record("划船")], "trace-1", NOW, "user-a", "req-1")

    assert storage.read_all("user-a") == []
    with storage._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_requests").fetchone()[0] == 0
