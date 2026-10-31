"""Время напоминания словами — слот {when} команд: «завтра в 9», «в 18:30», «сегодня вечером», «в пятницу в 10
утра», «послезавтра в полдень», «30 октября в 15:00», «в эту субботу», «в девять тридцать вечера».

parse_moment(слова, сейчас) — момент не в прошлом или None, если слова не целиком время (тогда команда не
подходит и фраза уходит дальше — к другим командам или ИИ). moment() — то же, но и уже прошедший момент
(«сегодня в 9 утра» в полдень): слот {when} берёт фразу времени целиком, а исполнитель отвечает «это время уже
прошло». «Через N минут» сюда не входит — это свои команды.
Час без «утра/вечера» — ближайший такой на часах: в 15:00 «в 7» — это 19:00, в 6:00 — 7:00.
"""

import re
from datetime import date, datetime, timedelta

from jarvis.commands import numbers

WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6,
}
MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
# часть суток без часа → час по умолчанию
PARTS = {"утром": 9, "днем": 13, "днём": 13, "вечером": 19, "ночью": 22}
# «в 7 вечера», «в 2 дня», «в 11 ночи», «в 9 утра»
SUFFIXES = ("утра", "дня", "вечера", "ночи")
THIS = ("эту", "этот", "это", "ближайшую", "ближайший", "ближайшее")
NEXT = ("следующую", "следующий", "следующее")
HOUR_WORDS = ("час", "часа", "часов")
MINUTE_WORDS = ("минут", "минуты", "минуту")
DEFAULT_HOUR = 9
_CLOCK = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def _number(words: list[str], i: int) -> tuple[int, int] | None:
    """Целое число со слова i: (значение, следующее слово)."""
    if i >= len(words):
        return None
    for value, end in numbers.parse_at(words, i):
        if float(value).is_integer() and value >= 0:
            return int(value), end
    return None


def _suffix(hour: int, suffix: str) -> int | None:
    if suffix == "утра":
        return hour % 12 if hour <= 12 else None
    if suffix == "дня":
        return hour + 12 if 1 <= hour <= 5 else hour if 12 <= hour <= 17 else None
    if suffix == "вечера":
        return hour + 12 if 1 <= hour <= 11 else hour if 12 <= hour <= 23 else None
    if suffix == "ночи":
        return hour + 12 if 9 <= hour <= 11 else 0 if hour == 12 else hour if hour <= 5 else None
    return None


def _clock(words: list[str], i: int) -> tuple[int, int, bool, int, bool] | None:
    """Время после «в»: (час, минуты, сказано ли «утра/вечера»…, следующее слово, ночь).

    Ночь — «в 2 ночи» или «в полночь»: сказанное поздно вечером значит следующие сутки."""
    if i >= len(words):
        return None
    word = words[i]
    if word == "полдень":
        return 12, 0, True, i + 1, False
    if word == "полночь":
        return 0, 0, True, i + 1, True
    m = _CLOCK.match(word)
    if m:
        hour, minute, end = int(m.group(1)), int(m.group(2)), i + 1
    else:
        found = _number(words, i)
        if found is None:
            return None
        hour, end = found
        minute = 0
        if end < len(words) and words[end] in HOUR_WORDS:
            end += 1
        more = _number(words, end)
        if (
            more is not None
            and 0 <= more[0] <= 59
            and (more[1] >= len(words) or words[more[1]] not in HOUR_WORDS)
        ):
            minute, end = more
            if end < len(words) and words[end] in MINUTE_WORDS:
                end += 1
    if hour > 23 or minute > 59:
        return None
    exact = hour == 0 or hour >= 13
    night = False
    if end < len(words) and words[end] in SUFFIXES:
        adjusted = _suffix(hour, words[end])
        if adjusted is None:
            return None
        night = words[end] == "ночи" and adjusted < 6
        hour, exact, end = adjusted, True, end + 1
    return hour, minute, exact, end, night


def _weekday(today: date, weekday: int, *, next_week: bool) -> date:
    ahead = (weekday - today.weekday()) % 7
    if next_week:
        ahead = ahead + 7 if ahead == 0 else ahead
        if ahead < 7 and (today + timedelta(days=ahead)).isocalendar()[1] == today.isocalendar()[1]:
            ahead += 7  # «в следующую пятницу» в понедельник — пятница следующей недели
    return today + timedelta(days=ahead)


def moment(words: list[str], now: datetime | None = None) -> datetime | None:
    """Слова целиком — момент напоминания (может быть и в прошлом); не время — None."""
    now = now or datetime.now()
    today = now.date()
    day: date | None = None
    weekday = -1
    hour: int | None = None
    minute = 0
    exact = False
    night = False
    said_today = False  # «сегодня в 2 ночи» — ближайшая ночь; «30 октября в 2 ночи» — ровно та дата
    part = 0
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        if day is None and w in ("сегодня", "завтра", "послезавтра"):
            day = today + timedelta(days=("сегодня", "завтра", "послезавтра").index(w))
            said_today = w == "сегодня"
            i += 1
        elif day is None and w in ("в", "во") and i + 1 < n and words[i + 1] in WEEKDAYS:
            weekday = WEEKDAYS[words[i + 1]]
            day = _weekday(today, weekday, next_week=False)
            i += 2
        elif (
            day is None
            and w in ("в", "во")
            and i + 2 < n
            and words[i + 1] in THIS + NEXT
            and words[i + 2] in WEEKDAYS
        ):
            weekday = WEEKDAYS[words[i + 2]]
            day = _weekday(today, weekday, next_week=words[i + 1] in NEXT)
            i += 3
        elif (
            day is None
            and (found := _number(words, i)) is not None
            and found[1] < n
            and words[found[1]] in MONTHS
        ):
            month = MONTHS[words[found[1]]]
            try:
                day = date(today.year, month, found[0])
                if day < today:
                    day = day.replace(year=today.year + 1)
            except ValueError:  # 31 апреля; 29 февраля, когда в следующем году его нет
                return None
            i = found[1] + 1
        elif hour is None and w in ("в", "во", "к") and (clock := _clock(words, i + 1)) is not None:
            hour, minute, exact, i, night = clock
        elif hour is None and not part and w in PARTS:
            part = PARTS[w]
            i += 1
        else:
            return None
    if day is None and hour is None and not part:
        return None
    if hour is None:
        hour, exact = part or DEFAULT_HOUR, True
    elif part and not exact:
        adjusted = _suffix(hour, {9: "утра", 13: "дня", 19: "вечера", 22: "ночи"}[part])
        hour, exact = (adjusted, True) if adjusted is not None else (hour, exact)
    at = datetime.combine(day or today, datetime.min.time()).replace(hour=hour, minute=minute)
    if not exact and at <= now and at + timedelta(hours=12) > now:
        at += timedelta(hours=12)  # «в 7» в три часа дня — это 19:00
    if night and at <= now and (day is None or said_today):
        at += timedelta(days=1)  # «сегодня в 2 ночи», «в полночь» днём — ближайшая ночь
    if at <= now and not (day is not None and weekday < 0):  # «сегодня в 9 утра» в полдень — прошло
        at += timedelta(days=7 if weekday >= 0 else 1)
    return at


def parse_moment(words: list[str], now: datetime | None = None) -> datetime | None:
    """Слова целиком — момент напоминания не в прошлом; иначе None."""
    now = now or datetime.now()
    found = moment(words, now)
    return found if found is not None and found > now else None


def parse_text(text: str, now: datetime | None = None) -> datetime | None:
    return parse_moment(text.split(), now)


MAX_WORDS = 8  # «в следующую пятницу в девять тридцать вечера» — 7 слов


def longest_at(words: list[str], start: int, now: datetime | None = None) -> int | None:
    """Конец самой длинной фразы времени с слова start (до MAX_WORDS слов) или None: время — целиком, а не
    его начало («в 7» из «в 7 вечера» было бы утром)."""
    for end in range(min(len(words), start + MAX_WORDS), start, -1):
        if moment(words[start:end], now) is not None:
            return end
    return None
