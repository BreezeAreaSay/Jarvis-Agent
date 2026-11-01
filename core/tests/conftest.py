"""Общие фикстуры: временный каталог данных, контекст ядра на платформе-заглушке, HTTP-клиент API."""

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis import paths
from jarvis.app import build_context
from jarvis.bus import Event
from jarvis.context import AppContext

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _no_real_windows_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """На Windows тесты не говорят настоящими голосами Windows: синтез WinRT и SAPI в потоках тестов
    медленный, сыплет исключениями COM и может зависнуть. «Голоса Windows» в тестах — как вне Windows."""
    if sys.platform != "win32":
        return
    from jarvis.voice.engines import windows
    from jarvis.voice.engines.base import TtsEngineError

    off = "голоса Windows в тестах выключены"

    async def synthesize(self: Any, text: str, opts: Any) -> Any:
        raise TtsEngineError(off)

    async def voices(self: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(windows.WindowsEngine, "available", lambda self: off)
    monkeypatch.setattr(windows.WindowsEngine, "synthesize", synthesize)
    monkeypatch.setattr(windows.WindowsEngine, "voices", voices)


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    paths.set_data_dir(tmp_path / "data")
    yield paths.data_dir()
    paths.set_data_dir(None)


@pytest.fixture
def ctx(data_dir: Path) -> Iterator[AppContext]:
    """Контекст без запуска сервисов (start() не вызывается) — для юнит-тестов сервисов."""
    context = build_context("fake")
    yield context
    context.db.close()


@pytest.fixture
async def running_ctx(data_dir: Path) -> AsyncIterator[AppContext]:
    """Контекст с привязанным циклом и запущенными сервисами."""
    from jarvis.app import start_services, stop_services

    context = build_context("fake")
    context.loop = asyncio.get_running_loop()
    context.bus.bind_loop(context.loop)
    await start_services(context)
    yield context
    await stop_services(context)


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    """API целиком (lifespan запускает сервисы) с токеном TOKEN в заголовках."""
    from jarvis.server import create_app

    context = build_context("fake")
    app = create_app(context, TOKEN)
    with TestClient(app, headers={"X-Jarvis-Token": TOKEN}) as c:
        c.ctx = context  # type: ignore[attr-defined]
        yield c


class EventLog:
    """Собирает события шины: events.of("toast") → список data."""

    def __init__(self) -> None:
        self.items: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.items.append(event)

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [e.data for e in self.items if e.type == type_]

    def types(self) -> list[str]:
        return [e.type for e in self.items]


@pytest.fixture
def events(ctx: AppContext) -> EventLog:
    log = EventLog()
    ctx.bus.on("*", log)
    return log
