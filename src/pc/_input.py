"""Ввод с клавиатуры через SendInput.

Общий для windows.focus (отпускание Alt), media (медиаклавиши) и system.type_text (текст).
Структуры объявлены типами фиксированной ширины (не ctypes.wintypes: на Linux DWORD там 8 байт), поэтому
INPUT = 40 байт на x64 на любой ОС, а модуль импортируется на Linux. Вызов ОС — за `_api`.
"""

import ctypes
import logging
import sys
from collections.abc import Sequence

log = logging.getLogger("jarvis")

# типы Win32 фиксированной ширины
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12

MAPVK_VK_TO_VSC = 0

# клавиши с префиксом E0: стрелки, Ins/Del/Home/End/PgUp/PgDn, Win, Apps, правые Ctrl/Alt, браузерные и медиа
EXTENDED_VK = frozenset(
    {*range(0x21, 0x29), 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5, *range(0xA6, 0xB8)}
)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", _DWORD), ("u", _INPUTUNION))


class _Api:
    """Тонкий слой над user32 (только Windows)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("SendInput есть только в Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send = user32.SendInput
        self._send.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        self._send.restype = ctypes.c_uint
        self._map = user32.MapVirtualKeyW
        self._map.argtypes = (ctypes.c_uint, ctypes.c_uint)
        self._map.restype = ctypes.c_uint

    def send(self, inputs: Sequence[INPUT]) -> int:
        """Один вызов SendInput (события не перемешиваются с вводом человека); число вставленных событий."""
        if not inputs:
            return 0
        array = (INPUT * len(inputs))(*inputs)
        sent = int(self._send(len(inputs), array, ctypes.sizeof(INPUT)))
        if sent != len(inputs):
            err = ctypes.get_last_error()
            log.warning("SendInput: отправлено %d из %d, ошибка %d", sent, len(inputs), err)
        return sent

    def scan_code(self, vk: int) -> int:
        return int(self._map(vk, MAPVK_VK_TO_VSC)) & 0xFFFF


_api: _Api | None = None


def api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def _key(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def key_inputs(vk: int, up: bool = False, scan: int = 0) -> INPUT:
    """Одно событие виртуальной клавиши (нажатие или отпускание)."""
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    return _key(vk, scan, flags)


def text_inputs(text: str) -> list[INPUT]:
    """События для текста: "\\n" → Enter, "\\t" → Tab, остальное — KEYEVENTF_UNICODE (нажатие и отпускание на
    каждый code unit UTF-16; символы вне BMP — суррогатной парой). "\\r\\n" — один Enter."""
    events: list[INPUT] = []
    prev = ""
    for ch in text:
        if ch == "\n" and prev == "\r":
            prev = ch
            continue
        prev = ch
        if ch in "\r\n":
            events += [key_inputs(VK_RETURN), key_inputs(VK_RETURN, up=True)]
        elif ch == "\t":
            events += [key_inputs(VK_TAB), key_inputs(VK_TAB, up=True)]
        else:
            raw = ch.encode("utf-16-le")
            for i in range(0, len(raw), 2):
                unit = raw[i] | (raw[i + 1] << 8)
                events.append(_key(0, unit, KEYEVENTF_UNICODE))
                events.append(_key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def send_key(vk: int, up: bool = False) -> bool:
    """Нажать (up=False) или отпустить (up=True) виртуальную клавишу. True — событие вставлено."""
    a = api()
    try:
        scan = a.scan_code(vk)
    except OSError:
        scan = 0
    return a.send([key_inputs(vk, up, scan)]) == 1


def send_text(text: str) -> int:
    """Напечатать текст одним вызовом SendInput; возвращает число вставленных событий."""
    return api().send(text_inputs(text))
