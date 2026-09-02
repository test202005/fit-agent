from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


# 本项目统一使用东八区作为业务时区：用户说的「今天」是他本地的今天
LOCAL_TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(LOCAL_TZ)


class FrozenClock:
    """测试用：把时间钉死，"今天"永远是同一天，用例明年跑结果不变。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=LOCAL_TZ)
        self._moment = moment

    def now(self) -> datetime:
        return self._moment.astimezone(LOCAL_TZ)
