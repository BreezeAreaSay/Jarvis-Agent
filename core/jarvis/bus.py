"""Шина событий ядра.

publish() можно звать из любого потока (голос, платформа, плагины): событие уходит в цикл asyncio и
раздаётся подписчикам — WebSocket-клиентам интерфейса и внутренним обработчикам (on()).
Типы событий и их данные описаны в docs/CONTRACTS.md.
"""

import asyncio
import contextlib
import fnmatch
import inspect
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts}


class Subscription:
    """Очередь событий одного подписчика: медленный подписчик теряет старые события, а не тормозит шину."""

    def __init__(self, bus: "EventBus", patterns: tuple[str, ...], maxsize: int) -> None:
        self._bus = bus
        self.patterns = patterns
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

    def matches(self, event_type: str) -> bool:
        return any(fnmatch.fnmatchcase(event_type, p) for p in self.patterns)

    def put(self, event: Event) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(event)

    async def get(self) -> Event:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._remove(self)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self._handlers: list[tuple[str, Handler]] = []
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def publish(self, type: str, data: dict[str, Any] | None = None) -> None:
        """Опубликовать событие. Безопасно из любого потока; без цикла — только внутренние обработчики."""
        event = Event(type, data or {})
        loop = self._loop
        if loop is None or loop.is_closed():
            self._deliver_sync(event)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(event)
        else:
            loop.call_soon_threadsafe(self._deliver, event)

    def subscribe(self, *patterns: str, maxsize: int = 500) -> Subscription:
        """Подписка на события по шаблонам fnmatch ("voice.*", "chat.*"); без шаблонов — на всё."""
        sub = Subscription(self, patterns or ("*",), maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def on(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """Внутренний обработчик (синхронный или async). Возвращает функцию отписки."""
        entry = (pattern, handler)
        with self._lock:
            self._handlers.append(entry)

        def off() -> None:
            with self._lock:
                if entry in self._handlers:
                    self._handlers.remove(entry)

        return off

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def _deliver(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs)
            handlers = list(self._handlers)
        for sub in subs:
            if sub.matches(event.type):
                sub.put(event)
        for pattern, handler in handlers:
            if fnmatch.fnmatchcase(event.type, pattern):
                self._call(handler, event)

    def _deliver_sync(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for pattern, handler in handlers:
            if fnmatch.fnmatchcase(event.type, pattern):
                try:
                    result = handler(event)
                    if inspect.isawaitable(result):
                        # без цикла корутину некому выполнить — закрываем, чтобы не было предупреждения
                        close = getattr(result, "close", None)
                        if close:
                            close()
                except Exception:
                    log.exception("обработчик события %s упал", event.type)

    def _call(self, handler: Handler, event: Event) -> None:
        try:
            result = handler(event)
        except Exception:
            log.exception("обработчик события %s упал", event.type)
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            task.add_done_callback(_log_task_error)


def _log_task_error(task: "asyncio.Future[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("асинхронный обработчик события упал", exc_info=exc)
