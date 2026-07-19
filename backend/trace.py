from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


class Tracer:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        self.write_failed = False

    def emit(
        self,
        trace_id: str,
        event: str,
        payload: dict[str, Any],
        node: str = "router",
    ) -> None:
        record = {
            "trace_id": trace_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "node": node,
            "event": event,
            "payload": payload,
        }
        self.events.append(record)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self.write_failed = True
            LOGGER.error(
                json.dumps(
                    {"event": "trace_write_failed", "trace_id": trace_id},
                    ensure_ascii=False,
                )
            )
