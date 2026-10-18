"""Фикстуры тестов сценариев: сервис с подменёнными часами, голос в список, данные виджетов без сети."""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.platform.base import SystemStats, WindowInfo
from jarvis.scenarios.service import Scenarios

# 19 октября 2026 — понедельник
MONDAY = datetime(2026, 10, 19)


class Clock:
    """Часы для сервиса: clock() — текущее время, set(…) / advance(…) двигают его."""

    def __init__(self, moment: datetime) -> None:
        self.ts = moment.timestamp()

    def __call__(self) -> float:
        return self.ts

    def set(self, moment: datetime) -> None:
        self.ts = moment.timestamp()

    def advance(self, seconds: float) -> None:
        self.ts += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock(MONDAY.replace(hour=7))


@pytest.fixture
async def svc(ctx: AppContext, clock: Clock) -> Scenarios:
    """Сервис сценариев: цикл привязан (события доходят сразу), часы — clock, программы «Пуска» прочитаны."""
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    service = ctx.scenarios
    service.clock = clock
    ctx.notes.clock = clock  # type: ignore[attr-defined]
    return service


@pytest.fixture
def spoken(ctx: AppContext) -> list[str]:
    said: list[str] = []

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        said.append(text)

    ctx.tts.say = say  # type: ignore[method-assign]
    return said


class Machine:
    """Состояние компьютера для платформы-заглушки: простой, заряд, запущенные программы."""

    def __init__(self, ctx: AppContext) -> None:
        self.platform: Any = ctx.platform
        self.idle = 0.0
        self.battery: float | None = None
        self.charging: bool | None = None
        self.platform.idle_seconds = lambda: self.idle
        self.platform.system_stats = lambda: SystemStats(battery=self.battery, charging=self.charging)

    def open(self, exe: str) -> None:
        self.platform.windows.append(WindowInfo(900 + len(self.platform.windows), exe, exe))

    def close(self, exe: str) -> None:
        self.platform.windows = [w for w in self.platform.windows if w.exe != exe]


@pytest.fixture
def machine(ctx: AppContext) -> Machine:
    return Machine(ctx)


def calls(ctx: AppContext, name: str) -> list[tuple[Any, ...]]:
    """Аргументы вызовов платформы-заглушки с этим именем."""
    return [args for call, args in ctx.platform.calls if call == name]  # type: ignore[attr-defined]


def scenario(**over: Any) -> dict[str, Any]:
    """Тело сценария для create(): по умолчанию — громкость 30 в 08:00 каждый день."""
    body: dict[str, Any] = {
        "name": "Тест",
        "icon": "sunrise",
        "triggers": [{"type": "time", "at": "08:00"}],
        "actions": [{"type": "System.VolumeSet", "value": "30"}],
    }
    body.update(over)
    return body
