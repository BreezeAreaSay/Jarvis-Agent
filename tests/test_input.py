"""pc._input: структуры SendInput и события для клавиш и текста."""

import ctypes
import sys

import pytest

from pc import _input


class FakeSendInput:
    def __init__(self, accept: int | None = None) -> None:
        self.batches: list[list[tuple[int, int, int]]] = []
        self.accept = accept

    def send(self, inputs: list[_input.INPUT]) -> int:
        batch = []
        for inp in inputs:
            assert inp.type == _input.INPUT_KEYBOARD
            batch.append((inp.ki.wVk, inp.ki.wScan, inp.ki.dwFlags))
        self.batches.append(batch)
        return len(inputs) if self.accept is None else self.accept

    def scan_code(self, vk: int) -> int:
        return 0x22 if vk == 0xB3 else 0


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSendInput:
    f = FakeSendInput()
    monkeypatch.setattr(_input, "_api", f)
    return f


U = _input.KEYEVENTF_UNICODE
UP = _input.KEYEVENTF_KEYUP


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="размеры для x64")
def test_struct_sizes_x64() -> None:
    assert ctypes.sizeof(_input.INPUT) == 40
    assert ctypes.sizeof(_input.MOUSEINPUT) == 32
    assert ctypes.sizeof(_input.KEYBDINPUT) == 24
    assert ctypes.sizeof(_input.HARDWAREINPUT) == 8
    assert _input.INPUT.u.offset == 8


def test_text_unicode_down_up(fake: FakeSendInput) -> None:
    assert _input.send_text("Ая") == 4
    assert fake.batches == [
        [(0, ord("А"), U), (0, ord("А"), U | UP), (0, ord("я"), U), (0, ord("я"), U | UP)]
    ]


def test_text_emoji_surrogate_pair(fake: FakeSendInput) -> None:
    _input.send_text("😀")
    assert fake.batches == [[(0, 0xD83D, U), (0, 0xD83D, U | UP), (0, 0xDE00, U), (0, 0xDE00, U | UP)]]


def test_text_newline_and_tab_are_keys(fake: FakeSendInput) -> None:
    _input.send_text("a\nb\tc\r\nd")
    vks = [(vk, flags) for vk, _scan, flags in fake.batches[0] if vk]
    assert vks == [
        (_input.VK_RETURN, 0),
        (_input.VK_RETURN, UP),
        (_input.VK_TAB, 0),
        (_input.VK_TAB, UP),
        (_input.VK_RETURN, 0),
        (_input.VK_RETURN, UP),
    ]
    assert len(fake.batches[0]) == 2 * 7  # a, ⏎, b, ⇥, c, ⏎ (\r\n — один), d


def test_text_one_sendinput_call(fake: FakeSendInput) -> None:
    _input.send_text("привет, мир")
    assert len(fake.batches) == 1
    assert _input.send_text("") == 0


def test_text_returns_inserted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_input, "_api", FakeSendInput(accept=0))
    assert _input.send_text("abc") == 0


def test_send_key_media_extended(fake: FakeSendInput) -> None:
    assert _input.send_key(0xB3) is True
    assert _input.send_key(0xB3, up=True) is True
    ext = _input.KEYEVENTF_EXTENDEDKEY
    assert fake.batches == [[(0xB3, 0x22, ext)], [(0xB3, 0x22, ext | UP)]]


def test_send_key_alt_release(fake: FakeSendInput) -> None:
    assert _input.send_key(_input.VK_MENU, up=True)
    assert fake.batches == [[(_input.VK_MENU, 0, UP)]]


def test_send_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_input, "_api", FakeSendInput(accept=0))
    assert _input.send_key(0xB0) is False


@pytest.mark.skipif(sys.platform != "win32", reason="нужен user32")
def test_real_api_signatures() -> None:
    a = _input._Api()
    assert a._send.restype is ctypes.c_uint
    assert a._send.argtypes == (ctypes.c_uint, ctypes.POINTER(_input.INPUT), ctypes.c_int)
    assert a.scan_code(_input.VK_RETURN) == 0x1C
    assert a.send([]) == 0
