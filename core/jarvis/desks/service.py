"""Столы — страницы с виджетами (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService


class Desks(BaseService):
    def list(self) -> list[dict[str, Any]]:
        return []

    def open(self, desk_id_or_name: str) -> dict[str, Any] | None:
        return None
