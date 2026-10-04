"""Порт инвентаря: приложения и известные папки этого компьютера (ADR 0030).

Router разрешает по нему сущности прямых команд, инструмент запуска — ID приложения в то, что открыть.
Адаптер может читать диск и реестр при первом обращении и держать результат в памяти.
"""

from collections.abc import Sequence
from typing import Protocol

from jarvis.domain.inventory import AppEntry, KnownFolder


class Inventory(Protocol):
    def apps(self) -> Sequence[AppEntry]:
        """Приложения, которые можно запустить (ярлыки меню «Пуск», зарегистрированные программы)."""
        ...

    def default_browser(self) -> AppEntry | None:
        """Браузер по умолчанию, если его удаётся определить."""
        ...

    def known_folder(self, folder: KnownFolder) -> str | None:
        """Абсолютный путь известной папки, если она есть на этом компьютере."""
        ...
