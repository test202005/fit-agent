"""每周回顾（V11.1）：周统计全部由代码从训练记录计算，模型只负责说话。

口径见 docs/prd-v11-weekly-review-and-action-guide.md 第 2.2 节：
周一到周日；按记录时间归周（记录没有训练日期字段）；只统计完整记录。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from backend.action_lib_v2 import infer_parts
from backend.storage import StorageClient


WEEK_OFFSETS = (0, -1)
# WHO 2020：每周 150～300 分钟中等强度有氧、至少 2 天力量训练；ACSM 2026：每周至少两次练到全部主要肌群
AEROBIC_MIN_TARGET = (150, 300)
STRENGTH_DAYS_TARGET = 2
MAJOR_PARTS = ("下肢", "臀", "胸", "背", "肩", "核心")


def week_range(now: datetime, offset: int) -> tuple[date, date]:
    monday = now.date() - timedelta(days=now.weekday()) + timedelta(weeks=offset)
    return monday, monday + timedelta(days=6)


def _record_date(row: dict[str, Any]) -> date | None:
    try:
        return datetime.fromisoformat(row["ts"]).date()
    except (KeyError, TypeError, ValueError):
        return None


def summarize(storage: StorageClient, user_id: str, now: datetime, offset: int) -> dict[str, Any]:
    if offset not in WEEK_OFFSETS:
        raise ValueError("week_offset 只支持 0（本周）或 -1（上周）")
    start, end = week_range(now, offset)
    rows = [r for r in storage.read_all(user_id) if (d := _record_date(r)) is not None and start <= d <= end]
    complete = [r for r in rows if r.get("state") == "complete"]
    days = sorted({_record_date(r) for r in complete})
    strength_days = sorted({_record_date(r) for r in complete
                            if any(r.get(f) is not None for f in ("sets", "reps", "weight_kg"))})
    minutes = round(sum(r.get("duration_min") or 0 for r in complete), 1)

    parts: Counter[str] = Counter()
    unrecognized: list[str] = []
    for row in complete:
        inferred = infer_parts(row.get("exercise") or "")
        if inferred is None:
            unrecognized.append(row.get("exercise") or "")
        else:
            parts.update(inferred)
    return {
        "ok": True,
        "week": {"offset": offset, "start": start.isoformat(), "end": end.isoformat(),
                 "label": "本周" if offset == 0 else "上周"},
        "training_days": len(days),
        "record_count": len(complete),
        "incomplete_count": len(rows) - len(complete),
        "strength_days": len(strength_days),
        "timed_minutes": minutes,
        "parts": dict(parts),
        "missing_major_parts": [p for p in MAJOR_PARTS if p not in parts],
        "unrecognized_exercises": sorted(set(unrecognized)),
        "records": [{"date": _record_date(r).isoformat(), "exercise": r.get("exercise"),
                     **{f: r.get(f) for f in ("sets", "reps", "weight_kg", "duration_min", "distance_km")
                        if r.get(f) is not None}} for r in complete][:30],
        "guideline": {
            "strength_days_target": STRENGTH_DAYS_TARGET,
            "strength_days_short": max(0, STRENGTH_DAYS_TARGET - len(strength_days)),
            "aerobic_minutes_target": list(AEROBIC_MIN_TARGET),
            "timed_minutes_short": max(0, AEROBIC_MIN_TARGET[0] - minutes),
            "source": "WHO 2020 身体活动指南；ACSM 2026 抗阻训练指南",
        },
        "caveats": [
            "timed_minutes 只统计记录了时长的训练，不等于中等强度有氧分钟",
            "按记录时间归周：记录没有训练日期，补记以前的训练会算进记录当天所在的周",
            "部位按动作名称推断，推不出的列在 unrecognized_exercises",
        ],
    }
