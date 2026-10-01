"""Часы: системные и управляемые (для детерминированных тестов и прогонов)."""

import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """Время идёт только по `advance()`."""

    def __init__(self, start: datetime | None = None) -> None:
        self._start = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._elapsed = 0.0

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("время не идёт назад")
        self._elapsed += seconds
