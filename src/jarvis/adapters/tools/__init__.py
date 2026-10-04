"""Встроенные инструменты HOST (ADR 0022, ADR 0030). Исполняет их только Tool Runtime.

`builtin_tools` — только чтение (на них снят бенчмарк модели); `launch_tools` — открыть приложение из
инвентаря, адрес или папку (эффект LAUNCH); `host_tools` — всё вместе, набор по умолчанию.
"""

from jarvis.adapters.tools._host import HOST, OS_FAMILY
from jarvis.adapters.tools.filesystem import ListTool, ReadTextTool, SearchTool, StatTool
from jarvis.adapters.tools.launch import AppLaunchTool, FolderOpenTool, UrlOpenTool, system_launcher
from jarvis.adapters.tools.processes import ProcessListTool
from jarvis.adapters.tools.system import CwdTool
from jarvis.ports.inventory import Inventory
from jarvis.ports.launcher import Launcher
from jarvis.ports.tools import Tool


def builtin_tools() -> list[Tool]:
    return [CwdTool(), ListTool(), StatTool(), SearchTool(), ReadTextTool(), ProcessListTool()]


def launch_tools(inventory: Inventory, launcher: Launcher = system_launcher) -> list[Tool]:
    return [AppLaunchTool(inventory, launcher), UrlOpenTool(launcher), FolderOpenTool(launcher)]


def host_tools(inventory: Inventory, launcher: Launcher = system_launcher) -> list[Tool]:
    return [*builtin_tools(), *launch_tools(inventory, launcher)]


__all__ = [
    "HOST",
    "OS_FAMILY",
    "builtin_tools",
    "host_tools",
    "launch_tools",
    "system_launcher",
]
