"""Окна Windows. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from dataclasses import dataclass

from pc.result import Caller, Result


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str


def list_windows() -> list[WindowInfo]:
    raise NotImplementedError


def foreground() -> WindowInfo | None:
    raise NotImplementedError


def find_window(target: str | int) -> WindowInfo | None:
    raise NotImplementedError


def focus(hwnd: int) -> Result:
    raise NotImplementedError


def focus_target(target: str | int, caller: Caller) -> Result:
    raise NotImplementedError


def close_target(target: str | int, caller: Caller) -> Result:
    raise NotImplementedError


def window_action(action: str, target: str | int | None, caller: Caller) -> Result:
    raise NotImplementedError


def list_windows_result(caller: Caller) -> Result:
    raise NotImplementedError
