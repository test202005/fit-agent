from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


DEFAULT_USER_ID = "demo-user"
RECORD_FIELDS = (
    "exercise",
    "weight_kg",
    "sets",
    "reps",
    "duration_min",
    "distance_km",
)


@dataclass(frozen=True)
class StorageWriteResult:
    written_ids: list[str]
    idempotent_replay: bool = False

    # 保持旧的 Storage 单测可以继续用 len(result) 和 result[index]。
    def __len__(self) -> int:
        return len(self.written_ids)

    def __getitem__(self, index: int) -> str:
        return self.written_ids[index]


class StorageClient(Protocol):
    def append(
        self,
        records: list[dict[str, Any]],
        trace_id: str,
        now: datetime,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> StorageWriteResult: ...

    def read_all(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]: ...


def _build_rows(
    records: list[dict[str, Any]],
    trace_id: str,
    started_at: datetime,
    user_id: str = DEFAULT_USER_ID,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """started_at 由调用方传入，不在此处取系统时间——业务时间必须可注入。"""
    created_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for record in records:
        row = {
            "id": f"r-{uuid.uuid4().hex}",
            "user_id": user_id,
            "request_id": request_id,
            "ts": started_at.isoformat(),
            "created_at": created_at,
            "state": record["state"],
            "trace_id": trace_id,
        }
        row.update({field: record.get(field) for field in RECORD_FIELDS})
        rows.append(row)
    return rows


class JsonlStorage:
    """一条记录一行。用于教学与对照，不模拟 SQLite 的事务和幂等约束。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        records: list[dict[str, Any]],
        trace_id: str,
        now: datetime,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> StorageWriteResult:
        if not records:
            return StorageWriteResult([])
        rows = _build_rows(records, trace_id, now, user_id, request_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        return StorageWriteResult([row["id"] for row in rows])

    def read_all(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            row
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for row in [json.loads(line)]
            if row.get("user_id", DEFAULT_USER_ID) == user_id
        ]


class FakeStorage:
    """内存实现，测试用。让写入断言不依赖文件系统。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(
        self,
        records: list[dict[str, Any]],
        trace_id: str,
        now: datetime,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> StorageWriteResult:
        if not records:
            return StorageWriteResult([])
        rows = _build_rows(records, trace_id, now, user_id, request_id)
        self.rows.extend(rows)
        return StorageWriteResult([row["id"] for row in rows])

    def read_all(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        return [
            row for row in self.rows
            if row.get("user_id", DEFAULT_USER_ID) == user_id
        ]


class SQLiteStorage:
    """SQLite 默认实现：真实持久化、用户范围、事务和请求幂等。"""

    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workout_records (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    exercise TEXT,
                    weight_kg REAL,
                    sets INTEGER,
                    reps INTEGER,
                    duration_min REAL,
                    distance_km REAL
                );
                CREATE TABLE IF NOT EXISTS write_requests (
                    user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS ix_workout_records_user_ts
                ON workout_records(user_id, ts);
                CREATE INDEX IF NOT EXISTS ix_workout_records_user_request
                ON workout_records(user_id, request_id);
                """
            )

    def append(
        self,
        records: list[dict[str, Any]],
        trace_id: str,
        now: datetime,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> StorageWriteResult:
        if not records:
            return StorageWriteResult([])
        request_id = request_id or f"server-{uuid.uuid4().hex}"
        rows = _build_rows(records, trace_id, now, user_id, request_id)
        created_at = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO write_requests(user_id, request_id, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, request_id, created_at),
            )
            if inserted.rowcount == 0:
                existing = connection.execute(
                    """
                    SELECT id FROM workout_records
                    WHERE user_id = ? AND request_id = ?
                    ORDER BY rowid
                    """,
                    (user_id, request_id),
                ).fetchall()
                connection.commit()
                return StorageWriteResult(
                    [row["id"] for row in existing], idempotent_replay=True
                )

            connection.executemany(
                """
                INSERT INTO workout_records(
                    id, user_id, request_id, trace_id, ts, created_at, state,
                    exercise, weight_kg, sets, reps, duration_min, distance_km
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["id"], row["user_id"], row["request_id"], row["trace_id"],
                        row["ts"], row["created_at"], row["state"], row["exercise"],
                        row["weight_kg"], row["sets"], row["reps"], row["duration_min"],
                        row["distance_km"],
                    )
                    for row in rows
                ],
            )
            connection.commit()
            return StorageWriteResult([row["id"] for row in rows])
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_all(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM workout_records WHERE user_id = ? ORDER BY rowid",
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
