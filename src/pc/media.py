"""Медиаклавиши. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from typing import Literal

from pc.result import Caller, Result

MediaAction = Literal["play_pause", "next", "prev"]


def media(action: MediaAction, caller: Caller) -> Result:
    raise NotImplementedError
