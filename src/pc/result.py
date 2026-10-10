"""Результат действия ПК и тип вызывающего."""

from dataclasses import dataclass
from typing import Any, Literal

Caller = Literal["user", "brain"]


@dataclass(frozen=True)
class Result:
    """ok — успех; text — короткая фраза по-русски для человека; data — данные для мозга или окна."""

    ok: bool
    text: str
    data: Any = None


def ok(text: str, data: Any = None) -> Result:
    return Result(True, text, data)


def fail(text: str, data: Any = None) -> Result:
    return Result(False, text, data)
