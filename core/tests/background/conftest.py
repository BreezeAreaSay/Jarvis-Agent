"""Фикстуры фоновых задач: запущенное ядро, модель по сценарию, хранилище заметок во временной папке."""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.app import build_context, start_services, stop_services
from jarvis.background.service import Background
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, no_sleep
from tests.conftest import EventLog


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "Мои заметки"
    (path / ".obsidian").mkdir(parents=True)
    return path


@pytest.fixture
async def bg(data_dir: Path, vault_dir: Path) -> AsyncIterator[tuple[AppContext, ScriptedLlm, EventLog]]:
    """Ядро с запущенными сервисами, ключом OpenAI и моделью по сценарию."""
    ctx = build_context("fake")
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    ctx.settings.patch({"background": {"vault": str(vault_dir)}})
    ctx.secrets.set("ai_api_key", "sk-test")
    script = ScriptedLlm()
    ctx.ai.set_transport(script.transport)  # type: ignore[attr-defined]
    ctx.ai.llm.sleep = no_sleep  # type: ignore[attr-defined]
    log = EventLog()
    ctx.bus.on("*", log)
    await start_services(ctx)
    yield ctx, script, log
    await stop_services(ctx)


def service(ctx: AppContext) -> Background:
    assert isinstance(ctx.background, Background)
    return ctx.background


async def until(check: Callable[[], Any], timeout: float = 5.0) -> None:
    """Подождать, пока check() станет истинным (фоновая задача работает в своём asyncio.Task)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("не дождался")
        await asyncio.sleep(0.01)
