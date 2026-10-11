"""Напоминания: <data>\\reminders.json, без Qt. Срабатывание — дело приложения (app.py).

API для app.py (изменяющие функции бросают только StoreError; читающие не бросают):
- pop_due(now) — сработавшие напоминания по порядку (удаляются из файла); при старте отдаёт и пропущенные,
  пока Jarvis не работал (due_ts в прошлом — показать время);
- next_due() — время ближайшего (как time.time()); None — напоминаний нет;
- version() — mtime файла в нс: изменился — пере-взвести таймер (напоминание мог добавить `jarvis ask` из
  другого процесса).
Разбор времени — чистая функция parse_when(text, now); для таймера — parse_duration(text); текст «в 18:30»,
«завтра в 8:15» для ответа — format_due(due_ts, now).
"""

import logging
import math
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from jarvis.memory import ALL_WORDS, LAST_WORDS, JsonList, StoreError, clean_text, matches, new_id

log = logging.getLogger("jarvis")

FILE_NAME = "reminders.json"
LIMIT = 50
TEXT_MAX = 200
MAX_AHEAD_S = 366 * 86400
DEFAULT_TEXT = "Напоминание"
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


@dataclass(frozen=True)
class Reminder:
    id: str
    text: str
    due_ts: float
    created_ts: float


def _num_field(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _parse(raw: dict[str, Any]) -> Reminder | None:
    ident, text = raw.get("id"), raw.get("text")
    due, created = _num_field(raw, "due_ts"), _num_field(raw, "created_ts")
    if not isinstance(ident, str) or not ident or not isinstance(text, str) or due is None:
        return None
    clean = " ".join(text.split())[:TEXT_MAX] or DEFAULT_TEXT
    return Reminder(ident, clean, due, created if created is not None else due)


_store: JsonList[Reminder] = JsonList(FILE_NAME, _parse, asdict)
_fired: set[str] = set()  # сработавшие, которые не удалось стереть из файла: второй раз не показывать


def version() -> int:
    """mtime reminders.json в нс (0 — файла нет): сравнивай с прошлым значением, чтобы пере-взвести таймер."""
    return _store.version()


def pending() -> list[Reminder]:
    """Несработавшие напоминания по времени (номер в списке = индекс + 1)."""
    return sorted((r for r in _store.load() if r.id not in _fired), key=lambda r: (r.due_ts, r.created_ts))


def next_due() -> float | None:
    """Когда ближайшее напоминание (time.time()); None — напоминаний нет."""
    items = pending()
    return items[0].due_ts if items else None


def pop_due(now: float | None = None) -> list[Reminder]:
    """Сработавшие к now (по умолчанию — сейчас) напоминания; из файла они удаляются."""
    t = time.time() if now is None else now
    with _store.lock:
        current = _store.load()
        due = [r for r in current if r.due_ts <= t and r.id not in _fired]
        if not due:
            return []
        ids = {r.id for r in due}
        try:
            _store.save([r for r in current if r.id not in ids])
        except StoreError:
            _fired.update(ids)  # файл не переписался — в этом процессе второй раз не покажем
    return sorted(due, key=lambda r: r.due_ts)


def add(text: str, due_ts: float, now: float | None = None) -> Reminder:
    """Добавить напоминание. StoreError: время не то, текст длинный, лимит LIMIT, ошибка записи."""
    t = time.time() if now is None else now
    if isinstance(due_ts, bool) or not isinstance(due_ts, int | float) or not math.isfinite(due_ts):
        raise StoreError("Не понял, когда напомнить")
    if due_ts - t > MAX_AHEAD_S:
        raise StoreError("Так далеко не напоминаю: не больше чем на год вперёд")
    clean = clean_text(text, TEXT_MAX) or DEFAULT_TEXT
    with _store.lock:
        current = _store.load()
        if len(current) >= LIMIT:
            raise StoreError(f"Слишком много напоминаний ({LIMIT}) — отмени ненужные")
        item = Reminder(new_id(r.id for r in current), clean, float(due_ts), t)
        _store.save([*current, item])
    return item


def cancel(selector: int | str | None) -> tuple[list[Reminder], list[Reminder]]:
    """Отменить напоминание → (отменённые, подходящие).

    selector — номер в списке pending() (с 1), «последнее» (созданное последним), «все», часть текста или None
    (одно напоминание — его). Подошло несколько (часть текста, None) — ничего не отменено, они во втором
    списке.
    """
    with _store.lock:
        current = pending()
        chosen, many = _select(selector, current)
        if many:
            return [], chosen
        if chosen:
            ids = {r.id for r in chosen}
            _store.save([r for r in _store.load() if r.id not in ids])
    return chosen, []


def _select(selector: int | str | None, current: list[Reminder]) -> tuple[list[Reminder], bool]:
    """(выбранные, неоднозначно ли)."""
    if selector is None or selector == "":
        return list(current), len(current) > 1
    if isinstance(selector, str) and selector.strip().isdigit():
        selector = int(selector.strip())
    if isinstance(selector, bool):
        return [], False
    if isinstance(selector, int):
        return ([current[selector - 1]] if 1 <= selector <= len(current) else []), False
    word = selector.casefold().replace("ё", "е").strip()
    if word in ALL_WORDS:
        return list(current), False
    if word in LAST_WORDS:
        return ([max(current, key=lambda r: r.created_ts)] if current else []), False
    found = [r for r in current if matches(word, r.text)]
    return found, len(found) > 1


def cancel_ids(ids: Iterable[str]) -> list[Reminder]:
    """Отменить по id (отмена «напомни»). → отменённые."""
    wanted = set(ids)
    with _store.lock:
        current = _store.load()
        gone = [r for r in current if r.id in wanted]
        if gone:
            _store.save([r for r in current if r.id not in wanted])
    return gone


def restore(records: Iterable[dict[str, Any]]) -> list[Reminder]:
    """Вернуть отменённые напоминания (отмена «отмени напоминание»); существующие id пропускаются."""
    with _store.lock:
        current = _store.load()
        have = {r.id for r in current}
        back = [
            r for raw in records if isinstance(raw, dict) for r in [_parse(raw)] if r and r.id not in have
        ]
        if not back:
            return []
        if len(current) + len(back) > LIMIT:
            raise StoreError(f"Слишком много напоминаний ({LIMIT})")
        _store.save([*current, *back])
    _fired.difference_update(r.id for r in back)
    return back


# --- время ---------------------------------------------------------------------------------------------

_UNITS = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9,
}  # fmt: skip
_TEENS = {
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}  # fmt: skip
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
_WORD_NUM = {**_UNITS, **_TEENS, **_TENS, "пару": 2, "шестьдесят": 60, "девяносто": 90, "сто": 100}


def _alt(words: Iterable[str]) -> str:
    return "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))


_COMPOUND = rf"(?:{_alt(_TENS)})(?:\s+(?:{_alt(_UNITS)}))?"
_NUM = rf"(?:\d{{1,4}}(?:[.,]\d{{1,2}})?|{_COMPOUND}|{_alt(set(_WORD_NUM) - set(_TENS))})"
_MINW = rf"(?:{_COMPOUND}|{_alt(_TEENS)})"  # минуты словами: «восемь пятнадцать», «девять сорок пять»
_HOURW = _alt({**_UNITS, "десять": 10, "одиннадцать": 11, "двенадцать": 12})
_UNIT = r"(?:секунд[уы]?|сек|минут[уы]?|мин|час(?:а|ов)?|дн(?:я|ей)|день|сутки|суток|недел[юиь])"
_END = r"(?![а-яa-z0-9])"
_DURATION = (
    r"(?:(?P<half>пол(?:часа|минуты|дня))"
    r"|(?P<one_half>полтора\s+(?:часа|дня)|полторы\s+минуты)"
    rf"|(?:(?P<n>{_NUM})(?P<nhalf>\s+с\s+половиной)?\s+)?(?P<u>{_UNIT}){_END}(?P<uhalf>\s+с\s+половиной)?"
    rf"(?:\s+(?:и\s+)?(?P<n2>{_NUM})\s+(?P<u2>минут[уы]?|мин))?){_END}"
)
_DAY = r"(?:сегодня|завтра|послезавтра)"
_PART_MER = {"утром": "утра", "днем": "дня", "вечером": "вечера", "ночью": "ночи"}
# «на 3 часа» в конце фразы — чаще длительность («поставить машину на 3 часа»), поэтому «на» — только с HH:MM
_ABS = (
    rf"(?:(?P<day>{_DAY})\s+)?(?:(?P<part>утром|днем|вечером|ночью)\s+)?"
    rf"(?:(?:(?:в|во|к|на)\s+)?(?P<h>\d{{1,2}})[:.\-](?P<m>\d{{2}})"
    rf"|(?:в|во|к)\s+(?P<h2>\d{{1,2}}|{_HOURW}){_END}(?:\s+(?P<m2>\d{{2}}|{_MINW}){_END})?"
    rf"(?:\s+час(?:а|ов)?)?(?:\s+(?P<m3>\d{{1,2}}|{_MINW})\s+минут[уы]?)?"
    r"|(?:в\s+)?(?P<noon>полдень|полночь)"
    r"|в\s+(?P<one>час)(?=\s+(?:дня|ночи)))"
    rf"(?:\s+(?P<mer>утра|дня|вечера|ночи))?(?:\s+(?P<day2>{_DAY}))?{_END}"
)
_WHEN = rf"(?:через\s+{_DURATION}|{_ABS})"
_AT_START = re.compile(rf"^\s*(?P<when>{_WHEN})(?=[\s,.;:!?—-]|$)")
_AT_END = re.compile(rf"(?:^|[\s,;:—-])(?P<when>{_WHEN})[\s,.;:!?…]*$")
_DUR_START = re.compile(rf"^\s*(?:на\s+)?(?P<when>{_DURATION})(?=[\s,.;:!?—-]|$)")
_UNIT_S = (("сек", 1), ("мин", 60), ("час", 3600), ("дн", 86400), ("день", 86400), ("сут", 86400))
_SINGULAR = frozenset({"секунду", "минуту", "час", "день", "сутки", "неделю"})
_LEAD_JUNK = re.compile(r"^(?:(?:о\s+том,?\s+)?(?:что|чтобы)|мне|нам)\s+", re.IGNORECASE)


def _num(s: str) -> float:
    s = s.strip()
    if s[:1].isdigit():
        return float(s.replace(",", "."))
    return float(sum(_WORD_NUM[w] for w in s.split()))


def _unit_seconds(unit: str) -> int:
    if unit.startswith("недел"):
        return 7 * 86400
    return next(sec for prefix, sec in _UNIT_S if unit.startswith(prefix))


def _duration_s(m: re.Match[str]) -> float | None:
    if m["half"]:
        return {"полчаса": 1800.0, "полминуты": 30.0, "полдня": 43200.0}[m["half"]]
    if m["one_half"]:
        return 90.0 if m["one_half"].startswith("полторы") else 1.5 * _unit_seconds(m["one_half"].split()[-1])
    if m["n"] is None and m["u"] not in _SINGULAR:
        return None  # «через минут» — не время
    amount = _num(m["n"]) if m["n"] else 1.0
    if m["nhalf"] or m["uhalf"]:
        amount += 0.5
    seconds = amount * _unit_seconds(m["u"])
    if m["n2"]:
        seconds += _num(m["n2"]) * 60
    return seconds if seconds > 0 else None


def _meridiem(h: int, mer: str | None) -> int | None:
    """«в 7 вечера» → 19; «в 12 ночи» → 0; несовместимое («в 15 утра») — None."""
    if mer is None:
        return h
    if mer == "утра":
        return h if 1 <= h <= 11 else None
    if mer == "дня":
        return h + 12 if 1 <= h <= 5 else (h if 12 <= h <= 17 else None)
    if mer == "вечера":
        return h + 12 if 4 <= h <= 11 else (h if 16 <= h <= 23 else (0 if h == 12 else None))
    return 0 if h == 12 else (h if 1 <= h <= 5 else (h + 12 if 9 <= h <= 11 else None))  # ночи


def _clock_due(m: re.Match[str], now: datetime) -> datetime | None:
    if m["noon"]:
        h, minute = (12, 0) if m["noon"] == "полдень" else (0, 0)
    elif m["one"]:
        h, minute = 1, 0
    elif m["h"] is not None:
        h, minute = int(m["h"]), int(m["m"])
    else:
        h, minute = int(_num(m["h2"])), int(_num(m["m2"] or m["m3"] or "0"))
    mer = m["mer"] or _PART_MER.get(m["part"] or "")
    hour = _meridiem(h, mer)
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    day = m["day"] or m["day2"]
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "завтра":
        return at + timedelta(days=1)
    if day == "послезавтра":
        return at + timedelta(days=2)
    if day == "сегодня":
        if at <= now and hour < 12 and mer is None:
            at = at.replace(hour=hour + 12)  # «сегодня в 9», сказанное днём, — это 21:00
        return at if at > now else None
    return at if at > now else at + timedelta(days=1)  # прошедшее время сегодня — завтра


def _due(m: re.Match[str], now: float) -> float | None:
    if m["u"] or m["half"] or m["one_half"]:
        seconds = _duration_s(m)
        due = now + seconds if seconds is not None else None
    else:
        at = _clock_due(m, datetime.fromtimestamp(now))
        due = at.timestamp() if at is not None else None
    if due is None or not 0 < due - now <= MAX_AHEAD_S:
        return None
    return due


def _tidy(text: str) -> str:
    rest = " ".join(text.split()).strip(" ,.;:!?—-")
    while (cut := _LEAD_JUNK.sub("", rest, count=1)) != rest:
        rest = cut
    return rest


def parse_when(text: str, now: float | None = None) -> tuple[float, str] | None:
    """Время в начале или в конце текста → (due_ts, остаток текста); времени нет или оно не подходит — None.

    «через N секунд/минут/часов/дней/недель» (числом или словами, «через минуту», «через пару часов»,
    «через 1 час 30 минут», «через 2 с половиной часа»), «через полчаса», «через полтора часа»;
    «(сегодня|завтра|послезавтра) в HH:MM | HH.MM | HH MM | HH (часов) (утра|дня|вечера|ночи)», «в полдень».
    Прошедшее время без дня — завтра; «сегодня в 9», сказанное днём, — 21:00. Не дальше MAX_AHEAD_S.
    """
    t = time.time() if now is None else now
    low = text.lower().replace("ё", "е")
    src = text if len(low) == len(text) else low
    for rx in (_AT_START, _AT_END):
        m = rx.search(low)
        if m is None:
            continue
        due = _due(m, t)
        if due is not None:
            return due, _tidy(f"{src[: m.start('when')]} {src[m.end('when') :]}")
    return None


def parse_duration(text: str) -> tuple[float, str, str] | None:
    """Длительность в начале текста («10 минут», «на полчаса») → (секунды, как сказано, остаток) или None."""
    low = text.lower().replace("ё", "е")
    m = _DUR_START.match(low)
    if m is None:
        return None
    seconds = _duration_s(m)
    if seconds is None or seconds > MAX_AHEAD_S:
        return None
    src = text if len(low) == len(text) else low
    said = " ".join(src[m.start("when") : m.end("when")].split())
    return seconds, said, _tidy(src[m.end("when") :])


def format_due(due_ts: float, now: float | None = None) -> str:
    """«через 30 секунд», «в 18:30», «завтра в 8:15», «15 октября в 10:00» — для ответа и списка."""
    t = time.time() if now is None else now
    left = due_ts - t
    if 0 < left < 60:
        n = max(1, round(left))
        return f"через {n} {plural(n, ('секунду', 'секунды', 'секунд'))}"
    due, today = datetime.fromtimestamp(due_ts), datetime.fromtimestamp(t)
    hm = f"{due.hour}:{due.minute:02d}"
    days = (due.date() - today.date()).days
    if days == 0:
        return f"в {hm}"
    if days == 1:
        return f"завтра в {hm}"
    if days == 2:
        return f"послезавтра в {hm}"
    if days == -1:
        return f"вчера в {hm}"
    year = f" {due.year}" if due.year != today.year else ""
    return f"{due.day} {MONTHS[due.month - 1]}{year} в {hm}"


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Форма слова для числа: plural(3, ("запись", "записи", "записей")) → «записи»."""
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]
