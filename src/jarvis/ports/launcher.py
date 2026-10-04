"""Как открыть приложение, адрес или папку средствами ОС (ADR 0030).

Инструменты запуска получают способ открыть цель от сборки приложения: на компьютере — системный
(`ShellExecute`, `xdg-open`), в тестах и eval — записывающий, который ничего не запускает.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from jarvis.domain.inventory import AppKind


@dataclass(frozen=True)
class LaunchTarget:
    kind: Literal["app", "url", "folder"]
    value: str  # ярлык или исполняемый файл из инвентаря, нормализованный адрес, канонический путь
    app_kind: AppKind | None = None


Launcher = Callable[[LaunchTarget], None]
