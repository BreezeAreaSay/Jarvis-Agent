"""Общие операции с текстом фраз: нормализация и слово-активатор «Джарвис»."""

import re

from rapidfuzz import fuzz

_SPACES = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s%+\-.:/]", re.UNICODE)
_EDGE = " ,.!?…:;—–-\"'«»"

# так распознавание слышит «Джарвис»: «джарис», «жарвис», «шарвис», «джервис», «дарвис»
WAKE_RATIO = 72

# первые звуки, которые распознавание путает: «пятницу» ↔ «бятница», «окей» ↔ «акей»; «дж…» — отдельно
_SOUND_GROUPS = ("дт", "зс", "бп", "вф", "гкх", "аоя", "еэиы", "ую")
_HUSH = "джшщчзj"  # «Джарвис» слышится и как «жарвис», «шарвис», «чарвис», «зарвис», «дарвис»
# латиница → кириллица на слух: Whisper на другом языке пишет «Jarvis», а активатор — «джарвис»
_LATIN_PAIRS = (
    ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("th", "т"), ("ph", "ф"),
    ("ck", "к"), ("ee", "и"), ("oo", "у"), ("ya", "я"), ("yu", "ю"), ("yo", "е"), ("ye", "е"), ("ay", "ей"),
    ("ey", "ей"),
)  # fmt: skip
_LATIN = str.maketrans(
    {
        "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
        "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
        "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
    }
)  # fmt: skip
_HAS_LATIN = re.compile(r"[a-z]")


def normalize(text: str) -> str:
    """Для сравнения фраз: нижний регистр, «ё» → «е», без знаков препинания, одиночные пробелы."""
    t = text.casefold().replace("ё", "е")
    t = _PUNCT.sub(" ", t)
    t = t.replace("…", " ")
    return _SPACES.sub(" ", t).strip(" .-")


def clean(text: str) -> str:
    """Для показа: одиночные пробелы, без пробелов и знаков по краям."""
    return _SPACES.sub(" ", text).strip(_EDGE)


def wake_form(text: str) -> str:
    """Активатор или услышанное — для сравнения: normalize и латиница кириллицей («Jarvis» → «джарвис»)."""
    t = normalize(text)
    if not _HAS_LATIN.search(t):
        return t
    for latin, cyr in _LATIN_PAIRS:
        t = t.replace(latin, cyr)
    return t.translate(_LATIN)


def first_sounds(target: str) -> str:
    """С каких букв может начинаться услышанный активатор: его первая буква и те, с которыми её путают."""
    if not target:
        return ""
    if target.startswith("дж") or target[0] in "жшщч":
        return _HUSH
    first = target[0]
    return first + "".join(g for g in _SOUND_GROUPS if first in g)


def _similar(heard: str, target: str) -> float:
    """Похожесть услышанного (без пробелов) на активатор: 100 — точно, 0 — не он."""
    if heard == target:
        return 100.0
    if not heard or heard[0] not in first_sounds(target):
        return 0.0
    if abs(len(heard) - len(target)) > max(2, len(target) // 5):
        return 0.0
    return fuzz.ratio(heard, target)


def wake_parts(wake_words: list[str]) -> list[list[str]]:
    """Активаторы по словам, в форме для сравнения: «Эй, Джарвис» → ["эй", "джарвис"]."""
    out = []
    for wake in wake_words:
        parts = wake_form(wake).split()
        if parts:
            out.append(parts)
    return out


def _wake_score(heard: list[str], targets: list[list[str]]) -> float:
    """Насколько слова фразы похожи на активатор: 100 — точно, 0 — не он.

    Фраза из нескольких слов сравнивается целиком без пробелов («эй джарвис» ↔ «эйджарвис»: распознавание
    то склеивает, то разрывает слова) и по словам («окей компьютер» ↔ «акей кампьютер»).
    """
    words = [w for w in heard if w]
    if not words:
        return 0.0
    best = 0.0
    joined = "".join(words)
    for parts in targets:
        score = _similar(joined, "".join(parts))
        if score < WAKE_RATIO and len(parts) > 1 and len(words) == len(parts):
            each = [_similar(a, b) for a, b in zip(words, parts, strict=True)]
            if min(each) >= WAKE_RATIO:
                score = min(each)
        if score >= WAKE_RATIO:
            best = max(best, score)
    return best


def find_wake(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Есть ли в начале фразы слово-активатор; вернёт (найдено, остаток фразы).

    Активатор ищется в первых трёх словах («ну джарвис включи…», «эй джарвис…»), в том числе склеенным из
    нескольких слов («джар вис»). Своё слово («пятница») узнаётся и с окончаниями и искажениями («пятницу»),
    латиница — на слух («Jarvis» для «джарвис»). Остаток — текст после активатора.
    """
    words = text.split()
    targets = wake_parts(wake_words)
    if not targets:
        return False, clean(text)
    heard = [wake_form(w) for w in words]
    longest = max(len(parts) for parts in targets) + 1
    for start in range(min(3, len(words))):
        best_score, best_n = 0.0, 0
        for n in range(1, longest + 1):
            if start + n > len(words):
                break
            score = _wake_score(heard[start : start + n], targets)
            if score > best_score:
                best_score, best_n = score, n
        if best_n:
            return True, clean(" ".join(words[start + best_n :]))
    return False, clean(text)


def strip_wake(text: str, wake_words: list[str]) -> str:
    return find_wake(text, wake_words)[1]


STOP_PHRASES = (
    "стоп",
    "стой",
    "хватит",
    "замолчи",
    "помолчи",
    "тихо",
    "заткнись",
    "прекрати",
    "остановись",
    "отмена",
    "stop",
)
_STOP_FILLER = frozenset({"пожалуйста", "немедленно", "срочно", "уже", "же", "все", "сейчас"})
# первым словом фразы — «стоп» наверняка («стоп, не надо»); «тихо» и «отмена» первым словом — бывает и не стоп
STRONG_STOP = frozenset({"стоп", "стой", "stop", "хватит", "прекрати", "остановись"})


def is_stop(text: str) -> bool:
    """«Стоп», «стоп стоп стоп», «хватит уже», «стой, пожалуйста» — вся фраза — просьба остановиться."""
    words = normalize(text).split()
    if not words or words[0] not in STOP_PHRASES:
        return False
    return all(w in STOP_PHRASES or w in _STOP_FILLER for w in words)


def starts_with_stop(text: str) -> bool:
    """Фраза начинается со «стоп» («стоп, не надо», «хватит, я сам») — не «стоп-кран» и не «тихо включи…»."""
    first = clean(text).split()[0].strip(_EDGE).casefold() if clean(text) else ""
    return is_stop(text) or first in STRONG_STOP
