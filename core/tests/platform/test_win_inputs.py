"""SendInput: размеры структур, построение событий, пересчёт координат мыши (чистый код — на Linux)."""

import ctypes

import pytest

from jarvis.platform.base import PlatformError
from jarvis.platform.windows import inputs as i
from jarvis.platform.windows import keys as k


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="размеры x64")
def test_struct_sizes_match_windows_x64() -> None:
    assert ctypes.sizeof(i.INPUT) == 40
    assert ctypes.sizeof(i.KEYBDINPUT) == 24
    assert ctypes.sizeof(i.MOUSEINPUT) == 32
    assert i.INPUT.u.offset == 8


def _keys(events: list[i.INPUT]) -> list[tuple[int, int, int]]:
    return [(e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in events]


def test_combo_presses_in_order_and_releases_in_reverse() -> None:
    vks = [k.VK_CONTROL, k.VK_SHIFT, ord("S")]
    events = i.combo_inputs(vks, {k.VK_CONTROL: 0x1D, k.VK_SHIFT: 0x2A, ord("S"): 0x1F})
    assert all(e.type == i.INPUT_KEYBOARD for e in events)
    assert _keys(events) == [
        (k.VK_CONTROL, 0x1D, 0),
        (k.VK_SHIFT, 0x2A, 0),
        (ord("S"), 0x1F, 0),
        (ord("S"), 0x1F, i.KEYEVENTF_KEYUP),
        (k.VK_SHIFT, 0x2A, i.KEYEVENTF_KEYUP),
        (k.VK_CONTROL, 0x1D, i.KEYEVENTF_KEYUP),
    ]
    assert all(e.ki.dwExtraInfo == i.INJECTED_MARK for e in events)


def test_extended_flag_for_arrows_and_media() -> None:
    down, up = i.combo_inputs([k.VK_LEFT])
    assert down.ki.dwFlags == i.KEYEVENTF_EXTENDEDKEY
    assert up.ki.dwFlags == i.KEYEVENTF_EXTENDEDKEY | i.KEYEVENTF_KEYUP
    play = i.key_input(k.VK_MEDIA_PLAY_PAUSE)
    assert play.ki.dwFlags & i.KEYEVENTF_EXTENDEDKEY


def test_text_uses_unicode_events() -> None:
    events = i.text_inputs("Ок")
    assert [(e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in events] == [
        (0, ord("О"), i.KEYEVENTF_UNICODE),
        (0, ord("О"), i.KEYEVENTF_UNICODE | i.KEYEVENTF_KEYUP),
        (0, ord("к"), i.KEYEVENTF_UNICODE),
        (0, ord("к"), i.KEYEVENTF_UNICODE | i.KEYEVENTF_KEYUP),
    ]


def test_text_surrogate_pairs() -> None:
    events = i.text_inputs("😀")
    units = [e.ki.wScan for e in events]
    assert units == [0xD83D, 0xD83D, 0xDE00, 0xDE00]
    assert [bool(e.ki.dwFlags & i.KEYEVENTF_KEYUP) for e in events] == [False, True, False, True]


def test_text_newlines_tabs_and_control_chars() -> None:
    events = i.text_inputs("a\r\nb\nc\td\x07", {k.VK_RETURN: 0x1C})
    vks = [e.ki.wVk for e in events]
    assert vks.count(k.VK_RETURN) == 4  # «\r\n» — один Enter, «\n» — ещё один (нажатие и отпускание)
    assert vks.count(k.VK_TAB) == 2
    assert len(events) == 4 * 2 + 2 * 2 + 2  # 4 буквы, 2 Enter, 1 Tab; \x07 пропущен
    enter = next(e for e in events if e.ki.wVk == k.VK_RETURN)
    assert enter.ki.wScan == 0x1C


@pytest.mark.parametrize("width", [800, 1024, 1280, 1366, 1440, 1600, 1920, 2560, 3440, 3840, 5120, 7680])
def test_absolute_coordinates_hit_every_pixel(width: int) -> None:
    screen = (0, 0, width, 1080)
    for px in range(width):
        ax, _ay = i.to_absolute(px, 0, screen)
        assert 0 <= ax <= 65535
        assert i.from_absolute(ax, 0, screen)[0] == px


def test_absolute_coordinates_multi_monitor_with_negative_origin() -> None:
    # второй монитор слева от основного: виртуальный экран начинается с x = -1920
    screen = (-1920, -200, 3840, 1280)
    for x, y in [(-1920, -200), (-1, 0), (0, 0), (959, 539), (1919, 1079), (-1000, 900)]:
        ax, ay = i.to_absolute(x, y, screen)
        assert i.from_absolute(ax, ay, screen) == (x, y)
    # вне экрана — прижимается к краю
    assert i.from_absolute(*i.to_absolute(-5000, -5000, screen), screen) == (-1920, -200)
    assert i.from_absolute(*i.to_absolute(9000, 9000, screen), screen) == (1919, 1079)


def test_move_input_flags() -> None:
    event = i.move_input(100, 200)
    assert event.type == i.INPUT_MOUSE
    assert event.mi.dx == 100 and event.mi.dy == 200
    assert event.mi.dwFlags == i.MOUSEEVENTF_MOVE | i.MOUSEEVENTF_ABSOLUTE | i.MOUSEEVENTF_VIRTUALDESK
    assert event.mi.dwExtraInfo == i.INJECTED_MARK


def test_clicks() -> None:
    left = i.click_inputs("left")
    assert [e.mi.dwFlags for e in left] == [i.MOUSEEVENTF_LEFTDOWN, i.MOUSEEVENTF_LEFTUP]
    double = i.click_inputs("left", double=True)
    assert [e.mi.dwFlags for e in double] == [i.MOUSEEVENTF_LEFTDOWN, i.MOUSEEVENTF_LEFTUP] * 2
    right = i.click_inputs("right")
    assert [e.mi.dwFlags for e in right] == [i.MOUSEEVENTF_RIGHTDOWN, i.MOUSEEVENTF_RIGHTUP]
    # кнопки поменяны местами (левша): «левый» щелчок — основная кнопка, физически правая
    swapped = i.click_inputs("left", swapped=True)
    assert [e.mi.dwFlags for e in swapped] == [i.MOUSEEVENTF_RIGHTDOWN, i.MOUSEEVENTF_RIGHTUP]
    middle = i.click_inputs("middle", swapped=True)
    assert [e.mi.dwFlags for e in middle] == [i.MOUSEEVENTF_MIDDLEDOWN, i.MOUSEEVENTF_MIDDLEUP]
    with pytest.raises(PlatformError):
        i.click_inputs("fourth")


def test_wheel() -> None:
    assert i.wheel_input(3).mi.mouseData == 3 * i.WHEEL_DELTA
    assert i.wheel_input(-2).mi.mouseData == (-2 * i.WHEEL_DELTA) & 0xFFFFFFFF
    assert ctypes.c_int32(i.wheel_input(-2).mi.mouseData).value == -240
    assert i.wheel_input(10_000).mi.mouseData == i.MAX_WHEEL_NOTCHES * i.WHEEL_DELTA
    assert i.wheel_input(1).mi.dwFlags == i.MOUSEEVENTF_WHEEL


def test_sender_needs_windows() -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("на Windows Sender создаётся")
    with pytest.raises(PlatformError):
        i.Sender()
