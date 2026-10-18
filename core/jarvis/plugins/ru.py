"""Русские падежи для голосовых фраз плагинов: «включи люстру», «свет на кухне», «на ноутбуке …».

Правила простые и покрывают типичные названия устройств, комнат и компьютеров («настольная лампа»,
«гостиная», «детская комната», «игровой ПК»). Фразы всё равно сравниваются нечётко, поэтому плагины дают
командам и изменённую, и исходную форму названия.
"""

PREPOSITIONS = frozenset(
    {
        "в",
        "во",
        "на",
        "у",
        "над",
        "под",
        "за",
        "около",
        "возле",
        "с",
        "со",
        "для",
        "из",
        "от",
        "к",
        "по",
        "при",
    }
)
_HUSHING = "жшчщ"
_VELAR = "гкх"
_VOWELS = "аеёиоуыэюя"
_ADJ_ENDINGS = ("ая", "яя", "ый", "ий", "ой", "ое", "ее", "ые", "ие")
# слова на -а/-я, у которых винительный совпадает с именительным (множественное число)
_ACC_SAME = frozenset({"ворота", "сани", "часы", "шторы", "жалюзи", "двери", "окна", "колонки", "лампы"})
_PREP_SPECIAL = {"санузел": "санузле", "уголок": "уголке", "потолок": "потолке", "пк": "пк"}
# марки и приставки перед названием: «Яндекс Станция» → «Яндекс Станцию» (главное слово — дальше)
_MODIFIERS = frozenset({"яндекс", "сяоми", "аквара", "филипс", "самсунг", "смарт", "мини"})
# комнаты и места, про которые говорят «на»: на кухне, на балконе, на втором этаже
_NA_STEMS = (
    "кухн",
    "балкон",
    "лоджи",
    "веранд",
    "террас",
    "улиц",
    "дач",
    "чердак",
    "двор",
    "мансард",
    "этаж",
    "крыльц",
    "участк",
    "площадк",
)


def _cyrillic(word: str) -> bool:
    return bool(word) and all(("а" <= ch <= "я") or ch in "ё-" for ch in word)


def _has_vowel(word: str) -> bool:
    return any(ch in _VOWELS for ch in word)


def _adjective(word: str) -> str | None:
    """Окончание прилагательного или None (существительное)."""
    if len(word) < 4:
        return None
    for ending in _ADJ_ENDINGS:
        if word.endswith(ending):
            return ending
    return None


def accusative(name: str) -> str:
    """«настольная лампа» → «настольную лампу», «яндекс станция» → «яндекс станцию», «свет» → «свет».

    Меняются слова до первого существительного включительно (дальше — уточнения: «лампа в спальне»).
    """
    words = name.lower().split()
    out: list[str] = []
    for index, word in enumerate(words):
        if word in PREPOSITIONS:
            out.extend(words[index:])
            break
        adjective = _adjective(word) if _cyrillic(word) else None
        out.append(_accusative_word(word, adjective))
        if _cyrillic(word) and adjective is None and _has_vowel(word) and word not in _MODIFIERS:
            out.extend(words[index + 1 :])
            break
    return " ".join(out)


def _accusative_word(word: str, adjective: str | None) -> str:
    if not _cyrillic(word) or len(word) < 3 or word in _ACC_SAME or word in _MODIFIERS:
        return word
    if adjective == "ая":
        return word[:-2] + "ую"
    if adjective == "яя":
        return word[:-2] + "юю"
    if adjective:
        return word
    if word.endswith("а"):
        return word[:-1] + "у"
    if word.endswith("я"):
        return word[:-1] + "ю"
    return word


def prepositional(name: str) -> str:
    """«кухня» → «кухне», «гостиная» → «гостиной», «второй этаж» → «втором этаже», «ноутбук» → «ноутбуке»."""
    words = name.lower().split()
    out: list[str] = []
    for index, word in enumerate(words):
        if word in PREPOSITIONS:
            out.extend(words[index:])
            break
        adjective = _adjective(word) if _cyrillic(word) else None
        out.append(_prepositional_word(word, adjective))
        if _cyrillic(word) and adjective is None and _has_vowel(word) and word not in _MODIFIERS:
            out.extend(words[index + 1 :])
            break
    return " ".join(out)


def _prepositional_word(word: str, adjective: str | None) -> str:
    if word in _PREP_SPECIAL:
        return _PREP_SPECIAL[word]
    if word in _MODIFIERS:
        return word
    if not _cyrillic(word) or len(word) < 2 or not _has_vowel(word):
        return word
    if adjective:
        stem = word[:-2]
        if adjective == "ая":
            return stem + ("ей" if stem[-1] in _HUSHING else "ой")
        if adjective == "яя":
            return stem + "ей"
        if adjective in ("ый", "ой", "ое"):
            return stem + "ом"
        if adjective == "ий":
            return stem + ("ом" if stem[-1] in _VELAR else "ем")
        if adjective == "ее":
            return stem + "ем"
        return stem + ("ых" if adjective == "ые" else "их")
    if word.endswith(("ия", "ие")):
        return word[:-1] + "и"
    if word.endswith(("а", "я", "о")):
        return word[:-1] + "е"
    if word.endswith("ь"):
        return word[:-1] + "и"
    if word.endswith("й"):
        return word[:-1] + "е"
    if word.endswith(("ы", "и", "е", "у", "ю", "э")):
        return word
    return word + "е"


def place(name: str) -> str:
    """Где: «кухня» → «на кухне», «спальня» → «в спальне», «второй этаж» → «на втором этаже»."""
    prep = prepositional(name)
    first = prep.split()[0] if prep else ""
    if any(stem in prep for stem in _NA_STEMS):
        return f"на {prep}"
    if first[:1] in ("в", "ф") and len(first) > 1 and first[1] not in _VOWELS:
        return f"во {prep}"
    return f"в {prep}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Форма слова для числа: plural(5, "уведомление", "уведомления", "уведомлений") → «уведомлений»."""
    n = abs(count) % 100
    if 10 < n < 20:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many
