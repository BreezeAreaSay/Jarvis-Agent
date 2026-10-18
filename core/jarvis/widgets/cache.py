"""Кэш данных виджетов: свежие данные живут TTL секунд, после ошибки сети отдаются устаревшие.

Один запрос к сервису на ключ одновременно: если два виджета просят погоду разом, второй дождётся первого.
После неудачи ключ какое-то время не дёргает сеть (ERROR_TTL) — интерфейс может опрашивать часто.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

ERROR_TTL = 30.0  # сколько секунд помнить ошибку сервиса и не повторять запрос
STALE_MAX = 24 * 3600.0  # устаревшие данные лучше, чем ничего, — но не старше суток


class WidgetError(Exception):
    """Данные получить не удалось; текст — для человека."""


@dataclass
class _Entry:
    value: Any
    at: float


class TtlCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._values: dict[str, _Entry] = {}
        self._errors: dict[str, tuple[float, WidgetError]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def peek(self, key: str) -> Any:
        entry = self._values.get(key)
        return entry.value if entry else None

    def put(self, key: str, value: Any) -> None:
        self._values[key] = _Entry(value, self.clock())
        self._errors.pop(key, None)

    def clear(self) -> None:
        self._values.clear()
        self._errors.clear()

    async def get(self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        """Вернёт (данные, устарели ли). Нет ни свежих, ни устаревших данных — WidgetError."""
        fresh = self._fresh(key, ttl)
        if fresh is not None:
            return fresh.value, False
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            fresh = self._fresh(key, ttl)  # пока ждали замок, данные мог принести соседний запрос
            if fresh is not None:
                return fresh.value, False
            now = self.clock()
            failed = self._errors.get(key)
            if failed is not None and now - failed[0] < ERROR_TTL:
                return self._stale_or_raise(key, failed[1])
            try:
                value = await fetch()
            except WidgetError as e:
                self._errors[key] = (self.clock(), e)
                return self._stale_or_raise(key, e)
            self.put(key, value)
            return value, False

    def _fresh(self, key: str, ttl: float) -> _Entry | None:
        entry = self._values.get(key)
        if entry is not None and self.clock() - entry.at < ttl:
            return entry
        return None

    def _stale_or_raise(self, key: str, error: WidgetError) -> tuple[Any, bool]:
        entry = self._values.get(key)
        if entry is not None and self.clock() - entry.at < STALE_MAX:
            return entry.value, True
        raise error
