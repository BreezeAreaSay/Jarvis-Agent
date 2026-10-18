"""Общие операции с текстом фраз: нормализация и слово-активатор «Джарвис»."""

import re

from rapidfuzz import fuzz

_SPACES = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s%+\-.:/]", re.UNICODE)
_EDGE = " ,.!?…:;—–-\"'«»"

# так распознавание слышит «Джарвис»: «джарис», «жарвис», «шарвис», «джервис», «дарвис»
WAKE_RATIO = 72


def normalize(text: str) -> str:
    """Для сравнения фраз: нижний регистр, «ё» → «е», без знаков препинания, одиночные пробелы."""
    t = text.casefold().replace("ё", "е")
    t = _PUNCT.sub(" ", t)
    t = t.replace("…", " ")
    return _SPACES.sub(" ", t).strip(" .-")


def clean(text: str) -> str:
    """Для показа: одиночные пробелы, без пробелов и знаков по краям."""
    return _SPACES.sub(" ", text).strip(_EDGE)


def _wake_score(word: str, wake_words: list[str]) -> float:
    """Насколько слово похоже на активатор: 100 — точно, 0 — не он."""
    w = normalize(word)
    if not w:
        return 0.0
    best = 0.0
    for wake in wake_words:
        target = normalize(wake)
        if not target:
            continue
        if w == target:
            return 100.0
        if w[0] in "джшчjзд" and abs(len(w) - len(target)) <= 2:
            score = fuzz.ratio(w, target)
            if score >= WAKE_RATIO:
                best = max(best, score)
    return best


def find_wake(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Есть ли в начале фразы слово-активатор; вернёт (найдено, остаток фразы).

    Активатор ищется в первых трёх словах («ну джарвис включи…», «эй джарвис…»), в том числе склеенным из двух
    слов («джар вис»). Остаток — текст после активатора.
    """
    words = text.split()
    for start in range(min(3, len(words))):
        best_score, best_n = 0.0, 0
        for n in (1, 2):
            if start + n > len(words):
                break
            score = _wake_score("".join(words[start : start + n]), wake_words)
            if score > best_score:
                best_score, best_n = score, n
        if best_n:
            return True, clean(" ".join(words[start + best_n :]))
    return False, clean(text)


def strip_wake(text: str, wake_words: list[str]) -> str:
    return find_wake(text, wake_words)[1]


STOP_PHRASES = (
    "стоп",
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


def is_stop(text: str) -> bool:
    t = normalize(text)
    return t in STOP_PHRASES or any(t == f"{p} пожалуйста" for p in STOP_PHRASES)
