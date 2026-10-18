"""Сочетания клавиш: проверка, запись, клавиши словами."""

import importlib.util
from pathlib import Path

import pytest

from jarvis.commands import keys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ctrl + Shift + S", "ctrl+shift+s"),
        ("ctrl+k ctrl+s", "ctrl+k ctrl+s"),
        ("ctrl+k, ctrl+s", "ctrl+k ctrl+s"),
        ("down*3", "down*3"),
        ("ctrl++", "ctrl+plus"),
        ("ctrl+page up", "ctrl+page_up"),
        ("ctrl+num +", "ctrl+num_add"),
        ("num 5", "num5"),
        ("play/pause media", "play_pause"),
        ("win+.", "win+period"),
        ("ctrl+с", "ctrl+c"),  # русская «с» на клавише C
        ("ctrl+,", "ctrl+comma"),
        ("f3+h", "f3+h"),
        ("shift+ctrl+esc", "shift+ctrl+esc"),
        ("контрол+ы", "ctrl+s"),  # «ы» — на клавише S
        ("print screen", "print_screen"),
        ("Alt+F4", "alt+f4"),
    ],
)
def test_normalize_keys(raw: str, expected: str) -> None:
    assert keys.normalize_keys(raw) == expected


@pytest.mark.parametrize("raw", ["", "ctrl+", "ctrl+foo", "down*0", "down*99"])
def test_invalid_keys(raw: str) -> None:
    assert keys.validate_keys(raw) is not None


def test_slot_value_is_not_validated() -> None:
    assert keys.validate_keys("{keys}") is None


def test_humanize() -> None:
    assert keys.humanize("ctrl+shift+s") == "Ctrl+Shift+S"
    assert keys.humanize("down*3") == "↓ ×3"
    assert keys.humanize("play_pause") == "Play/Pause"
    assert keys.humanize("space") == "Пробел"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("контрол шифт эс", "ctrl+shift+s"),
        ("энтер", "enter"),
        ("эф пять", "f5"),
        ("пробел", "space"),
        ("контрол плюс с", "ctrl+s"),
        ("вниз три раза", "down*3"),
        ("альт таб", "alt+tab"),
        ("стрелка вверх", "up"),
        ("клавишу эскейп", "esc"),
        ("пейдж даун", "page_down"),
        ("вин д", "win+d"),
        ("контрол z", "ctrl+z"),
    ],
)
def test_spoken_keys(spoken: str, expected: str) -> None:
    assert keys.parse_spoken(spoken) == expected


@pytest.mark.parametrize("spoken", ["привет мир", "кнопку купить", ""])
def test_spoken_not_keys(spoken: str) -> None:
    assert keys.parse_spoken(spoken) is None


def test_names_understood_by_windows_platform() -> None:
    """Канонические имена клавиш понимает разбор сочетаний платформы Windows (модуль чистый, без WinAPI)."""
    path = Path(__file__).resolve().parents[2] / "jarvis" / "platform" / "windows" / "keys.py"
    if not path.exists():
        pytest.skip("модуля клавиш платформы Windows нет")
    spec = importlib.util.spec_from_file_location("win_keys_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rejected = []
    for name in sorted(keys.KEYS):
        try:
            module.parse_combo(name)
        except Exception:
            rejected.append(name)
    assert rejected == []
