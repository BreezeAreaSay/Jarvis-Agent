"""Система: блокировка, питание, буфер обмена, ввод текста. ЗАГОТОВКА: по docs/architecture.md."""

from typing import Literal

from pc.result import Caller, Result

PowerAction = Literal["sleep", "shutdown", "restart"]


def lock(caller: Caller) -> Result:
    raise NotImplementedError


def power(action: PowerAction, caller: Caller) -> Result:
    raise NotImplementedError


def clipboard_get(caller: Caller) -> Result:
    raise NotImplementedError


def clipboard_set(text: str, caller: Caller) -> Result:
    raise NotImplementedError


def type_text(text: str, target: str | int, caller: Caller) -> Result:
    raise NotImplementedError
