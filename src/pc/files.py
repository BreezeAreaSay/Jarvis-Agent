"""Файлы и открытие целей. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from typing import Literal

from pc.result import Caller, Result

Kind = Literal["file", "folder", "any"]
OpenKind = Literal["app", "folder", "file", "url"]


def find(query: str, kind: Kind = "any", caller: Caller = "user", limit: int = 20) -> Result:
    raise NotImplementedError


def known_folder(name: str) -> str | None:
    raise NotImplementedError


def open_target(target: str, kind: OpenKind | None, caller: Caller) -> Result:
    raise NotImplementedError


def trash(path: str, caller: Caller) -> Result:
    raise NotImplementedError


def read_text(path: str, caller: Caller, max_bytes: int = 65536) -> Result:
    raise NotImplementedError
