"""Фильтр всего, что уходит мозгу. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from typing import Any


def redact_title(title: str) -> str:
    raise NotImplementedError


def redact_path(path: str) -> str | None:
    raise NotImplementedError


def redact_text(text: str) -> str:
    raise NotImplementedError


def redact(value: Any) -> Any:
    raise NotImplementedError
