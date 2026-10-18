"""Клавиши в командах: проверка и запись сочетаний («ctrl+shift+s»), последовательности («ctrl+k ctrl+s»,
«down*3») и клавиши словами («контрол шифт эс» → ctrl+shift+s).

Канонические имена совпадают с теми, что понимает платформа (jarvis.platform: press_keys): модификаторы ctrl,
alt, shift, win; a–z, 0–9, f1–f24; enter, space, tab, esc, page_up, print_screen, play_pause, num5, num_add…
"""

import re

from jarvis.commands.numbers import UNITS

MODIFIERS = ("ctrl", "alt", "shift", "win")


def _words(text: str) -> list[str]:
    return text.split()


_NAMED = _words(
    "enter space tab esc backspace delete insert home end page_up page_down up down left right "
    "caps_lock num_lock scroll_lock print_screen pause menu volume_up volume_down volume_mute "
    "play_pause next_track prev_track media_stop browser_back browser_forward browser_refresh "
    "browser_home browser_search plus minus equal comma period slash backslash semicolon quote "
    "backquote bracket_left bracket_right num_add num_subtract num_multiply num_divide num_decimal"
)
KEYS: frozenset[str] = frozenset(
    [*MODIFIERS, *_NAMED]
    + [chr(c) for c in range(ord("a"), ord("z") + 1)]
    + [str(d) for d in range(10)]
    + [f"f{n}" for n in range(1, 25)]
    + [f"num{d}" for d in range(10)]
)

# имя без пробелов, «_» и «-» → каноническое
_ALIASES: dict[str, str] = {}


def _alias(canonical: str, names: str) -> None:
    for name in names.split():
        _ALIASES[name] = canonical


_alias(
    "ctrl",
    "ctrl control ctl lctrl rctrl leftctrl rightctrl controlleft controlright контрол контрл ктрл контроль",
)
_alias("alt", "alt lalt ralt altgr leftalt rightalt option альт алт")
_alias("shift", "shift lshift rshift leftshift rightshift шифт")
_alias("win", "win windows lwin rwin super meta cmd command вин виндовс винда виндоус")
_alias("enter", "enter return энтер ентер ввод интер")
_alias("space", "space spacebar пробел спейс")
_alias("tab", "tab таб табуляция")
_alias("esc", "esc escape эскейп ескейп эскейк эск искейп")
_alias("backspace", "backspace back bksp бэкспейс бекспейс бэкспэйс")
_alias("delete", "delete del делит делете дел дилит")
_alias("insert", "insert ins инсерт")
_alias("home", "home хоум")
_alias("end", "end энд енд")
_alias("page_up", "pageup pgup prior пейджап")
_alias("page_down", "pagedown pgdn pgdown пейдждаун")
_alias("up", "up arrowup uparrow стрелкавверх вверх")
_alias("down", "down arrowdown downarrow стрелкавниз вниз")
_alias("left", "left arrowleft leftarrow стрелкавлево влево")
_alias("right", "right arrowright rightarrow стрелкавправо вправо")
_alias("print_screen", "printscreen prtsc prtscr prtscn printscr snapshot принтскрин")
_alias("caps_lock", "capslock caps капслок капс")
_alias("num_lock", "numlock намлок")
_alias("scroll_lock", "scrolllock скролллок")
_alias("pause", "pause break pausebreak")
_alias("menu", "menu apps contextmenu application контекстноеменю")
_alias("volume_up", "volumeup volup audiovolumeup")
_alias("volume_down", "volumedown voldown audiovolumedown")
_alias("volume_mute", "volumemute volmute mute audiomute audiovolumemute")
_alias("play_pause", "playpause mediaplaypause mediaplay mediapause play/pausemedia playpausemedia")
_alias("next_track", "nexttrack medianext medianexttrack mediatracknext")
_alias("prev_track", "prevtrack previoustrack mediaprev mediaprevious mediaprevtrack mediatrackprevious")
_alias("media_stop", "mediastop stopmedia")
_alias("browser_back", "browserback")
_alias("browser_forward", "browserforward")
_alias("browser_refresh", "browserrefresh")
_alias("browser_home", "browserhome")
_alias("browser_search", "browsersearch")
_alias("plus", "plus + плюс")
_alias("minus", "minus hyphen dash - минус")
_alias("equal", "equal equals =")
_alias("comma", "comma , запятая")
_alias("period", "period dot . точка")
_alias("slash", "slash / слэш слеш")
_alias("backslash", "backslash \\ бэкслэш бекслеш")
_alias("semicolon", "semicolon ;")
_alias("quote", "quote apostrophe '")
_alias("backquote", "backquote backtick grave tilde ` ~")
_alias("bracket_left", "bracketleft leftbracket openbracket [")
_alias("bracket_right", "bracketright rightbracket closebracket ]")
_alias("num_add", "numadd numpadadd numpadplus numplus add num+")
_alias("num_subtract", "numsubtract numpadsubtract numpadminus numminus subtract num-")
_alias("num_multiply", "nummultiply numpadmultiply multiply num* *")
_alias("num_divide", "numdivide numpaddivide divide num/")
_alias("num_decimal", "numdecimal numpaddecimal decimal num.")

# русская раскладка: буква → клавиша, на которой она нарисована («ctrl+с» — это ctrl+c)
_RU_LAYOUT = {
    **dict(zip("йцукенгшщз", "qwertyuiop", strict=True)),
    **dict(zip("фывапролд", "asdfghjkl", strict=True)),
    **dict(zip("ячсмить", "zxcvbnm", strict=True)),
    "х": "bracket_left",
    "ъ": "bracket_right",
    "ж": "semicolon",
    "э": "quote",
    "б": "comma",
    "ю": "period",
    "ё": "backquote",
}

_SQUASH = re.compile(r"[\s_\-]+")
_FKEY = re.compile(r"[fф](\d{1,2})")
_NUMPAD = re.compile(r"(?:numpad|num)(\d)")
_REPEAT = re.compile(r"^(.+?)\*(\d{1,2})$")
# многословные имена из интерфейса («page up», «num +», «play/pause media») — до разбора на части
_SPACED = (
    (re.compile(r"\bnum\s*\+"), "num_add"),
    (re.compile(r"\bnum\s*-"), "num_subtract"),
    (re.compile(r"\bnum\s*\*"), "num_multiply"),
    (re.compile(r"\bnum\s*/"), "num_divide"),
    (re.compile(r"\bnum\s*\."), "num_decimal"),
    (re.compile(r"\b(?:num|numpad)\s+(\d)\b"), r"num\1"),
    (re.compile(r"\bplay/pause media\b"), "play_pause"),
    (re.compile(r"\bstop media\b"), "media_stop"),
    (
        re.compile(
            r"\b(page|caps|scroll|num|print|volume|next|prev|previous|arrow)\s+(up|down|lock|screen|mute|track)\b"
        ),
        r"\1_\2",
    ),
)


def key_name(name: str) -> str | None:
    """Имя одной клавиши → каноническое или None."""
    raw = name.strip().casefold()
    if not raw:
        return None
    if len(raw) == 1:
        if "a" <= raw <= "z" or "0" <= raw <= "9":
            return raw
        if raw in _RU_LAYOUT:
            return _RU_LAYOUT[raw]
        return _ALIASES.get(raw)
    if raw in KEYS:
        return raw
    squashed = _SQUASH.sub("", raw.replace("ё", "е"))
    if squashed in _ALIASES:
        return _ALIASES[squashed]
    if m := _FKEY.fullmatch(squashed):
        n = int(m.group(1))
        return f"f{n}" if 1 <= n <= 24 else None
    if m := _NUMPAD.fullmatch(squashed):
        return f"num{m.group(1)}"
    if squashed in ("previoustrack", "prevtrack"):
        return "prev_track"
    return None


def _prepare(value: str) -> str:
    s = value.strip().casefold()
    for pattern, repl in _SPACED:
        s = pattern.sub(repl, s)
    return re.sub(r"\s*\+\s*", "+", s)


def _split_combo(token: str) -> list[str]:
    if token == "+":
        return ["+"]
    trailing_plus = token.endswith("++")
    body = token[:-2] if trailing_plus else token
    parts = body.split("+") if body else []
    if any(not p for p in parts) or (not parts and not trailing_plus):
        raise ValueError(f"Неполное сочетание клавиш: «{token}»")
    return [*parts, "+"] if trailing_plus else parts


def parse_combo(combo: str) -> str:
    """«Ctrl + Shift + S» → «ctrl+shift+s»; неизвестная клавиша — ValueError с текстом для человека."""
    token = _prepare(combo)
    if not token:
        raise ValueError("Не указаны клавиши")
    keys: list[str] = []
    for part in _split_combo(token):
        name = key_name(part)
        if name is None:
            raise ValueError(f"Не понял клавишу «{part}»")
        if name not in keys:
            keys.append(name)
    mods = [k for k in keys if k in MODIFIERS]
    rest = [k for k in keys if k not in MODIFIERS]
    return "+".join(mods + rest)


def parse_sequence(value: str) -> list[tuple[str, int]]:
    """«ctrl+k ctrl+s», «ctrl+k, ctrl+s», «down*3» → [(сочетание, повторы)]. Ошибка — ValueError."""
    prepared = _prepare(value)
    tokens = [t for t in re.split(r"(?<!\+)[,;]|\s+", prepared) if t]
    if not tokens:
        raise ValueError("Не указаны клавиши")
    out: list[tuple[str, int]] = []
    for token in tokens:
        repeat = 1
        m = _REPEAT.match(token)
        if m and token != "*":
            token, repeat = m.group(1), int(m.group(2))
        if not 1 <= repeat <= 50:
            raise ValueError("Повторов клавиши — от 1 до 50")
        out.append((parse_combo(token), repeat))
    return out


def normalize_keys(value: str) -> str:
    """Последовательность в каноническом виде: «Ctrl + K, Ctrl + S» → «ctrl+k ctrl+s»."""
    return " ".join(combo if n == 1 else f"{combo}*{n}" for combo, n in parse_sequence(value))


def validate_keys(value: str) -> str | None:
    """Текст ошибки или None, если клавиши понятны (значение со слотом «{keys}» не проверяется)."""
    if "{" in value:
        return None
    try:
        parse_sequence(value)
    except ValueError as e:
        return str(e)
    return None


# --- для людей ------------------------------------------------------------------------------------

_DISPLAY = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
    "enter": "Enter",
    "space": "Пробел",
    "tab": "Tab",
    "esc": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "print_screen": "PrtSc",
    "caps_lock": "CapsLock",
    "num_lock": "NumLock",
    "scroll_lock": "ScrollLock",
    "pause": "Pause",
    "menu": "Меню",
    "volume_up": "Громкость +",
    "volume_down": "Громкость −",
    "volume_mute": "Без звука",
    "play_pause": "Play/Pause",
    "next_track": "Следующий трек",
    "prev_track": "Предыдущий трек",
    "media_stop": "Стоп",
    "plus": "+",
    "minus": "-",
    "equal": "=",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "quote": "'",
    "backquote": "`",
    "bracket_left": "[",
    "bracket_right": "]",
    "num_add": "Num +",
    "num_subtract": "Num −",
    "num_multiply": "Num *",
    "num_divide": "Num /",
    "num_decimal": "Num .",
}


def _display_key(key: str) -> str:
    if key in _DISPLAY:
        return _DISPLAY[key]
    if key.startswith("num") and key[3:].isdigit():
        return f"Num {key[3:]}"
    if key.startswith("browser_"):
        return "Браузер: " + key[8:]
    return key.upper()


def humanize(value: str) -> str:
    """«ctrl+shift+s» → «Ctrl+Shift+S», «down*3» → «↓ ×3»; непонятное значение — как есть."""
    try:
        seq = parse_sequence(value)
    except ValueError:
        return value
    parts = []
    for combo, repeat in seq:
        text = "+".join(_display_key(k) for k in combo.split("+"))
        parts.append(text if repeat == 1 else f"{text} ×{repeat}")
    return ", ".join(parts)


# --- клавиши словами -----------------------------------------------------------------------------

_SPOKEN_IGNORE = frozenset(
    _words(
        "клавишу клавиши клавиша клавишей кнопку кнопка кнопки сочетание сочетания комбинацию комбинация "
        "на и стрелку стрелка стрелочку стрелки затем потом"
    )
)
_LETTER_NAMES = {
    "эй": "a",
    "би": "b",
    "си": "c",
    "ди": "d",
    "эф": "f",
    "джи": "g",
    "эйч": "h",
    "ейч": "h",
    "ай": "i",
    "джей": "j",
    "кей": "k",
    "кэй": "k",
    "эл": "l",
    "эль": "l",
    "эм": "m",
    "эн": "n",
    "оу": "o",
    "пи": "p",
    "кью": "q",
    "ар": "r",
    "эр": "r",
    "эс": "s",
    "ти": "t",
    "ви": "v",
    "икс": "x",
    "уай": "y",
    "вай": "y",
    "зет": "z",
    "зед": "z",
}
# одиночная русская буква из распознавания — как её произносят латинскую («с» ← «эс»)
_SPOKEN_CYR = dict(zip("абвгдезиклмнопрстуфхцэ", "abvgdeziklmnoprstufhce", strict=True))
_BIGRAMS = {
    ("пейдж", "ап"): "page_up",
    ("пейдж", "даун"): "page_down",
    ("page", "up"): "page_up",
    ("page", "down"): "page_down",
    ("принт", "скрин"): "print_screen",
    ("print", "screen"): "print_screen",
    ("капс", "лок"): "caps_lock",
    ("caps", "lock"): "caps_lock",
    ("дабл", "ю"): "w",
    ("контекстное", "меню"): "menu",
}
_TIMES = frozenset({"раз", "раза"})


def parse_spoken(text: str) -> str | None:
    """Клавиши словами → последовательность: «контрол шифт эс» → «ctrl+shift+s», «вниз три раза» → «down*3».

    None — если хоть одно слово не похоже на клавишу.
    """
    words = [w for w in text.casefold().replace("ё", "е").replace("+", " ").split() if w]
    keys: list[str] = []
    repeat = 1
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if (w, nxt) in _BIGRAMS:
            keys.append(_BIGRAMS[(w, nxt)])
            i += 2
            continue
        if w in ("эф", "ф", "f") and nxt:
            n = UNITS.get(nxt) if not nxt.isdigit() else int(nxt)
            if n is None and nxt in ("десять", "одиннадцать", "двенадцать"):
                n = {"десять": 10, "одиннадцать": 11, "двенадцать": 12}[nxt]
            if n is not None and 1 <= n <= 24:
                keys.append(f"f{n}")
                i += 2
                continue
        if nxt in _TIMES and (nxt_n := (int(w) if w.isdigit() else UNITS.get(w))) is not None and keys:
            repeat = max(1, min(20, nxt_n))
            i += 2
            continue
        if w in _SPOKEN_IGNORE or (w == "плюс" and i + 1 < len(words)):
            i += 1
            continue
        name = _spoken_key(w)
        if name is None:
            return None
        keys.append(name)
        i += 1
    if not keys:
        return None
    mods = [k for k in dict.fromkeys(keys) if k in MODIFIERS]
    rest = [k for k in dict.fromkeys(keys) if k not in MODIFIERS]
    combo = "+".join(mods + rest)
    return combo if repeat == 1 else f"{combo}*{repeat}"


def _spoken_key(word: str) -> str | None:
    if word in _LETTER_NAMES:
        return _LETTER_NAMES[word]
    if len(word) == 1:
        if "a" <= word <= "z" or "0" <= word <= "9":
            return word
        return _SPOKEN_CYR.get(word)
    if word in UNITS and UNITS[word] <= 9:
        return str(UNITS[word])
    return key_name(word)
