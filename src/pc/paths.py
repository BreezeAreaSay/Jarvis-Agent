"""Канонизация и проверка путей. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from dataclasses import dataclass
from typing import Literal

from pc.result import Caller

Op = Literal["open", "read", "trash", "list"]


class PathDenied(ValueError):
    """Путь нельзя использовать; текст — причина по-русски."""


@dataclass(frozen=True)
class PathCheck:
    ok: bool
    path: str
    reason: str = ""
    confirm: bool = False


def canonical(path: str) -> str:
    raise NotImplementedError


def is_within(path: str, root: str) -> bool:
    raise NotImplementedError


def check(path: str, caller: Caller, op: Op) -> PathCheck:
    raise NotImplementedError


def is_executable_type(path: str) -> bool:
    raise NotImplementedError


def is_secret_file(path: str) -> bool:
    raise NotImplementedError


def hidden_for_brain(path: str) -> bool:
    raise NotImplementedError
