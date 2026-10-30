"""Повторяющиеся фоновые задачи: расписание, следующий запуск и разбор фраз «каждый вечер …».

Расписание (Task.repeat) — словарь:
- {"kind": "daily", "at": "21:00", "weekdays": []} — в at по дням weekdays (0 — понедельник; пусто — каждый
  день);
- {"kind": "hours", "hours": 3} — каждые 3 часа (от начала прошлого запуска), от 1 до 168.

Время — местное время компьютера; now — unix-секунды (сервис подставляет свои часы, тесты — свои).
Пропущенные запуски (компьютер был выключен) не наверстываются по одному: запуск один, следующий — по
расписанию от «сейчас».
"""

import re
from datetime import datetime, timedelta
from typing import Any

from jarvis.scenarios.model import WEEKDAYS_SHORT, WEEKEND, WORKDAYS, clean_weekdays, fmt_hhmm, parse_hhmm
from jarvis.services import UserFacingError

MAX_HOURS = 168
DEFAULT_AT = "09:00"
PARTS_OF_DAY = {"утр": "09:00", "дн": "13:00", "обед": "13:00", "вечер": "20:00", "ноч": "23:00"}
WEEKDAY_WORDS = (
    ("понедельник", 0),
    ("вторник", 1),
    ("сред", 2),
    ("четверг", 3),
    ("пятниц", 4),
    ("суббот", 5),
    ("воскресень", 6),
)
WEEKDAYS_IN = ("по понедельникам", "по вторникам", "по средам", "по четвергам", "по пятницам", "по субботам",
               "по воскресеньям")  # fmt: skip


def clean_repeat(raw: Any) -> dict[str, Any]:
    """Проверить расписание из API или от модели; пусто — {} (задача не повторяется)."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise UserFacingError("Расписание задачи — объект {kind, at, weekdays} или {kind, hours}")
    kind = str(raw.get("kind") or "").strip()
    if kind == "daily":
        hm = parse_hhmm(raw.get("at") or DEFAULT_AT)
        if hm is None:
            raise UserFacingError("Время повтора — ЧЧ:ММ, например 21:00")
        return {"kind": "daily", "at": fmt_hhmm(*hm), "weekdays": clean_weekdays(raw.get("weekdays"))}
    if kind == "hours":
        try:
            hours = int(float(str(raw.get("hours") or "").replace(",", ".")))
        except ValueError:
            raise UserFacingError("Повтор раз в несколько часов: hours — число от 1 до 168") from None
        if not 1 <= hours <= MAX_HOURS:
            raise UserFacingError("Повтор раз в несколько часов: от 1 до 168 часов")
        return {"kind": "hours", "hours": hours}
    raise UserFacingError("Неизвестный повтор — нужен daily (в какое-то время) или hours (каждые N часов)")


def next_run(repeat: dict[str, Any], after: float) -> float:
    """Следующий запуск строго позже after (unix-секунды); не повторяется — 0."""
    if not repeat:
        return 0.0
    if repeat["kind"] == "hours":
        return after + int(repeat["hours"]) * 3600
    hm = parse_hhmm(repeat.get("at")) or (9, 0)
    days = repeat.get("weekdays") or []
    start = datetime.fromtimestamp(after)
    for ahead in range(0, 8):
        day = (start + timedelta(days=ahead)).date()
        if days and day.weekday() not in days:
            continue
        moment = datetime(day.year, day.month, day.day, hm[0], hm[1]).timestamp()
        if moment > after:
            return moment
    return after + 24 * 3600  # не бывает: в неделе есть хотя бы один нужный день


def describe(repeat: dict[str, Any]) -> str:
    """Расписание словами: «каждый день в 21:00», «по будням в 9:00», «по Пн, Ср в 10:00», «каждые 3 часа»."""
    if not repeat:
        return ""
    if repeat["kind"] == "hours":
        hours = int(repeat["hours"])
        if hours == 1:
            return "каждый час"
        if hours % 24 == 0:
            days = hours // 24
            return "раз в сутки" if days == 1 else f"раз в {days} {_plural(days, 'день', 'дня', 'дней')}"
        return f"каждые {hours} {_plural(hours, 'час', 'часа', 'часов')}"
    hm = parse_hhmm(repeat.get("at")) or (9, 0)
    at = f"{hm[0]}:{hm[1]:02d}"
    days = sorted(repeat.get("weekdays") or [])
    if not days:
        return f"каждый день в {at}"
    if days == WORKDAYS:
        return f"по будням в {at}"
    if days == WEEKEND:
        return f"по выходным в {at}"
    if len(days) == 1:
        return f"{WEEKDAYS_IN[days[0]]} в {at}"
    return f"по {', '.join(WEEKDAYS_SHORT[d] for d in days)} в {at}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- фразы: «каждый вечер записывай курс доллара» ----------------------------------------------------------

_TIME = re.compile(
    r"^(?:в|к)\s+(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?(?:\s*(?:час(?:а|ов)?|ч\.?))?"
    r"(?:\s+(?P<part>утра|дня|вечера|ночи))?(?=\W|$)",
    re.I,
)
_PART = re.compile(r"^(?P<part>утром|днём|днем|в\s+обед|вечером|ночью)(?=\W|$)", re.I)
_EVERY_N_HOURS = re.compile(r"^(?:каждые|раз\s+в)\s+(?P<n>\d{1,3})\s*(?:час(?:а|ов)?|ч\.?)(?=\W|$)", re.I)
_EVERY_HOUR = re.compile(r"^(?:каждый\s+час|ежечасно|раз\s+в\s+час)(?=\W|$)", re.I)
_DAILY = re.compile(
    r"^(?:каждый\s+день|ежедневно|раз\s+в\s+день|каждые\s+сутки|"
    r"кажд(?:ое|ый|ую)\s+(?P<part>утро|вечер|ночь|день))(?=\W|$)",
    re.I,
)
_WORKDAYS = re.compile(r"^(?:по\s+будн(?:ям|им\s+дням)|в\s+будни)(?=\W|$)", re.I)
_WEEKEND = re.compile(r"^(?:по\s+выходным|в\s+выходные)(?=\W|$)", re.I)
_WEEKLY = re.compile(r"^(?:каждую\s+неделю|еженедельно|раз\s+в\s+неделю)(?=\W|$)", re.I)
_ON_DAY = re.compile(
    r"^(?:кажд(?:ый|ую|ое)\s+(?P<one>понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)|"
    r"по\s+(?P<many>понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям))(?=\W|$)",
    re.I,
)
_LEAD = re.compile(r"^[\s,:;—–-]+")


def _part_time(word: str) -> str:
    low = word.casefold().replace("ё", "е")
    return next((at for key, at in PARTS_OF_DAY.items() if key in low), DEFAULT_AT)


def _weekday(word: str) -> int:
    low = word.casefold()
    return next(n for key, n in WEEKDAY_WORDS if low.startswith(key))


def _clock(rest: str) -> tuple[str | None, str]:
    """«в 21:00 …», «в 9 вечера …», «вечером …» в начале rest → (ЧЧ:ММ, остаток); нет времени — (None,
    rest)."""
    rest = _LEAD.sub("", rest)
    m = _TIME.match(rest)
    if m:
        hour, minute = int(m["h"]), int(m["m"] or 0)
        part = (m["part"] or "").casefold()
        if part in ("вечера", "дня") and hour < 12:
            hour += 12
        elif part == "ночи" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None, rest
        return fmt_hhmm(hour, minute), rest[m.end() :]
    m = _PART.match(rest)
    if m:
        return _part_time(m["part"]), rest[m.end() :]
    return None, rest


def parse_repeat(text: str, now: float | None = None) -> tuple[dict[str, Any], str]:
    """Повтор в начале текста задачи: «каждый вечер записывай курс доллара» → ({daily 20:00}, «Записывай
    курс доллара»). Нет повтора — ({}, text). Только в начале: «узнай, что меняется каждый день» — не
    повтор."""
    source = (text or "").strip()
    rest = source
    repeat: dict[str, Any] = {}
    if m := _EVERY_N_HOURS.match(rest):
        hours = int(m["n"])
        if 1 <= hours <= MAX_HOURS:
            repeat, rest = {"kind": "hours", "hours": hours}, rest[m.end() :]
    elif m := _EVERY_HOUR.match(rest):
        repeat, rest = {"kind": "hours", "hours": 1}, rest[m.end() :]
    elif m := _DAILY.match(rest):
        part = m["part"] or ""
        at = _part_time(part) if part else ""
        repeat, rest = {"kind": "daily", "at": at, "weekdays": []}, rest[m.end() :]
    elif m := _WORKDAYS.match(rest):
        repeat, rest = {"kind": "daily", "at": "", "weekdays": list(WORKDAYS)}, rest[m.end() :]
    elif m := _WEEKEND.match(rest):
        repeat, rest = {"kind": "daily", "at": "", "weekdays": list(WEEKEND)}, rest[m.end() :]
    elif m := _ON_DAY.match(rest):
        day = _weekday(m["one"] or m["many"])
        repeat, rest = {"kind": "daily", "at": "", "weekdays": [day]}, rest[m.end() :]
    elif m := _WEEKLY.match(rest):
        today = datetime.fromtimestamp(now if now is not None else datetime.now().timestamp()).weekday()
        repeat, rest = {"kind": "daily", "at": "", "weekdays": [today]}, rest[m.end() :]
    if not repeat:
        return {}, source
    if repeat["kind"] == "daily":
        at, rest = _clock(rest)
        hm = parse_hhmm(at) if at else None
        if hm is not None and hm[0] < 12 and repeat["at"] in ("13:00", "20:00"):  # «каждый вечер в 9» — 21:00
            at = fmt_hhmm(hm[0] + 12, hm[1])
        repeat["at"] = at or repeat["at"] or DEFAULT_AT
    rest = _LEAD.sub("", rest).strip()
    if len(rest) < 3:  # «каждый день» и всё — это не задача
        return {}, source
    return clean_repeat(repeat), rest[:1].upper() + rest[1:]
