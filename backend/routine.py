"""训练安排：按顺序轮换执行的训练单元，含 active 与 paused 两种状态。

用户日常看到的是 active 视图，系统存的是全量顺序；paused 单元保留原位置，恢复时回到原处。
v1 写协议：整体替换，提交的 order 必须是全部单元（含 paused）的一个排列。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


STATUSES = ("active", "paused")


class RoutineProtocolError(Exception):
    """写协议校验失败。不写入，错误码与说明原样回给模型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_order(units: list[dict[str, Any]], order: Any) -> list[str]:
    """v1 协议：order 必须是全部单元的排列。只说明规则，不列出缺哪几项。"""
    if not isinstance(order, list) or not all(isinstance(u, str) for u in order):
        raise RoutineProtocolError("invalid_order", "order 必须是训练单元 unit_id 的字符串列表")
    known = [unit["unit_id"] for unit in units]
    if any(unit_id not in known for unit_id in order):
        raise RoutineProtocolError("unknown_unit", "order 中包含不存在的训练单元")
    if len(set(order)) != len(order):
        raise RoutineProtocolError("duplicate_unit", "order 中有重复的训练单元")
    if len(order) != len(known):
        raise RoutineProtocolError("incomplete_order", "order 必须包含全部训练单元的完整顺序")
    return list(order)


class RoutineStore(Protocol):
    def seed(self, user_id: str, units: list[dict[str, Any]]) -> None: ...
    def read(self, user_id: str) -> list[dict[str, Any]]: ...
    def replace_order(self, user_id: str, order: list[str], trace_id: str) -> None: ...


def _check_units(units: list[dict[str, Any]]) -> None:
    ids = [unit["unit_id"] for unit in units]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate unit_id in seed")
    for unit in units:
        if unit["status"] not in STATUSES:
            raise ValueError(f"invalid status {unit['status']!r}")


class FakeRoutineStore:
    """内存实现：Stub 回归与单测用。"""

    def __init__(self) -> None:
        self.units: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[dict[str, Any]] = []

    def seed(self, user_id: str, units: list[dict[str, Any]]) -> None:
        _check_units(units)
        self.units[user_id] = [
            {"unit_id": u["unit_id"], "name": u["name"], "status": u["status"]} for u in units
        ]

    def read(self, user_id: str) -> list[dict[str, Any]]:
        return [dict(unit) for unit in self.units.get(user_id, [])]

    def replace_order(self, user_id: str, order: list[str], trace_id: str) -> None:
        by_id = {unit["unit_id"]: unit for unit in self.units.get(user_id, [])}
        self.units[user_id] = [by_id[unit_id] for unit_id in order]
        self.writes.append({"user_id": user_id, "order": list(order), "trace_id": trace_id})


class SQLiteRoutineStore:
    """与训练记录同库；position 为全量顺序中的位置（从 1 开始）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS training_routine_units (
                    user_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    PRIMARY KEY(user_id, unit_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def seed(self, user_id: str, units: list[dict[str, Any]]) -> None:
        _check_units(units)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM training_routine_units WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO training_routine_units VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (user_id, u["unit_id"], u["name"], u["status"], i, now, "t-seed")
                    for i, u in enumerate(units, start=1)
                ],
            )

    def read(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT unit_id, name, status FROM training_routine_units "
                "WHERE user_id = ? ORDER BY position",
                (user_id,),
            ).fetchall()
        return [{"unit_id": r[0], "name": r[1], "status": r[2]} for r in rows]

    def replace_order(self, user_id: str, order: list[str], trace_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.executemany(
                "UPDATE training_routine_units SET position = ?, updated_at = ?, trace_id = ? "
                "WHERE user_id = ? AND unit_id = ?",
                [(i, now, trace_id, user_id, unit_id) for i, unit_id in enumerate(order, start=1)],
            )
