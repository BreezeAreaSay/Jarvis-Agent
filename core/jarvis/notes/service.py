"""Заметки, задачи, календарь, напоминания, память (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService


class Notes(BaseService):
    def memory_list(self) -> list[dict[str, Any]]:
        return []

    def memory_add(self, text: str, source: str = "ai") -> dict[str, Any]:
        return {}

    def memory_delete(self, memory_id: str) -> bool:
        return False

    def memory_prompt(self) -> str:
        return ""

    def note_add(self, title: str, body: str = "") -> dict[str, Any]:
        return {}

    def task_add(self, title: str, due: float | None = None) -> dict[str, Any]:
        return {}

    def event_add(self, title: str, start: float, end: float | None = None, **kw: Any) -> dict[str, Any]:
        return {}

    def reminder_add(self, text: str, at: float, repeat: str | None = None) -> dict[str, Any]:
        return {}
