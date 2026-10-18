"""Текстовые помощники команд: подготовка фразы, слова-паразиты, грубый стемминг, транслит, похожесть слов."""

import re
from functools import lru_cache

from rapidfuzz import fuzz

from jarvis import text as textops

_DECIMAL_COMMA = re.compile(r"(\d),(\d)")
_CYRILLIC = re.compile(r"[а-я]")

# «ну», «давай», «слушай», «будь добр»… в начале фразы — не часть команды
_LEAD = (
    "ну",
    "а",
    "и",
    "давай",
    "давайка",
    "слушай",
    "так",
    "ок",
    "окей",
    "ладно",
    "эй",
    "короче",
    "ой",
    "ага",
    "пожалуйста",
    "плиз",
    "будь добр",
    "будь добра",
    "будь любезен",
    "будь любезна",
    "можешь",
    "можешь ли",
    "ты можешь",
    "можешь ли ты",
    "не мог бы ты",
    "не могла бы ты",
    "мог бы ты",
    "могла бы ты",
    "сможешь",
    "скажи",
    "скажи мне",
    "подскажи",
    "подскажи мне",
    "теперь",
    "сделай",
    "сделайте",
    "мне нужно",
    "мне надо",
    "нужно",
    "надо",
    "хочу",
    "я хочу",
    "хочу чтобы ты",
    "я хочу чтобы ты",
    "срочно",
    "быстро",
    "быстренько",
    # связки в начале части составной фразы: «открой ютуб, затем сделай громче»
    "потом",
    "затем",
    "а потом",
    "и потом",
    "а затем",
    "и затем",
    "после этого",
    "после чего",
)
LEAD_FILLERS: tuple[tuple[str, ...], ...] = tuple(
    sorted({tuple(p.split()) for p in _LEAD}, key=len, reverse=True)
)
TRAIL_FILLERS = frozenset({"пожалуйста", "плиз", "спасибо", "сейчас", "срочно", "быстро", "быстрее"})
ANYWHERE_FILLERS = frozenset({"пожалуйста", "плиз", "пж", "пжлст", "please"})

QUESTION_WORDS = frozenset(
    {
        "что",
        "как",
        "почему",
        "зачем",
        "кто",
        "где",
        "когда",
        "сколько",
        "какой",
        "какая",
        "какое",
        "какие",
        "какую",
        "каким",
        "чем",
        "ли",
        "расскажи",
        "объясни",
        "куда",
        "откуда",
        "чей",
        "чья",
    }
)


def normalize(text: str) -> str:
    """normalize() ядра + «2,5» → «2.5» и «50 %» → «50%»."""
    t = textops.normalize(_DECIMAL_COMMA.sub(r"\1.\2", text))
    return re.sub(r"(\d)\s+%", r"\1%", t)


def strip_fillers(words: list[str], wake_words: frozenset[str] = frozenset()) -> list[str]:
    """Убрать обращение, «пожалуйста» и слова-паразиты по краям. Пустой итог — вернуть как было."""
    out = [w for w in words if w not in ANYWHERE_FILLERS]
    if out and out[0] in wake_words:
        out = out[1:]
    changed = True
    while changed and out:
        changed = False
        for filler in LEAD_FILLERS:
            n = len(filler)
            if len(out) > n and tuple(out[:n]) == filler:
                out = out[n:]
                changed = True
                break
    while len(out) > 1 and (out[-1] in TRAIL_FILLERS or out[-1] in wake_words):
        out = out[:-1]
    return out or [w for w in words if w not in ANYWHERE_FILLERS] or list(words)


def prepare(text: str, wake_words: frozenset[str] = frozenset()) -> list[str]:
    """Фраза человека → слова для сравнения с шаблонами."""
    return strip_fillers(normalize(text).split(), wake_words)


def _words(text: str) -> list[str]:
    return text.split()


# --- грубый стемминг: «паузу» и «пауза» → «пауз» ---------------------------------------------------

_ENDINGS = tuple(
    sorted(
        _words(
            "иями ями ами ого его ому ему ыми ими ешь ете ишь ите ает яет ует ют ут ят ат ет ит "
            "ая яя ое ее ые ие ой ей ий ый ую юю ом ем ам ям ах ях ов ев ть ти ии ию ия "
            "а я о е ы и у ю ь й"
        ),
        key=len,
        reverse=True,
    )
)


@lru_cache(maxsize=50_000)
def stem(word: str) -> str:
    if len(word) <= 3 or not _CYRILLIC.search(word):
        return word
    w = word
    if w.endswith(("ся", "сь")) and len(w) > 5:
        w = w[:-2]
    for ending in _ENDINGS:
        if w.endswith(ending) and len(w) - len(ending) >= 3:
            return w[: -len(ending)]
    return w


def stem_text(words: list[str] | tuple[str, ...]) -> str:
    return " ".join(stem(w) for w in words)


# слова с противоположным смыслом, которые пишутся почти одинаково: «включи» ≠ «выключи»
_OPPOSITES = (
    ("включ", "выключ"),
    ("включ", "отключ"),
    ("подключ", "отключ"),
    ("подключ", "выключ"),
    ("заблок", "разблок"),
    ("блокир", "разблок"),
    ("добав", "убав"),
    ("прибав", "убав"),
    ("замьют", "размьют"),
    ("замьют", "отмьют"),
)


def opposite(a: str, b: str) -> bool:
    """Слова — противоположности вроде «включи» / «выключи» (нечёткое сравнение их не путает)."""
    return any(
        (a.startswith(x) and b.startswith(y)) or (a.startswith(y) and b.startswith(x)) for x, y in _OPPOSITES
    )


@lru_cache(maxsize=100_000)
def word_sim(template_word: str, word: str) -> float:
    """Похожесть двух слов 0..100 с поправкой на окончания («минуту» ≈ «минут»); противоположности — 0."""
    if template_word == word:
        return 100.0
    if opposite(template_word, word):
        return 0.0
    score = fuzz.ratio(template_word, word)
    a, b = stem(template_word), stem(word)
    if a == b:
        return max(score, 95.0)
    return max(score, fuzz.ratio(a, b) - 5)


def word_threshold(template_word: str) -> float:
    """Сколько похожести нужно слову шаблона: короткие слова — только точно."""
    n = len(template_word)
    if n <= 2:
        return 100.0
    if n <= 4:
        return 75.0
    return 72.0


# --- транслит и «фонетика» для названий программ ------------------------------------------------------

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def translit(text: str) -> str:
    """Кириллица → латиница («спотифай» → «spotifay»)."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())


_PHONETIC_RULES = (
    ("ph", "f"),
    ("ck", "k"),
    ("qu", "kv"),
    ("x", "ks"),
    ("w", "v"),
    ("ea", "i"),
    ("ee", "i"),
    ("oo", "u"),
    ("kh", "h"),
    ("th", "t"),
    ("j", "dzh"),
    ("y", "i"),
)
_C_SOFT = re.compile(r"c(?=[eiy])")
_DOUBLE = re.compile(r"([a-z])\1+")


@lru_cache(maxsize=20_000)
def phonetic(text: str) -> str:
    """Латиница «как слышится»: photoshop → fotoshop, steam → stim; кириллицу сначала транслитерирует."""
    t = translit(text)
    t = t.replace("++", " plus plus")
    t = _C_SOFT.sub("s", t)
    t = t.replace("c", "k")
    for old, new in _PHONETIC_RULES:
        t = t.replace(old, new)
    t = _DOUBLE.sub(r"\1", t)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


def slugify(text: str, limit: int = 40) -> str:
    """Название → id-часть: «Режим стрима» → «rezhim_strima»."""
    t = re.sub(r"[^a-z0-9]+", "_", translit(normalize(text))).strip("_")
    return t[:limit].strip("_") or "command"
