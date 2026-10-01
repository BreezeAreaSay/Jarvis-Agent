"""Реестр инструментов: регистрация, поиск, список определений. Больше ничего: права, исполнение,
повторы и трасса — не его дело."""

from collections.abc import Iterable

from jarvis.domain.errors import ToolNotFound
from jarvis.domain.tools import ToolDefinition, ToolId
from jarvis.ports.tools import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[ToolId, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        tool_id = tool.definition.id
        if tool_id in self._tools:
            raise ValueError(f"инструмент {tool_id} уже зарегистрирован")
        self._tools[tool_id] = tool

    def get(self, tool_id: str) -> Tool:
        tool = self._tools.get(ToolId(tool_id))
        if tool is None:
            raise ToolNotFound(f"нет инструмента {tool_id}", tool_id=tool_id)
        return tool

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[key].definition for key in sorted(self._tools)]
