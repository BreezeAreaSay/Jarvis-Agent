"""Разбор клавиш и сочетаний платформы Windows (чистый код — проверяется на Linux)."""

import pytest

from jarvis.platform.base import PlatformError
from jarvis.platform.windows import keys as k
from jarvis.platform.windows.keys import parse_combo, parse_key


@pytest.mark.parametrize(
    "combo",
    ["ctrl+shift+s", "ctrl + shift + s", "Ctrl+Shift+S", "CTRL +SHIFT+ s", "control+shift+s", "ctrl shift s"],
)
def test_spacing_and_case_variants(combo: str) -> None:
    assert parse_combo(combo) == [k.VK_CONTROL, k.VK_SHIFT, ord("S")]


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("win+d", [k.VK_LWIN, ord("D")]),
        ("alt+f4", [k.VK_MENU, k.VK_F1 + 3]),
        ("alt+tab", [k.VK_MENU, k.VK_TAB]),
        ("enter", [k.VK_RETURN]),
        ("esc", [k.VK_ESCAPE]),
        ("escape", [k.VK_ESCAPE]),
        ("space", [k.VK_SPACE]),
        ("tab", [k.VK_TAB]),
        ("backspace", [k.VK_BACK]),
        ("delete", [k.VK_DELETE]),
        ("home", [k.VK_HOME]),
        ("end", [k.VK_END]),
        ("pageup", [k.VK_PRIOR]),
        ("page down", [k.VK_NEXT]),
        ("Page_Down", [k.VK_NEXT]),
        ("up", [k.VK_UP]),
        ("down", [k.VK_DOWN]),
        ("left", [k.VK_LEFT]),
        ("right", [k.VK_RIGHT]),
        ("f1", [k.VK_F1]),
        ("F12", [k.VK_F1 + 11]),
        ("f24", [k.VK_F1 + 23]),
        ("volume_up", [k.VK_VOLUME_UP]),
        ("volume_down", [k.VK_VOLUME_DOWN]),
        ("volume_mute", [k.VK_VOLUME_MUTE]),
        ("media_play_pause", [k.VK_MEDIA_PLAY_PAUSE]),
        ("media_next", [k.VK_MEDIA_NEXT_TRACK]),
        ("media_prev", [k.VK_MEDIA_PREV_TRACK]),
        ("printscreen", [k.VK_SNAPSHOT]),
        ("print screen", [k.VK_SNAPSHOT]),
        ("capslock", [k.VK_CAPITAL]),
        ("a", [ord("A")]),
        ("Z", [ord("Z")]),
        ("7", [ord("7")]),
        ("ctrl+0", [k.VK_CONTROL, ord("0")]),
        ("num5", [k.VK_NUMPAD0 + 5]),
        ("0x41", [0x41]),
        ("vk_return", [k.VK_RETURN]),
        ("left ctrl", [k.VK_LCONTROL]),
        ("right alt", [k.VK_RMENU]),
    ],
)
def test_named_keys(combo: str, expected: list[int]) -> None:
    assert parse_combo(combo) == expected


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("пробел", [k.VK_SPACE]),
        ("энтер", [k.VK_RETURN]),
        ("ввод", [k.VK_RETURN]),
        ("эскейп", [k.VK_ESCAPE]),
        ("таб", [k.VK_TAB]),
        ("стрелка вверх", [k.VK_UP]),
        ("Стрелка Вниз", [k.VK_DOWN]),
        ("стрелка влево", [k.VK_LEFT]),
        ("стрелка вправо", [k.VK_RIGHT]),
        ("шифт", [k.VK_SHIFT]),
        ("контрол", [k.VK_CONTROL]),
        ("альт", [k.VK_MENU]),
        ("вин", [k.VK_LWIN]),
        ("контрол + шифт + s", [k.VK_CONTROL, k.VK_SHIFT, ord("S")]),
        ("альт+ф4", [k.VK_MENU, k.VK_F1 + 3]),
        ("вин+стрелка вверх", [k.VK_LWIN, k.VK_UP]),
    ],
)
def test_russian_aliases(combo: str, expected: list[int]) -> None:
    assert parse_combo(combo) == expected


def test_cyrillic_letters_are_physical_keys() -> None:
    # на клавише C нарисована «С», на V — «М»: сочетания работают по клавишам, а не по раскладке
    assert parse_combo("ctrl+с") == [k.VK_CONTROL, ord("C")]
    assert parse_combo("ctrl+м") == [k.VK_CONTROL, ord("V")]
    assert parse_combo("ctrl+ф") == [k.VK_CONTROL, ord("A")]
    assert parse_key("ё") == k.VK_OEM_3


def test_plus_and_punctuation_keys() -> None:
    assert parse_combo("ctrl++") == [k.VK_CONTROL, k.VK_OEM_PLUS]
    assert parse_combo("ctrl + +") == [k.VK_CONTROL, k.VK_OEM_PLUS]
    assert parse_combo("+") == [k.VK_OEM_PLUS]
    assert parse_combo("ctrl+-") == [k.VK_CONTROL, k.VK_OEM_MINUS]
    assert parse_combo("ctrl+plus") == [k.VK_CONTROL, k.VK_OEM_PLUS]
    assert parse_combo("shift+/") == [k.VK_SHIFT, k.VK_OEM_2]


def test_modifiers_go_first_and_duplicates_drop() -> None:
    assert parse_combo("s+ctrl") == [k.VK_CONTROL, ord("S")]
    assert parse_combo("ctrl+ctrl+c") == [k.VK_CONTROL, ord("C")]
    assert parse_combo("a+shift+b") == [k.VK_SHIFT, ord("A"), ord("B")]


@pytest.mark.parametrize(
    "combo", ["", "   ", "ctrl+", "+ctrl", "ctrl++shift", "foo", "ctrl+foo", "f25", "f0", "ctrl+shift+"]
)
def test_invalid_combos(combo: str) -> None:
    with pytest.raises(PlatformError) as err:
        parse_combo(combo)
    assert str(err.value)  # текст для человека, по-русски
    assert any("а" <= ch <= "я" for ch in str(err.value).casefold())


def test_unknown_key_message_names_the_key() -> None:
    with pytest.raises(PlatformError, match="«blah»"):
        parse_combo("ctrl+blah")


def test_extended_keys() -> None:
    for vk in (k.VK_LEFT, k.VK_UP, k.VK_DELETE, k.VK_HOME, k.VK_LWIN, k.VK_RCONTROL, k.VK_MEDIA_PLAY_PAUSE):
        assert k.is_extended(vk)
    for vk in (k.VK_RETURN, k.VK_LCONTROL, k.VK_SHIFT, ord("A"), k.VK_F1, k.VK_SPACE):
        assert not k.is_extended(vk)
