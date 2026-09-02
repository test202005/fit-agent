from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


RECORD_FIELDS = (
    "exercise",
    "weight_kg",
    "sets",
    "reps",
    "duration_min",
    "distance_km",
)


class StorageClient(Protocol):
    def append(self, records: list[dict[str, Any]], trace_id: str) -> list[str]: ...

    def read_all(self) -> list[dict[str, Any]]: ...


def _build_rows(
    records: list[dict[str, Any]], trace_id: str, started_at: datetime
) -> list[dict[str, Any]]:
    rows = []
    stamp = started_at.strftime("%Y%m%d%H%M%S%f")
    for index, record in enumerate(records, start=1):
        row = {
            "id": f"r-{stamp}-{index:03d}",
            "ts": started_at.isoformat(),
            "state": record["state"],
            "trace_id": trace_id,
        }
        row.update({field: record.get(field) for field in RECORD_FIELDS})
        rows.append(row)
    return rows


class JsonlStorage:
    """一条记录一行。够测即可，换实现不影响上层（见 docs/存储选型与企业实践差异.md）。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, records: list[dict[str, Any]], trace_id: str) -> list[str]:
        if not records:
            return []
        rows = _build_rows(records, trace_id, datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 整行写入，规避写一半的记录
        payload = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        return [row["id"] for row in rows]

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class FakeStorage:
    """内存实现，测试用。让写入断言不依赖文件系统。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, records: list[dict[str, Any]], trace_id: str) -> list[str]:
        if not records:
            return []
        rows = _build_rows(records, trace_id, datetime.now(timezone.utc))
        self.rows.extend(rows)
        return [row["id"] for row in rows]

    def read_all(self) -> list[dict[str, Any]]:
        return list(self.rows)
