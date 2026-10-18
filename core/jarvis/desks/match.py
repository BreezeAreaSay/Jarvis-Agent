"""Поиск стола по названию из речи: «игры», «стол игры», «игровой» → «Игры».

Сначала точное совпадение, затем сравнение слов по основе (общее начало слова: «игр-ы» / «игр-овой»,
«рабо-та» / «рабо-чий»), затем нечёткое сравнение rapidfuzz (опечатки распознавания).
"""

from collections.abc import Iterable
from typing import Any

from rapidfuzz import fuzz

from jarvis.text import normalize

THRESHOLD = 75.0

# слова из команды, которые не относятся к названию стола
FILLER = frozenset(
    {
        "стол",
        "стола",
        "столу",
        "столом",
        "столе",
        "столы",
        "открой",
        "открыть",
        "откройте",
        "покажи",
        "показать",
        "переключи",
        "переключись",
        "переключиться",
        "перейди",
        "перейти",
        "включи",
        "на",
        "в",
        "во",
        "мой",
        "мою",
        "мои",
        "страницу",
        "страница",
        "вкладку",
        "вкладка",
        "экран",
        "пожалуйста",
        "джарвис",
        "и",
        "с",
        "для",
        "по",
    }
)


def words(text: str) -> list[str]:
    return [w for w in normalize(text).split() if w not in FILLER]


def common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def same_stem(a: str, b: str) -> bool:
    """Слова с общей основой: отличаются только окончанием («игры» / «игровой», «умного» / «умный»)."""
    if a == b:
        return True
    prefix = common_prefix(a, b)
    return prefix >= 3 and prefix >= min(len(a), len(b)) - 2


def score(query_words: list[str], name: str, aliases: Iterable[str] = ()) -> float:
    """Насколько фраза похожа на стол: 100 — точно он, 0 — совсем не он."""
    if not query_words:
        return 0.0
    query = " ".join(query_words)
    best = 0.0
    for candidate, weight in [(name, 1.0), *((alias, 0.9) for alias in aliases)]:
        cand_words = words(candidate)
        if not cand_words:
            continue
        text = " ".join(cand_words)
        if query == text:
            best = max(best, 100.0 * weight)
            continue
        matched = sum(1 for q in query_words if any(same_stem(q, c) for c in cand_words))
        stem = 92.0 * matched / len(query_words)
        fuzzy = max(fuzz.ratio(query, text), fuzz.token_sort_ratio(query, text))
        best = max(best, max(stem, fuzzy) * weight)
    return best


def best_match(
    query: str, desks: list[dict[str, Any]], aliases: dict[str, Iterable[str]]
) -> dict[str, Any] | None:
    """Стол, на который похожа фраза, или None. Сначала точное имя, потом лучший по score() ≥ THRESHOLD."""
    plain = normalize(query)
    if not plain:
        return None
    for desk in desks:
        if normalize(desk["name"]) == plain:
            return desk
    query_words = words(query)
    if not query_words:
        return None
    ranked: list[tuple[float, int, int, dict[str, Any]]] = []
    for index, desk in enumerate(desks):
        value = score(query_words, desk["name"], aliases.get(desk["id"], ()))
        if value >= THRESHOLD:
            ranked.append((value, -abs(len(desk["name"]) - len(query)), -index, desk))
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return ranked[0][3]
