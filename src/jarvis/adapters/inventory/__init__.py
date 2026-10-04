"""Инвентарь компьютера: приложения и известные папки для прямых команд (ADR 0030).

Создаёт только composition root; Router и инструмент запуска видят его через порт `Inventory`.
"""

import sys

from jarvis.adapters.inventory.posix import PosixInventory
from jarvis.adapters.inventory.static import StaticInventory
from jarvis.adapters.inventory.windows import WindowsInventory
from jarvis.ports.inventory import Inventory


def system_inventory() -> Inventory:
    """Инвентарь этого компьютера; читается лениво, при первом обращении."""
    return WindowsInventory() if sys.platform == "win32" else PosixInventory()


__all__ = ["PosixInventory", "StaticInventory", "WindowsInventory", "system_inventory"]
