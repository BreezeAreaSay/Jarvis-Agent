"""Числа во фразах: словами во всех падежах («двадцать пять», «пятидесяти», «одну»), цифрами («50», «50%»,
«2.5») и особыми словами («половину» → 50, «максимум» / «на полную» → 100, «минимум» → 0, «полтора» → 1.5).

parse_at() разбирает число с заданного слова и отдаёт варианты (значение, конец) — от самого длинного
к короткому, чтобы сопоставитель фраз мог оставить слово «процентов» шаблону, если оно там есть.
"""

import re

UNITS: dict[str, int] = {}
TEENS: dict[str, int] = {}
TENS: dict[str, int] = {}
HUNDREDS: dict[str, int] = {}


def _add(table: dict[str, int], value: int, forms: str) -> None:
    for form in forms.split():
        table[form] = value


_add(UNITS, 0, "ноль нуль нуля нулю нулем нуле")
_add(UNITS, 1, "один одна одно одну одного одной одному одним одном")
_add(UNITS, 2, "два две двух двум двумя")
_add(UNITS, 3, "три трех трем тремя")
_add(UNITS, 4, "четыре четырех четырем четырьмя")
_add(UNITS, 5, "пять пяти пятью")
_add(UNITS, 6, "шесть шести шестью")
_add(UNITS, 7, "семь семи семью")
_add(UNITS, 8, "восемь восьми восемью восьмью")
_add(UNITS, 9, "девять девяти девятью")
_add(TEENS, 10, "десять десяти десятью")
_add(TEENS, 11, "одиннадцать одиннадцати одиннадцатью")
_add(TEENS, 12, "двенадцать двенадцати двенадцатью")
_add(TEENS, 13, "тринадцать тринадцати тринадцатью")
_add(TEENS, 14, "четырнадцать четырнадцати четырнадцатью")
_add(TEENS, 15, "пятнадцать пятнадцати пятнадцатью")
_add(TEENS, 16, "шестнадцать шестнадцати шестнадцатью")
_add(TEENS, 17, "семнадцать семнадцати семнадцатью")
_add(TEENS, 18, "восемнадцать восемнадцати восемнадцатью")
_add(TEENS, 19, "девятнадцать девятнадцати девятнадцатью")
_add(TENS, 20, "двадцать двадцати двадцатью")
_add(TENS, 30, "тридцать тридцати тридцатью")
_add(TENS, 40, "сорок сорока")
_add(TENS, 50, "пятьдесят пятидесяти пятьюдесятью")
_add(TENS, 60, "шестьдесят шестидесяти шестьюдесятью")
_add(TENS, 70, "семьдесят семидесяти семьюдесятью")
_add(TENS, 80, "восемьдесят восьмидесяти восемьюдесятью восьмьюдесятью")
_add(TENS, 90, "девяносто девяноста")
_add(HUNDREDS, 100, "сто ста сотня сотню сотни")
_add(HUNDREDS, 200, "двести двухсот двумстам двумястами двухстах")
_add(HUNDREDS, 300, "триста трехсот тремстам тремястами трехстах")
_add(HUNDREDS, 400, "четыреста четырехсот четыремстам четырьмястами четырехстах")
_add(HUNDREDS, 500, "пятьсот пятисот пятистам пятьюстами пятистах")
_add(HUNDREDS, 600, "шестьсот шестисот шестистам шестьюстами шестистах")
_add(HUNDREDS, 700, "семьсот семисот семистам семьюстами семистах")
_add(HUNDREDS, 800, "восемьсот восьмисот восьмистам восемьюстами восьмистах")
_add(HUNDREDS, 900, "девятьсот девятисот девятистам девятьюстами девятистах")

THOUSANDS = frozenset(["тысяча", "тысячи", "тысячу", "тысяч", "тысячей", "тысячам", "тысячами", "тысячах"])

# слова-числа сами по себе: «громкость наполовину», «яркость на максимум»
SPECIAL: dict[str, float] = {}


def _special(value: float, forms: str) -> None:
    for form in forms.split():
        SPECIAL[form] = value


_special(1.5, "полтора полторы полутора")
_special(50.0, "половина половину половины половине наполовину")
_special(
    100.0,
    "максимум максимума максимуме максимальную максимальная максимальный максимально максималку "
    "полную полная полной полнейшую фулл",
)
_special(0.0, "минимум минимума минимуме минимальную минимальная минимальный минимально")

UNIT_WORDS = frozenset({"%", "процент", "процента", "процентов", "процентах", "проц"})
HALF_WORDS = frozenset({"половиной", "половинкой"})

_DIGITS = re.compile(r"(\d+(?:\.\d+)?)(%?)")


def _parse_words(words: list[str], i: int) -> tuple[int, int]:
    """Число словами с позиции i → (значение, позиция после числа); ничего — (0, i)."""
    total = 0
    current = 0
    rank = 4  # 3 — сотни, 2 — десятки, 1 — единицы; каждая следующая часть должна быть «младше»
    had_thousands = False
    j = i
    while j < len(words):
        w = words[j]
        if w in HUNDREDS and rank > 3:
            current += HUNDREDS[w]
            rank = 3
        elif w in TENS and rank > 2:
            current += TENS[w]
            rank = 2
        elif w in TEENS and rank > 2:
            current += TEENS[w]
            rank = 1
        elif w in UNITS and rank > 1:
            value = UNITS[w]
            if value == 0 and (j > i):
                break  # «ноль» — только отдельным словом
            current += value
            rank = 1
            if value == 0:
                j += 1
                break
        elif w in THOUSANDS and not had_thousands and (j > i or w == "тысяча" or w == "тысячу"):
            total += (current or 1) * 1000
            current = 0
            rank = 4
            had_thousands = True
        else:
            break
        j += 1
    return total + current, j


def parse_at(words: list[str], i: int) -> list[tuple[float, int]]:
    """Варианты числа, начиная со слова i: [(значение, индекс после числа)], самые длинные — первыми."""
    if i >= len(words):
        return []
    w = words[i]
    variants: list[tuple[float, int]] = []
    m = _DIGITS.fullmatch(w)
    if m:
        variants.append((float(m.group(1)), i + 1))
        if m.group(2):
            return variants
    elif w in SPECIAL:
        variants.append((SPECIAL[w], i + 1))
    else:
        value, j = _parse_words(words, i)
        if j == i:
            return []
        variants.append((float(value), j))
        if j + 1 < len(words) and words[j] == "с" and words[j + 1] in HALF_WORDS:
            variants.insert(0, (value + 0.5, j + 2))
    with_units = [(v, end + 1) for v, end in variants if end < len(words) and words[end] in UNIT_WORDS]
    return sorted(with_units + variants, key=lambda item: -item[1])


def parse_number(text: str) -> float | None:
    """Вся строка — число: «двадцать пять» → 25, «50%» → 50, «на полную» → None (лишнее слово)."""
    words = text.split()
    for value, end in parse_at(words, 0):
        if end == len(words):
            return value
    return None


def find_number(words: list[str]) -> tuple[float, int, int] | None:
    """Первое число во фразе: (значение, начало, конец) или None."""
    for i in range(len(words)):
        variants = parse_at(words, i)
        if variants:
            value, end = variants[0]
            return value, i, end
    return None


def fmt_number(value: float) -> str:
    """50.0 → «50», 1.5 → «1.5»."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def plural(n: float, forms: tuple[str, str, str]) -> str:
    """Форма слова для числа: plural(5, ("минута", "минуты", "минут")) → «минут»."""
    if not float(n).is_integer():
        return forms[1]
    k = abs(int(n))
    if k % 10 == 1 and k % 100 != 11:
        return forms[0]
    if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14:
        return forms[1]
    return forms[2]


def count_text(n: float, forms: tuple[str, str, str]) -> str:
    """«5 минут», «1 секунда», «1.5 часа»."""
    return f"{fmt_number(n)} {plural(n, forms)}"


SECONDS = ("секунда", "секунды", "секунд")
SECONDS_ACC = ("секунду", "секунды", "секунд")
MINUTES = ("минута", "минуты", "минут")
MINUTES_ACC = ("минуту", "минуты", "минут")
HOURS = ("час", "часа", "часов")
PERCENT = ("процент", "процента", "процентов")
DEGREES = ("градус", "градуса", "градусов")


def duration_text(seconds: float, accusative: bool = False) -> str:
    """Длительность словами: 330 → «5 минут 30 секунд», 3600 → «1 час» (accusative — «на 1 минуту»)."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(count_text(hours, HOURS))
    if minutes:
        parts.append(count_text(minutes, MINUTES_ACC if accusative else MINUTES))
    if secs or not parts:
        parts.append(count_text(secs, SECONDS_ACC if accusative else SECONDS))
    return " ".join(parts)


_DURATION = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(ms|мс|s|с|сек|m|м|мин|h|ч)?\s*$", re.IGNORECASE)
_DURATION_MUL = {
    "ms": 0.001,
    "мс": 0.001,
    "s": 1,
    "с": 1,
    "сек": 1,
    "m": 60,
    "м": 60,
    "мин": 60,
    "h": 3600,
    "ч": 3600,
}


def parse_duration(value: str, default_unit: str = "s") -> float | None:
    """«2» → 2 с, «500ms» → 0.5, «5m» → 300, «1h» → 3600; неверная строка — None."""
    m = _DURATION.match(value or "")
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or default_unit).lower()
    return number * _DURATION_MUL.get(unit, 1)
