"""Данные виджетов (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService


class Widgets(BaseService):
    async def data(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}
