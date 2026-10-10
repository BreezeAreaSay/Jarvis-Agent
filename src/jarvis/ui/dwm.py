"""DWM Windows 11: скругления, тёмная рамка и системный фон (acrylic) окна. Без Qt.

Вызовы ОС — за тонким слоем `_api` (ctypes, лениво). Функции возвращают bool: True — DWM принял атрибут.
На не-Windows, на старых сборках и при ошибке HRESULT — False, и окно остаётся со сплошным фоном.
"""

import logging
import sys
from typing import Any, Literal

log = logging.getLogger("jarvis")

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38

DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2  # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMSBT_TABBEDWINDOW = 4

BUILD_WIN11 = 22000  # скругления DWM
BUILD_BACKDROP = 22621  # DWMWA_SYSTEMBACKDROP_TYPE (Windows 11 22H2)


class _Api:
    """Тонкий слой над dwmapi.DwmSetWindowAttribute (только Windows)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)  # type: ignore[attr-defined]
        fn = dwmapi.DwmSetWindowAttribute
        fn.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD]
        fn.restype = ctypes.c_long  # HRESULT
        self._set = fn

    def build(self) -> int:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]

    def set_dword(self, hwnd: int, attr: int, value: int) -> int:
        """DwmSetWindowAttribute с 4-байтным значением (BOOL/DWORD/enum) → HRESULT."""
        ct = self._ctypes
        data = ct.c_int(value)
        return int(self._set(ct.c_void_p(hwnd), attr, ct.byref(data), ct.sizeof(data)))


_api: Any = None


def api() -> Any:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def _available() -> bool:
    if _api is not None:
        return True
    return sys.platform == "win32"


def _set(hwnd: int, attr: int, value: int, min_build: int = 0) -> bool:
    if not hwnd or not _available():
        return False
    try:
        a = api()
        if min_build and a.build() < min_build:
            return False
        hr = a.set_dword(int(hwnd), attr, value)
    except Exception as e:  # нет dwmapi, неверный hwnd и т.п. — просто без эффекта
        log.debug("DwmSetWindowAttribute(%s): %s", attr, e)
        return False
    if hr < 0:
        log.debug("DwmSetWindowAttribute(%s) = 0x%08X", attr, hr & 0xFFFFFFFF)
        return False
    return True


def set_dark(hwnd: int, on: bool = True) -> bool:
    """Тёмная системная рамка и тень (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    return _set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if on else 0)


def set_corners(hwnd: int, rounded: bool = True) -> bool:
    """Скругления Windows 11 (DWMWA_WINDOW_CORNER_PREFERENCE). Windows 10 — False."""
    pref = DWMWCP_ROUND if rounded else DWMWCP_DONOTROUND
    return _set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, pref, BUILD_WIN11)


def set_backdrop(hwnd: int, kind: Literal["acrylic", "none"]) -> bool:
    """Системный фон окна: acrylic (DWMSBT_TRANSIENTWINDOW) или none. Windows 10 и 11 до 22H2 — False."""
    value = DWMSBT_TRANSIENTWINDOW if kind == "acrylic" else DWMSBT_NONE
    return _set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, value, BUILD_BACKDROP)


def apply(hwnd: int, backdrop: Literal["solid", "acrylic"] = "solid") -> bool:
    """Оформить окно: тёмный режим, скругления и (по желанию) acrylic. True — acrylic включён.

    Любая ошибка — сплошной фон: окно рисует его само, поэтому откат всегда безопасен.
    """
    set_dark(hwnd, True)
    set_corners(hwnd, True)
    if backdrop != "acrylic":
        return False
    return set_backdrop(hwnd, "acrylic")
