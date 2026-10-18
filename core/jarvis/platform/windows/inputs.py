"""Ввод через SendInput: клавиши, текст (KEYEVENTF_UNICODE), мышь в абсолютных координатах.

Структуры INPUT объявлены типами фиксированной ширины — 40 байт на x64 на любой ОС, поэтому построение событий
и пересчёт координат проверяются тестами на Linux. Вызовы ОС — класс Sender (только Windows).
Все события помечаются dwExtraInfo = INJECTED_MARK: хуки клавиатуры и мыши (запись макросов, push-to-talk)
могут отличить ввод Jarvis от ввода человека.
"""

import ctypes
import logging
import time
from collections.abc import Sequence
from typing import Any

from jarvis.platform.base import PlatformError
from jarvis.platform.windows import keys
from jarvis.platform.windows.winapi import DWORD, LONG, POINT, ULONG_PTR, WORD, api, last_error

log = logging.getLogger(__name__)

INJECTED_MARK = 0x4A525653  # «JRVS»

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120
MAX_WHEEL_NOTCHES = 100

MAPVK_VK_TO_VSC = 0

TEXT_CHUNK = 32  # символов в одном вызове SendInput
TEXT_PAUSE_S = 0.01  # между порциями: медленные программы не теряют символы
MODIFIERS_WAIT_S = 0.6  # сколько ждать, пока человек отпустит Ctrl/Alt/Shift/Win

Screen = tuple[int, int, int, int]  # left, top, width, height виртуального экрана


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


# --- построение событий (чистые функции) ------------------------------------------------------------


def key_input(vk: int, up: bool = False, scan: int = 0) -> INPUT:
    """Нажатие или отпускание виртуальной клавиши (с флагом E0 для «расширенных» клавиш)."""
    flags = KEYEVENTF_KEYUP if up else 0
    if keys.is_extended(vk):
        flags |= KEYEVENTF_EXTENDEDKEY
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=scan & 0xFFFF, dwFlags=flags, time=0, dwExtraInfo=INJECTED_MARK)
    return event


def _unicode_unit(unit: int, up: bool) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    event.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=INJECTED_MARK)
    return event


def combo_inputs(vks: Sequence[int], scans: dict[int, int] | None = None) -> list[INPUT]:
    """Сочетание: нажать по порядку, отпустить в обратном."""
    scans = scans or {}
    down = [key_input(vk, False, scans.get(vk, 0)) for vk in vks]
    up = [key_input(vk, True, scans.get(vk, 0)) for vk in reversed(vks)]
    return down + up


def text_inputs(text: str, scans: dict[int, int] | None = None) -> list[INPUT]:
    """Текст: «\\n» (и «\\r\\n») → Enter, «\\t» → Tab, остальное — KEYEVENTF_UNICODE по code unit UTF-16
    (символ вне BMP — суррогатная пара). Прочие управляющие символы пропускаются."""
    scans = scans or {}
    events: list[INPUT] = []
    prev = ""
    for ch in text:
        if ch == "\n" and prev == "\r":
            prev = ch
            continue
        prev = ch
        if ch in "\r\n":
            vk = keys.VK_RETURN
        elif ch == "\t":
            vk = keys.VK_TAB
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue
        else:
            raw = ch.encode("utf-16-le", "surrogatepass")
            for i in range(0, len(raw), 2):
                unit = raw[i] | (raw[i + 1] << 8)
                events.append(_unicode_unit(unit, False))
                events.append(_unicode_unit(unit, True))
            continue
        events.append(key_input(vk, False, scans.get(vk, 0)))
        events.append(key_input(vk, True, scans.get(vk, 0)))
    return events


def to_absolute(x: int, y: int, screen: Screen) -> tuple[int, int]:
    """Точка экрана → нормализованные координаты 0..65535 виртуального рабочего стола (все мониторы).

    Windows переводит обратно как left + dx * width / 65536 с отбрасыванием дробной части; формула
    65536 * px / width + 1 (как в AutoHotkey) попадает внутрь нужной точки при любой ширине. Точка вне экрана
    прижимается к краю.
    """
    left, top, width, height = screen
    width, height = max(1, width), max(1, height)
    px = min(max(int(x) - left, 0), width - 1)
    py = min(max(int(y) - top, 0), height - 1)
    ax = min(65535, (65536 * px) // width + 1)
    ay = min(65535, (65536 * py) // height + 1)
    return ax, ay


def from_absolute(ax: int, ay: int, screen: Screen) -> tuple[int, int]:
    """Обратный пересчёт, как его делает Windows (для тестов и журнала)."""
    left, top, width, height = screen
    return left + (ax * width) // 65536, top + (ay * height) // 65536


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=INJECTED_MARK
    )
    return event


def move_input(ax: int, ay: int) -> INPUT:
    return _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)


def nudge_input() -> INPUT:
    """Пустое относительное движение мыши: ввод «от нас» без побочных эффектов (для SetForegroundWindow)."""
    return _mouse(MOUSEEVENTF_MOVE)


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click_inputs(button: str = "left", double: bool = False, swapped: bool = False) -> list[INPUT]:
    """Нажатие и отпускание кнопки (дважды — двойной щелчок). swapped — кнопки мыши поменяны местами
    (левша): «левый» щелчок — основная кнопка, физически правая."""
    if button not in _BUTTONS:
        raise PlatformError(f"Неизвестная кнопка мыши: {button}")
    if swapped and button in ("left", "right"):
        button = "right" if button == "left" else "left"
    down, up = _BUTTONS[button]
    events = [_mouse(down), _mouse(up)]
    return events * 2 if double else events


def wheel_input(notches: int) -> INPUT:
    """Колесо: >0 — вверх (от себя). Не больше MAX_WHEEL_NOTCHES щелчков за раз."""
    notches = max(-MAX_WHEEL_NOTCHES, min(MAX_WHEEL_NOTCHES, int(notches)))
    return _mouse(MOUSEEVENTF_WHEEL, data=notches * WHEEL_DELTA)


# --- отправка (Windows) ------------------------------------------------------------------------------


class Sender:
    """SendInput и состояние клавиш. Создаётся лениво; не в Windows — PlatformError."""

    def __init__(self) -> None:
        try:
            self._w = api()
            u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        except OSError as e:
            raise PlatformError("Ввод с клавиатуры и мыши доступен только в Windows") from e
        self._send = u.SendInput
        self._send.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int32]
        self._send.restype = ctypes.c_uint32
        self._scans: dict[int, int] = {}

    def send(self, events: Sequence[INPUT]) -> int:
        """Один вызов SendInput (события не перемешиваются с вводом человека); число вставленных событий."""
        if not events:
            return 0
        array = (INPUT * len(events))(*events)
        sent = int(self._send(len(events), array, ctypes.sizeof(INPUT)))
        if sent != len(events):
            log.warning("SendInput: вставлено %d из %d, ошибка %d", sent, len(events), last_error())
        return sent

    def send_all(self, events: Sequence[INPUT], what: str = "нажатия клавиш") -> None:
        if events and self.send(events) == 0:
            raise PlatformError(f"Windows не приняла {what} (экран заблокирован или окно администратора?)")

    def scan(self, vk: int) -> int:
        code = self._scans.get(vk)
        if code is None:
            code = int(self._w.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) & 0xFFFF
            self._scans[vk] = code
        return code

    def scans(self, vks: Sequence[int]) -> dict[int, int]:
        return {vk: self.scan(vk) for vk in vks}

    def key(self, vk: int, up: bool = False) -> INPUT:
        return key_input(vk, up, self.scan(vk))

    def is_down(self, vk: int) -> bool:
        return bool(int(self._w.GetAsyncKeyState(vk)) & 0x8000)

    def held_modifiers(self) -> list[int]:
        return [vk for vk in keys.SIDED_MODIFIERS if self.is_down(vk)]

    def release_modifiers(self, wait_s: float = 0.0, keep: Sequence[int] = ()) -> None:
        """Отпустить «залипшие» Shift/Ctrl/Alt/Win: подождать до wait_s, пока человек отпустит их сам, затем
        отправить отпускание тех, что всё ещё нажаты. Перед отпусканием Alt/Win — нажатие «маски», чтобы
        одиночное отпускание не открыло меню окна или «Пуск»."""
        deadline = time.monotonic() + wait_s
        held = self.held_modifiers()
        while held and time.monotonic() < deadline:
            time.sleep(0.02)
            held = self.held_modifiers()
        held = [vk for vk in held if vk not in keep]
        if not held:
            return
        events: list[INPUT] = []
        if any(vk in (keys.VK_LMENU, keys.VK_RMENU, keys.VK_LWIN, keys.VK_RWIN) for vk in held):
            events += [key_input(keys.VK_MASK), key_input(keys.VK_MASK, up=True)]
        events += [self.key(vk, up=True) for vk in held]
        log.debug("отпускаю залипшие модификаторы: %s", [hex(vk) for vk in held])
        self.send(events)

    def combo(self, vks: Sequence[int]) -> None:
        self.send_all(combo_inputs(vks, self.scans(vks)))

    def type_text(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for start in range(0, len(text), TEXT_CHUNK):
            chunk = text[start : start + TEXT_CHUNK]
            events = text_inputs(chunk, self.scans([keys.VK_RETURN, keys.VK_TAB]))
            if events and self.send(events) == 0:
                raise PlatformError(
                    "Windows не приняла ввод текста (экран заблокирован или окно администратора?)"
                )
            if start + TEXT_CHUNK < len(text):
                time.sleep(TEXT_PAUSE_S)

    # --- мышь

    def virtual_screen(self) -> Screen:
        m = self._w.GetSystemMetrics
        width, height = int(m(78)), int(m(79))  # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            return (0, 0, int(m(0)), int(m(1)))
        return (int(m(76)), int(m(77)), width, height)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN

    def primary_size(self) -> tuple[int, int]:
        return int(self._w.GetSystemMetrics(0)), int(self._w.GetSystemMetrics(1))

    def swapped_buttons(self) -> bool:
        return bool(self._w.GetSystemMetrics(23))  # SM_SWAPBUTTON

    def cursor(self) -> tuple[int, int]:
        point = POINT()
        if not self._w.GetCursorPos(ctypes.byref(point)):
            raise PlatformError("Не удалось узнать положение курсора")
        return int(point.x), int(point.y)

    def move_event(self, x: int, y: int) -> INPUT:
        ax, ay = to_absolute(x, y, self.virtual_screen())
        return move_input(ax, ay)

    def move(self, x: int, y: int) -> None:
        if self.send([self.move_event(x, y)]) == 0 and not self._w.SetCursorPos(int(x), int(y)):
            raise PlatformError("Не удалось передвинуть курсор")

    def click(self, x: int | None, y: int | None, button: str, double: bool) -> None:
        events: list[Any] = []
        if x is not None and y is not None:
            events.append(self.move_event(x, y))
        events += click_inputs(button, double, self.swapped_buttons())
        self.send_all(events, "щелчок мыши")

    def scroll(self, notches: int) -> None:
        if notches:
            self.send_all([wheel_input(notches)], "прокрутку")
