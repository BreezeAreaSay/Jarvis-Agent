"""Клавиши: имена → виртуальные коды Windows (VK) и разбор сочетаний «ctrl+shift+s».

Коды не зависят от раскладки: «ctrl+c» и «ctrl+с» (русская «с» на той же клавише) — одно и то же сочетание.
Имена — латиницей и по-русски («пробел», «энтер», «стрелка вверх», «шифт»); регистр, пробелы, «_» и «-» внутри
имени не важны («Page Up», «page_up», «PAGEUP»). Модуль чистый (без Windows) — его проверяют тесты на Linux.
"""

import re

from jarvis.platform.base import PlatformError

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C  # Print Screen
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D  # клавиша контекстного меню
VK_NUMPAD0 = 0x60
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_BROWSER_BACK = 0xA6
VK_BROWSER_FORWARD = 0xA7
VK_BROWSER_REFRESH = 0xA8
VK_BROWSER_STOP = 0xA9
VK_BROWSER_SEARCH = 0xAA
VK_BROWSER_FAVORITES = 0xAB
VK_BROWSER_HOME = 0xAC
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_LAUNCH_MAIL = 0xB4
VK_OEM_1 = 0xBA  # ; :
VK_OEM_PLUS = 0xBB  # = +
VK_OEM_COMMA = 0xBC
VK_OEM_MINUS = 0xBD
VK_OEM_PERIOD = 0xBE
VK_OEM_2 = 0xBF  # / ?
VK_OEM_3 = 0xC0  # ` ~ (русская «ё»)
VK_OEM_4 = 0xDB  # [ {
VK_OEM_5 = 0xDC  # \ |
VK_OEM_6 = 0xDD  # ] }
VK_OEM_7 = 0xDE  # ' "
# Неназначенный код: нажатие между Alt/Win и их отпусканием, чтобы одиночное отпускание не открыло меню окна
# или «Пуск» (тот же приём, что «маска» в AutoHotkey).
VK_MASK = 0xE8

MODIFIERS = frozenset(
    {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU}
    | {VK_LWIN, VK_RWIN}
)
# левые и правые модификаторы — их состояние проверяется перед вводом (GetAsyncKeyState)
SIDED_MODIFIERS = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)

# клавиши с префиксом E0 (KEYEVENTF_EXTENDEDKEY): навигация, Win, меню, правые Ctrl/Alt, Num /, медиа
EXTENDED_VKS = frozenset(
    {VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_SNAPSHOT, VK_INSERT, VK_DELETE}
    | {VK_LWIN, VK_RWIN, VK_APPS, VK_DIVIDE, VK_NUMLOCK, VK_RCONTROL, VK_RMENU}
    | set(range(VK_BROWSER_BACK, 0xB8))
)

_NAMES: dict[int, tuple[str, ...]] = {
    VK_CONTROL: ("ctrl", "control", "ctl", "контрол", "контрл", "ктрл", "контроль", "кнтрл"),
    VK_LCONTROL: ("lctrl", "leftctrl", "lcontrol", "leftcontrol", "левыйконтрол", "левыйctrl"),
    VK_RCONTROL: ("rctrl", "rightctrl", "rcontrol", "rightcontrol", "правыйконтрол", "правыйctrl"),
    VK_SHIFT: ("shift", "шифт"),
    VK_LSHIFT: ("lshift", "leftshift", "левыйшифт", "левыйshift"),
    VK_RSHIFT: ("rshift", "rightshift", "правыйшифт", "правыйshift"),
    VK_MENU: ("alt", "альт"),
    VK_LMENU: ("lalt", "leftalt", "левыйальт", "левыйalt"),
    VK_RMENU: ("ralt", "rightalt", "altgr", "правыйальт", "правыйalt"),
    VK_LWIN: ("win", "windows", "lwin", "leftwin", "super", "meta", "вин", "виндовс", "винда"),
    VK_RWIN: ("rwin", "rightwin"),
    VK_RETURN: ("enter", "return", "энтер", "ентер", "ввод"),
    VK_ESCAPE: ("esc", "escape", "эскейп", "ескейп", "эскейк", "эск"),
    VK_SPACE: ("space", "spacebar", "пробел", "спейс"),
    VK_TAB: ("tab", "таб", "табуляция"),
    VK_BACK: ("backspace", "back", "bksp", "бэкспейс", "бекспейс", "бэкспэйс"),
    VK_DELETE: ("delete", "del", "делит", "делете", "дел"),
    VK_INSERT: ("insert", "ins", "инсерт"),
    VK_HOME: ("home", "хоум"),
    VK_END: ("end", "энд", "енд"),
    VK_PRIOR: ("pageup", "pgup", "пейджап"),
    VK_NEXT: ("pagedown", "pgdn", "pgdown", "пейдждаун"),
    VK_UP: ("up", "arrowup", "uparrow", "стрелкавверх", "вверх"),
    VK_DOWN: ("down", "arrowdown", "downarrow", "стрелкавниз", "вниз"),
    VK_LEFT: ("left", "arrowleft", "leftarrow", "стрелкавлево", "влево"),
    VK_RIGHT: ("right", "arrowright", "rightarrow", "стрелкавправо", "вправо"),
    VK_SNAPSHOT: ("printscreen", "prtsc", "prtscr", "prtscn", "printscr", "snapshot", "принтскрин"),
    VK_CAPITAL: ("capslock", "caps", "капслок", "капс"),
    VK_NUMLOCK: ("numlock", "намлок"),
    VK_SCROLL: ("scrolllock", "скролллок"),
    VK_PAUSE: ("pause", "break", "pausebreak"),
    VK_APPS: ("apps", "menu", "contextmenu", "application", "контекстноеменю"),
    VK_VOLUME_UP: ("volumeup", "volup", "audiovolumeup"),
    VK_VOLUME_DOWN: ("volumedown", "voldown", "audiovolumedown"),
    VK_VOLUME_MUTE: ("volumemute", "volmute", "mute", "audiomute"),
    VK_MEDIA_PLAY_PAUSE: ("mediaplaypause", "playpause", "mediaplay", "mediapause"),
    VK_MEDIA_NEXT_TRACK: ("medianext", "medianexttrack", "nexttrack"),
    VK_MEDIA_PREV_TRACK: ("mediaprev", "mediaprevious", "mediaprevtrack", "prevtrack", "previoustrack"),
    VK_MEDIA_STOP: ("mediastop",),
    VK_BROWSER_BACK: ("browserback",),
    VK_BROWSER_FORWARD: ("browserforward",),
    VK_BROWSER_REFRESH: ("browserrefresh",),
    VK_BROWSER_STOP: ("browserstop",),
    VK_BROWSER_SEARCH: ("browsersearch",),
    VK_BROWSER_FAVORITES: ("browserfavorites",),
    VK_BROWSER_HOME: ("browserhome",),
    VK_LAUNCH_MAIL: ("launchmail",),
    VK_MULTIPLY: ("multiply", "numpadmultiply", "nummultiply"),
    VK_ADD: ("add", "numpadadd", "numadd", "numpadplus"),
    VK_SUBTRACT: ("subtract", "numpadsubtract", "numsubtract", "numpadminus"),
    VK_DECIMAL: ("decimal", "numpaddecimal", "numdecimal"),
    VK_DIVIDE: ("divide", "numpaddivide", "numdivide"),
    VK_OEM_PLUS: ("plus", "equals", "equal", "плюс"),
    VK_OEM_MINUS: ("minus", "hyphen", "dash", "минус"),
    VK_OEM_COMMA: ("comma", "запятая"),
    VK_OEM_PERIOD: ("period", "dot", "точка"),
    VK_OEM_2: ("slash", "слэш", "слеш"),
    VK_OEM_5: ("backslash", "бэкслэш", "бекслеш"),
    VK_OEM_1: ("semicolon",),
    VK_OEM_7: ("quote", "apostrophe"),
    VK_OEM_3: ("backquote", "backtick", "grave", "tilde"),
    VK_OEM_4: ("bracketleft", "leftbracket", "openbracket"),
    VK_OEM_6: ("bracketright", "rightbracket", "closebracket"),
}
BY_NAME: dict[str, int] = {name: vk for vk, names in _NAMES.items() for name in names}

_PUNCT = {
    ";": VK_OEM_1,
    "=": VK_OEM_PLUS,
    "+": VK_OEM_PLUS,
    ",": VK_OEM_COMMA,
    "-": VK_OEM_MINUS,
    ".": VK_OEM_PERIOD,
    "/": VK_OEM_2,
    "`": VK_OEM_3,
    "~": VK_OEM_3,
    "[": VK_OEM_4,
    "\\": VK_OEM_5,
    "]": VK_OEM_6,
    "'": VK_OEM_7,
    "*": VK_MULTIPLY,
}
# русская раскладка ЙЦУКЕН: буква → клавиша, на которой она нарисована
_RU = {
    **dict(zip("йцукенгшщз", "QWERTYUIOP", strict=True)),
    **dict(zip("фывапролд", "ASDFGHJKL", strict=True)),
    **dict(zip("ячсмить", "ZXCVBNM", strict=True)),
}
_RU_OEM = {"х": VK_OEM_4, "ъ": VK_OEM_6, "ж": VK_OEM_1, "э": VK_OEM_7, "б": VK_OEM_COMMA, "ю": VK_OEM_PERIOD}
_RU_OEM["ё"] = VK_OEM_3

_CANON = re.compile(r"[\s_\-]+")
_FKEY = re.compile(r"[fф](\d{1,2})")
_NUMPAD = re.compile(r"(?:numpad|num)(\d)")
_HEX = re.compile(r"0x([0-9a-f]{1,2})")


def _char_vk(ch: str) -> int | None:
    """Один символ (уже в нижнем регистре) → VK."""
    if "a" <= ch <= "z":
        return ord(ch.upper())
    if "0" <= ch <= "9":
        return ord(ch)
    if ch in _RU:
        return ord(_RU[ch])
    if ch in _RU_OEM:
        return _RU_OEM[ch]
    return _PUNCT.get(ch)


def lookup_key(name: str) -> int | None:
    """Имя одной клавиши → VK или None, если клавиша не известна."""
    raw = name.strip()
    if not raw:
        return None
    low = raw.casefold()
    if len(low) == 1:
        return _char_vk(low)
    if m := _HEX.fullmatch(low):
        code = int(m.group(1), 16)
        return code if 0 < code < 0xFF else None
    key = _CANON.sub("", low.replace("ё", "е"))
    if key.startswith("vk") and key[2:] in BY_NAME:
        key = key[2:]
    if key in BY_NAME:
        return BY_NAME[key]
    if m := _FKEY.fullmatch(key):
        n = int(m.group(1))
        return VK_F1 + n - 1 if 1 <= n <= 24 else None
    if m := _NUMPAD.fullmatch(key):
        return VK_NUMPAD0 + int(m.group(1))
    if len(key) == 1:  # «Shift _» и подобное после чистки
        return _char_vk(key)
    return None


def parse_key(name: str) -> int:
    """Имя клавиши → VK; неизвестная клавиша — PlatformError."""
    vk = lookup_key(name)
    if vk is None:
        raise PlatformError(f"Не понял клавишу «{name.strip()}»")
    return vk


def _split(combo: str) -> list[str]:
    """«ctrl + shift + s» → ["ctrl", "shift", "s"]; «ctrl++» → ["ctrl", "+"] (последний «+» — клавиша)."""
    s = combo.strip()
    if s == "+":
        return ["+"]
    trailing_plus = False
    if s.endswith("+"):
        body = s[:-1].rstrip()
        if not body.endswith("+"):
            raise PlatformError(f"Неполное сочетание клавиш: «{combo.strip()}»")
        trailing_plus = True
        s = body[:-1].rstrip()
    parts = [p.strip() for p in s.split("+")] if s else []
    if any(not p for p in parts):
        raise PlatformError(f"Неполное сочетание клавиш: «{combo.strip()}»")
    if trailing_plus:
        parts.append("+")
    return parts


def _token_keys(token: str) -> list[int]:
    vk = lookup_key(token)
    if vk is not None:
        return [vk]
    words = token.split()
    if len(words) > 1:  # «ctrl shift s» без плюсов — если каждое слово известно
        found = [lookup_key(w) for w in words]
        if all(v is not None for v in found):
            return [v for v in found if v is not None]
    raise PlatformError(f"Не понял клавишу «{token}»")


def parse_combo(combo: str) -> list[int]:
    """Сочетание → VK по порядку нажатия: сначала модификаторы (в порядке записи), затем остальные клавиши.

    «ctrl+shift+s», «Ctrl + Shift + S», «win+d», «alt+f4», «контрол+с», «стрелка вверх», «ctrl++».
    Пустое или непонятное сочетание — PlatformError с текстом для человека.
    """
    if not combo or not combo.strip():
        raise PlatformError("Не указаны клавиши")
    keys: list[int] = []
    for token in _split(combo):
        for vk in _token_keys(token):
            if vk not in keys:
                keys.append(vk)
    mods = [vk for vk in keys if vk in MODIFIERS]
    rest = [vk for vk in keys if vk not in MODIFIERS]
    return mods + rest


def is_extended(vk: int) -> bool:
    return vk in EXTENDED_VKS
