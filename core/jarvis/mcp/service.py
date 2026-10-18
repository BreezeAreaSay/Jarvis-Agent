"""Подключения MCP (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService
from jarvis.services import UserFacingError


class Mcp(BaseService):
    def tools(self) -> list[dict[str, Any]]:
        return []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        raise UserFacingError("MCP ещё не подключён")
