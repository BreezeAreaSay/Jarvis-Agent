"""Фикстуры и заглушки тестов плагинов.

pctx — контекст ядра на платформе-заглушке с привязанным циклом: настоящие настройки, секреты, шина, база,
подтверждения и менеджер плагинов; голос, синтез речи, ИИ, заметки и команды — заглушки из этого файла.
Контекст собирается без чужих сервисов, чтобы тесты плагинов не зависели от их запуска; API целиком
проверяется через общую фикстуру client. pevents — журнал событий шины pctx.
"""

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.bus import EventBus
from jarvis.confirm import Confirmations
from jarvis.context import AppContext
from jarvis.db import Database
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.manager import Plugins
from jarvis.services import DynamicCommand, DynamicProvider
from jarvis.settings import Secrets, SettingsStore
from tests.conftest import EventLog

RATE = 16000


class FakeCommands:
    """Сервис команд: запоминает провайдеры динамических команд и число обновлений."""

    def __init__(self) -> None:
        self.providers: dict[str, DynamicProvider] = {}
        self.refreshes = 0

    def register_provider(self, name: str, provider: DynamicProvider) -> None:
        self.providers[name] = provider

    def refresh_dynamic(self) -> None:
        self.refreshes += 1

    def dynamic(self) -> list[DynamicCommand]:
        return [command for provider in self.providers.values() for command in provider()]

    def get(self, command_id: str) -> DynamicCommand:
        return next(c for c in self.dynamic() if c.id == command_id)

    def by_phrase(self, phrase: str) -> DynamicCommand:
        return next(c for c in self.dynamic() if phrase in c.phrases)

    def phrases(self) -> set[str]:
        return {p for c in self.dynamic() for p in c.phrases}


class FakeVoice:
    state = "idle"

    def __init__(self) -> None:
        self.phrases: list[str] = []
        self.empty_delay = 0.01  # сколько «слушать тишину», когда фразы кончились
        self.captures: list[float] = []
        self.on_capture: Callable[[float], None] | None = None
        self.transcribed: list[tuple[int, int, str]] = []
        self.transcribe_fn: Callable[[np.ndarray, int, str], str] | None = None

    async def capture_text(self, timeout: float = 15.0, *, punct: bool = True) -> str:
        self.captures.append(timeout)
        if self.on_capture is not None:
            self.on_capture(timeout)
        if self.phrases:
            await asyncio.sleep(0.01)
            return self.phrases.pop(0)
        await asyncio.sleep(self.empty_delay)
        return ""

    async def transcribe(self, samples: Any, rate: int, *, language: str = "ru") -> str:
        self.transcribed.append((len(samples), rate, language))
        return self.transcribe_fn(samples, rate, language) if self.transcribe_fn else ""


class FakeTts:
    def __init__(self, seconds: float = 0.1, rate: int = RATE) -> None:
        self.seconds = seconds
        self.rate = rate
        self.play_delay = 0.0
        self.speaking = False
        self.said: list[str] = []
        self.synthesized: list[str] = []
        self.played: list[int] = []
        self.silenced = 0
        self._cut: asyncio.Event | None = None

    async def say(self, text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        self.said.append(text)

    def silence(self) -> None:
        self.silenced += 1
        if self._cut is not None:
            self._cut.set()

    async def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        self.synthesized.append(text)
        t = np.arange(int(self.rate * self.seconds)) / self.rate
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), self.rate

    async def play(self, samples: Any, rate: int, *, wait: bool = True) -> None:
        self.played.append(len(samples))
        self._cut = asyncio.Event()
        if self.play_delay:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._cut.wait(), self.play_delay)


class FakeAi:
    def __init__(self, configured: bool = False, reply: Any = "") -> None:
        self.configured = configured
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        self.calls.append({"messages": messages, "json_schema": json_schema})
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply(messages) if callable(self.reply) else self.reply


class FakeNotes:
    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []

    def note_add(self, title: str, body: str = "") -> dict[str, Any]:
        item = {"id": f"n{len(self.notes) + 1}", "title": title, "body": body}
        self.notes.append(item)
        return item

    def task_add(self, title: str, due: float | None = None) -> dict[str, Any]:
        item = {"id": f"t{len(self.tasks) + 1}", "title": title, "due": due}
        self.tasks.append(item)
        return item


class FakeSource:
    """Источник звука: отдаёт готовые сэмплы блоками и (hold) ждёт остановки, как настоящий."""

    def __init__(
        self,
        samples: np.ndarray,
        *,
        block: int = 1600,
        pace: float = 0.002,
        hold: bool = True,
        error: str = "",
    ) -> None:
        self.name = "тестовый звук"
        self.samples = samples.astype(np.float32)
        self.block = block
        self.pace = pace
        self.hold = hold
        self.error = error

    def frames(self, stop: threading.Event) -> Iterator[np.ndarray]:
        for start in range(0, len(self.samples), self.block):
            if stop.is_set():
                return
            yield self.samples[start : start + self.block]
            if self.pace:
                time.sleep(self.pace)
        if self.error:
            raise RuntimeError(self.error)
        if self.hold:
            stop.wait()


def speech(seconds: float, amp: float = 0.3, rate: int = RATE) -> np.ndarray:
    """Похоже на речь для детектора по энергии: «слоги» — 200 мс тона и 50 мс тишины."""
    t = np.arange(int(seconds * rate)) / rate
    gate = ((t % 0.25) < 0.2).astype(np.float32)
    return (amp * np.sin(2 * np.pi * 220 * t) * gate).astype(np.float32)


def silence(seconds: float, rate: int = RATE) -> np.ndarray:
    return np.zeros(int(seconds * rate), dtype=np.float32)


async def wait_until(condition: Callable[[], Any], timeout: float = 3.0, step: float = 0.01) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("условие не выполнилось вовремя")
        await asyncio.sleep(step)


def manager(ctx: AppContext) -> Plugins:
    plugins = ctx.plugins
    assert isinstance(plugins, Plugins)
    return plugins


async def enable(ctx: AppContext, plugin_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Запустить менеджер (если ещё нет) и включить плагин."""
    plugins = manager(ctx)
    if not plugins._started:
        await plugins.start()
    return await plugins.update(plugin_id, enabled=True, config=config)


@pytest.fixture
async def pctx(data_dir: Path) -> AsyncIterator[AppContext]:
    bus = EventBus()
    context = AppContext(
        SettingsStore(data_dir / "settings.json", bus),
        Secrets(data_dir / "secrets.dat", bus),
        bus,
        Database(data_dir / "jarvis.db"),
        FakePlatform(),
    )
    context.loop = asyncio.get_running_loop()
    bus.bind_loop(context.loop)
    context.confirmations = Confirmations(context)
    context.commands = FakeCommands()  # type: ignore[assignment]
    context.voice = FakeVoice()  # type: ignore[assignment]
    context.tts = FakeTts()  # type: ignore[assignment]
    context.ai = FakeAi()  # type: ignore[assignment]
    context.notes = FakeNotes()  # type: ignore[assignment]
    context.plugins = Plugins(context)
    yield context
    await context.plugins.stop()
    await context.shutdown_pools()
    context.db.close()


@pytest.fixture
def pevents(pctx: AppContext) -> EventLog:
    log = EventLog()
    pctx.bus.on("*", log)
    return log
