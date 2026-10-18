"""Фикстуры тестов команд: сервис команд на платформе-заглушке с привязанным циклом и списком «Пуска»."""

import asyncio
from typing import Any

import pytest

from jarvis.commands.service import Commands
from jarvis.context import AppContext


@pytest.fixture
async def cmds(ctx: AppContext) -> Commands:
    """Сервис команд: цикл привязан (события доходят сразу), программы «Пуска» прочитаны."""
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    service: Any = ctx.commands
    await service.refresh_apps()
    return service


def calls(ctx: AppContext, name: str) -> list[tuple[Any, ...]]:
    """Аргументы вызовов платформы-заглушки с этим именем."""
    return [args for call, args in ctx.platform.calls if call == name]  # type: ignore[attr-defined]
