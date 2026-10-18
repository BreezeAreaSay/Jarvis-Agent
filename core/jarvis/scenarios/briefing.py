"""«Сводка дня» — действие Jarvis.Briefing: приветствие, погода, встречи, задачи, курсы, новости. Без ИИ.

Режимы (значение действия):
- "" — утренняя сводка целиком: приветствие по времени суток и дата, погода в городе из профиля, встречи и
  задачи на сегодня (с просроченными), курсы доллара и евро, 2–3 главные новости;
- "short" — коротко: приветствие, погода, встречи и задачи;
- "tasks" — незакрытые задачи (просроченные и на сегодня — первыми);
- "missed" — «С возвращением»: напоминания, сработавшие, пока человека не было (их помнит сервис сценариев).

build() возвращает (текст для тоста, текст для голоса). Данные — ctx.widgets.data(...) и ctx.notes; сбой сети
или пустые данные — эта часть просто пропускается.
"""

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any

from jarvis.ai.format import MONTHS_GEN, WEEKDAYS
from jarvis.commands import numbers
from jarvis.commands.executor import DAY_ORDINALS
from jarvis.scenarios.schedule import day_bounds, local

log = logging.getLogger(__name__)

MODES = ("", "short", "tasks", "missed")
FETCH_TIMEOUT_S = 8.0
MAX_EVENTS = 5
MAX_TASKS = 5
MAX_MISSED = 5
NEWS_COUNT = 3
DEFAULT_SINCE_S = 3600.0  # «что пропустили» без известного ухода — за последний час

MEETINGS = ("встреча", "встречи", "встреч")
OPEN_TASKS = ("незакрытая задача", "незакрытые задачи", "незакрытых задач")
REMINDERS = ("напоминание", "напоминания", "напоминаний")
RUBLES = ("рубль", "рубля", "рублей")
KOPECKS = ("копейка", "копейки", "копеек")
CURRENCIES = {"USD": "доллар", "EUR": "евро"}

Part = tuple[str, str]  # (для тоста, для голоса)


def greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def _hello(words: str, address: str) -> str:
    return f"{words}, {address}!" if address else f"{words}!"


def _clock(ts: float) -> str:
    moment = local(ts)
    return f"{moment.hour}:{moment.minute:02d}"


def _clock_spoken(ts: float) -> str:
    """«9 часов 30 минут», «18 часов» — так синтез читает время без запинок."""
    moment = local(ts)
    out = numbers.count_text(moment.hour, numbers.HOURS)
    if moment.minute:
        out += " " + numbers.count_text(moment.minute, numbers.MINUTES)
    return out


def _signed(value: float) -> str:
    v = round(value)
    return f"+{v}" if v > 0 else str(v)


def _degrees(value: float) -> str:
    v = round(value)
    sign = "плюс " if v > 0 else "минус " if v < 0 else ""
    return f"{sign}{abs(v)} {numbers.plural(abs(v), numbers.DEGREES)}"


def _number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                return float(raw.replace(",", ".").replace("°", "").strip())
    return None


def _rubles(value: float) -> str:
    rub = int(value)
    kop = round((value - rub) * 100)
    if kop >= 100:
        rub, kop = rub + 1, 0
    out = numbers.count_text(rub, RUBLES)
    return out + (" " + numbers.count_text(kop, KOPECKS) if kop else "")


def _now(ctx: Any) -> float:
    clock = getattr(getattr(ctx, "scenarios", None), "clock", None)
    return float(clock()) if callable(clock) else time.time()


def _address(ctx: Any) -> str:
    return str(ctx.settings.value.profile.address or "").strip()


async def _fetch(ctx: Any, kind: str, params: dict[str, Any]) -> dict[str, Any] | None:
    widgets = getattr(ctx, "widgets", None)
    if widgets is None:
        return None
    try:
        data = await asyncio.wait_for(widgets.data(kind, params), FETCH_TIMEOUT_S)
    except Exception as e:
        log.info("сводка: данные «%s» не получены: %s", kind, e)
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    return data


def _notes(ctx: Any, method: str) -> Any:
    return getattr(getattr(ctx, "notes", None), method, None)


# --- части сводки --------------------------------------------------------------------------------------


def _date(moment: datetime) -> Part:
    weekday, month = WEEKDAYS[moment.weekday()], MONTHS_GEN[moment.month - 1]
    return (
        f"Сегодня {weekday}, {moment.day} {month}.",
        f"Сегодня {weekday}, {DAY_ORDINALS[moment.day - 1]} {month}.",
    )


async def _weather(ctx: Any, city: str, hour: int) -> Part | None:
    data = await _fetch(ctx, "weather", {"city": city} if city else {})
    if data is None:
        return None
    current = data["current"] if isinstance(data.get("current"), dict) else data
    temp = _number(current, "temp", "temperature", "temp_c")
    if temp is None:
        return None
    condition = str(current.get("condition") or current.get("description") or "").strip()
    place = str(data.get("city") or city or "").strip()
    high = None
    daily = data.get("daily")
    if hour < 15 and isinstance(daily, list) and daily and isinstance(daily[0], dict):
        high = _number(daily[0], "max", "temp_max")
        if high is not None and round(high) <= round(temp):
            high = None
    shown = (
        (f"{place}: " if place else "Погода: ")
        + f"{_signed(temp)}°"
        + (f", {condition}" if condition else "")
    )
    spoken = f"За окном {_degrees(temp)}" + (f", {condition}" if condition else "")
    if high is not None:
        shown += f", днём до {_signed(high)}°"
        spoken += f", днём до {_degrees(high)}"
    return shown + ".", spoken + "."


def _events(ctx: Any, now: float) -> Part:
    events_list = _notes(ctx, "events_list")
    start, end = day_bounds(now)
    items: list[dict[str, Any]] = []
    if callable(events_list):
        try:
            items = [
                e
                for e in events_list(start, end)
                if e.get("all_day") or float(e.get("end") or e.get("start") or 0) >= now
            ]
        except Exception:
            log.exception("сводка: события не прочитаны")
    if not items:
        return "Встреч на сегодня нет.", "Встреч на сегодня нет."
    shown, spoken = [], []
    for event in items[:MAX_EVENTS]:
        title = str(event.get("title") or "").strip()
        if event.get("all_day"):
            shown.append(f"{title} (весь день)")
            spoken.append(f"весь день — {title}")
        else:
            shown.append(f"{_clock(float(event['start']))} {title}")
            spoken.append(f"в {_clock_spoken(float(event['start']))} — {title}")
    extra = len(items) - MAX_EVENTS
    tail = f" и ещё {extra}" if extra > 0 else ""
    return (
        "В календаре: " + ", ".join(shown) + tail + ".",
        f"В календаре {numbers.count_text(len(items), MEETINGS)}: " + ", ".join(spoken) + tail + ".",
    )


def _open_tasks(ctx: Any) -> list[dict[str, Any]]:
    tasks_list = _notes(ctx, "tasks_list")
    if not callable(tasks_list):
        return []
    try:
        return list(tasks_list(include_done=False))
    except Exception:
        log.exception("сводка: задачи не прочитаны")
        return []


def _task_names(tasks: list[dict[str, Any]], day_start: float) -> tuple[list[str], list[str]]:
    shown, spoken = [], []
    for task in tasks[:MAX_TASKS]:
        title = str(task.get("title") or "").strip()
        overdue = task.get("due") is not None and float(task["due"]) < day_start
        shown.append(title + (" (просрочено)" if overdue else ""))
        spoken.append(title + (" — просрочено" if overdue else ""))
    return shown, spoken


def _tasks_today(ctx: Any, now: float) -> Part:
    start, end = day_bounds(now)
    due = sorted(
        (t for t in _open_tasks(ctx) if t.get("due") is not None and float(t["due"]) < end),
        key=lambda t: float(t["due"]),
    )
    if not due:
        return "Срочных задач нет.", "Срочных задач нет."
    shown, spoken = _task_names(due, start)
    extra = len(due) - MAX_TASKS
    tail = f" и ещё {extra}" if extra > 0 else ""
    return "Задачи на сегодня: " + ", ".join(shown) + tail + ".", "Задачи: " + ", ".join(spoken) + tail + "."


async def _rates(ctx: Any) -> Part | None:
    data = await _fetch(ctx, "rates", {"codes": "USD,EUR"})
    if data is None:
        return None
    shown, spoken = [], []
    for item in data.get("items") or []:
        code = str(item.get("code") or "").upper() if isinstance(item, dict) else ""
        value = _number(item, "value") if code in CURRENCIES else None
        if value is None or value <= 0:
            continue
        symbol = str(item.get("symbol") or code)
        shown.append(f"{symbol} {value:.2f} ₽".replace(".", ","))
        spoken.append(f"{CURRENCIES[code]} — {_rubles(value)}")
    if not shown:
        return None
    return "Курсы: " + ", ".join(shown) + ".", "Курс: " + ", ".join(spoken) + "."


async def _news(ctx: Any) -> Part | None:
    data = await _fetch(ctx, "news", {"limit": NEWS_COUNT})
    if data is None:
        return None
    titles = [
        " ".join(str(i.get("title")).split()).rstrip(".")
        for i in (data.get("items") or [])
        if isinstance(i, dict) and i.get("title")
    ][:NEWS_COUNT]
    if not titles:
        return None
    return "Новости: " + "; ".join(titles) + ".", "Главные новости. " + ". ".join(titles) + "."


# --- режимы ---------------------------------------------------------------------------------------------


def open_tasks_text(ctx: Any, now: float) -> Part:
    """Режим tasks: все незакрытые задачи — просроченные и на сегодня, потом со сроком, потом без срока."""
    tasks = _open_tasks(ctx)
    if not tasks:
        text = "Все задачи закрыты — можно отдыхать."
        return text, text
    start, end = day_bounds(now)

    def rank(task: dict[str, Any]) -> tuple[int, float]:
        due = task.get("due")
        if due is None:
            return 2, float(task.get("position") or 0)
        return (0 if float(due) < end else 1), float(due)

    ordered = sorted(tasks, key=rank)
    shown, spoken = _task_names(ordered, start)
    extra = len(ordered) - MAX_TASKS
    tail = f" и ещё {extra}" if extra > 0 else ""
    count = numbers.count_text(len(ordered), OPEN_TASKS)
    shown_text = f"{count[:1].upper()}{count[1:]}: " + ", ".join(shown) + tail + "."
    return shown_text, f"У вас {count}: " + ", ".join(spoken) + tail + "."


def missed_text(ctx: Any, now: float, slots: dict[str, Any], address: str) -> Part:
    """Режим missed: «С возвращением» и напоминания, сработавшие за время отсутствия."""
    service = getattr(ctx, "scenarios", None)
    since = slots.get("_away_since")
    if since is None and service is not None:
        since = service.away_since()
    if since is None:
        since = now - DEFAULT_SINCE_S
    items: list[dict[str, Any]] = service.missed_since(float(since)) if service is not None else []
    hello = _hello("С возвращением", address)
    shown, spoken = [hello], [hello]
    away = slots.get("number")
    if isinstance(away, int | float) and away >= 1:
        text = f"Вас не было {numbers.duration_text(float(away) * 60)}."
        shown.append(text)
        spoken.append(text)
    if not items:
        shown.append("Вы ничего не пропустили.")
        spoken.append("Вы ничего не пропустили.")
    else:
        recent = items[-MAX_MISSED:]
        shown.append(
            "Пока вас не было: " + "; ".join(f"{_clock(i['ts'])} — {i['text']}" for i in recent) + "."
        )
        spoken.append(
            f"Пока вас не было, {numbers.count_text(len(items), REMINDERS)}: "
            + "; ".join(f"в {_clock_spoken(i['ts'])} — {i['text']}" for i in recent)
            + "."
        )
    return " ".join(shown), " ".join(spoken)


async def build(
    ctx: Any, mode: str = "", slots: dict[str, Any] | None = None, *, now: float | None = None
) -> Part:
    """Сводка: (текст для тоста, текст для голоса)."""
    kind = (mode or "").strip().casefold()
    ts = _now(ctx) if now is None else now
    address = _address(ctx)
    if kind == "tasks":
        return open_tasks_text(ctx, ts)
    if kind == "missed":
        return missed_text(ctx, ts, slots or {}, address)
    moment = local(ts)
    full = kind != "short"
    city = str(ctx.settings.value.profile.city or "").strip()
    jobs = [_weather(ctx, city, moment.hour)]
    if full:
        jobs += [_rates(ctx), _news(ctx)]
    fetched = await asyncio.gather(*jobs)
    hello = _hello(greeting(moment.hour), address)
    parts: list[Part] = [(hello, hello), _date(moment)]
    if fetched[0] is not None:
        parts.append(fetched[0])
    parts += [_events(ctx, ts), _tasks_today(ctx, ts)]
    parts += [p for p in fetched[1:] if p is not None]
    return " ".join(p[0] for p in parts), " ".join(p[1] for p in parts)
