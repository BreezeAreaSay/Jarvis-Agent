"""Громкость. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from pc.result import Caller, Result


def volume_get(caller: Caller) -> Result:
    raise NotImplementedError


def volume(
    set: int | None = None, delta: int | None = None, mute: bool | None = None, caller: Caller = "user"
) -> Result:
    raise NotImplementedError
