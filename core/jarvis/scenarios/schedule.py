"""Время сценариев: когда в последний раз «наступало» расписание и выполняются ли условия.

Всё считается по местному времени компьютера; now — unix-секунды (сервис подставляет свои часы, тесты — свои).
"""

from datetime import datetime, time, timedelta
from typing import Any

from jarvis.scenarios.model import parse_hhmm


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def last_occurrence(at: str, weekdays: list[int] | None, now: float) -> float | None:
    """Последний момент расписания «в at по дням weekdays», не позже now. None — время не разобрать."""
    hm = parse_hhmm(at)
    if hm is None:
        return None
    today = local(now).date()
    for back in range(8):
        day = today - timedelta(days=back)
        if weekdays and day.weekday() not in weekdays:
            continue
        ts = datetime.combine(day, time(hm[0], hm[1])).timestamp()
        if ts <= now:
            return ts
    return None


def _minutes(value: str) -> int | None:
    hm = parse_hhmm(value)
    return hm[0] * 60 + hm[1] if hm else None


def conditions_ok(conditions: dict[str, Any] | None, now: float) -> bool:
    """Условия сценария: дни недели и окно времени (может переходить через полночь: 22:00–06:00)."""
    cond = conditions or {}
    moment = local(now)
    days = cond.get("weekdays") or []
    if days and moment.weekday() not in days:
        return False
    start = _minutes(cond.get("time_from") or "")
    end = _minutes(cond.get("time_to") or "")
    if start is None and end is None:
        return True
    current = moment.hour * 60 + moment.minute
    if start is not None and end is not None:
        return start <= current <= end if start <= end else (current >= start or current <= end)
    if start is not None:
        return current >= start
    return end is not None and current <= end


def day_bounds(now: float) -> tuple[float, float]:
    """Начало сегодняшнего и завтрашнего дня (unix-секунды)."""
    today = local(now).date()
    start = datetime.combine(today, time()).timestamp()
    end = datetime.combine(today + timedelta(days=1), time()).timestamp()
    return start, end
