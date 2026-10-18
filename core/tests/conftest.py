"""Общие фикстуры: временный каталог данных, контекст ядра на платформе-заглушке, HTTP-клиент API."""

import asyncio
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
