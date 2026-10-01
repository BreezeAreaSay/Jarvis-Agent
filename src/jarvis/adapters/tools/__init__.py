"""Встроенные инструменты HOST (ADR 0022): только чтение. Исполняет их только Tool Runtime."""

from jarvis.adapters.tools._host import HOST, OS_FAMILY
from jarvis.adapters.tools.filesystem import ListTool, ReadTextTool, SearchTool, StatTool
from jarvis.adapters.tools.processes import ProcessListTool
from jarvis.adapters.tools.system import CwdTool
from jarvis.ports.tools import Tool


def builtin_tools() -> list[Tool]:
    return [CwdTool(), ListTool(), StatTool(), SearchTool(), ReadTextTool(), ProcessListTool()]


__all__ = ["HOST", "OS_FAMILY", "builtin_tools"]
