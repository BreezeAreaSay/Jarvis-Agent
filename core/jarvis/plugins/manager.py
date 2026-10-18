"""Менеджер плагинов (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService
from jarvis.services import UserFacingError


class Plugins(BaseService):
    def list(self) -> list[dict[str, Any]]:
        return []

    async def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        raise UserFacingError("Плагины ещё не подключены")

    async def action(self, plugin_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        raise UserFacingError("Плагины ещё не подключены")
