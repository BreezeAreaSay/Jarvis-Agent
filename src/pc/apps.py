"""Инвентарь приложений. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pc.result import Caller, Result

THRESHOLD = 85.0


@dataclass(frozen=True)
class App:
    name: str
    app_id: str


def inventory() -> list[App]:
    raise NotImplementedError


def refresh() -> list[App]:
    raise NotImplementedError


def enable_auto_refresh(on: bool) -> None:
    raise NotImplementedError


def resolve(name: str) -> tuple[App | None, float]:
    raise NotImplementedError


def top(name: str, n: int = 5) -> list[tuple[App, float]]:
    raise NotImplementedError


def list_apps_result(query: str | None, caller: Caller) -> Result:
    raise NotImplementedError


def run_sta(fn: Callable[..., Any], *args: Any) -> Any:
    raise NotImplementedError
