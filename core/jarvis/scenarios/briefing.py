"""«Сводка дня» — действие Jarvis.Briefing: приветствие, погода, встречи, задачи, курсы, новости. Без ИИ.

Режимы (значение действия):
- "" — утренняя сводка целиком: приветствие по времени суток и дата, погода в городе из профиля, встречи и
  задачи на сегодня (с просроченными), курсы доллара и евро, 2–3 главные новости;
- "short" — коротко: приветствие, погода, встречи и задачи;
- "tasks" — незакрытые задачи (просроченные и на сегодня — первыми);
- "today" — дела на сегодня («озвучь задачи на сегодня»): задачи со сроком сегодня и просроченные, встречи и
  напоминания — по времени («в 7 вечера — сходить в банк»);
- "missed" — «С возвращением»: напоминания, сработавшие, пока человека не было (их помнит сервис сценариев);
- "welcome" — «я вернулся»: приветствие в характере (jarvis.voice.character), напоминания, сработавшие без
  человека, ближайшие (3 часа) напоминания и встречи, сколько задач на сегодня; "hello" — то же с приветствием
  по времени суток («доброе утро») и погодой, если она уже под рукой (кэш, не дольше 0,4 с).

build() возвращает (текст для тоста, текст для голоса). Данные — ctx.widgets.data(...) и ctx.notes; сбой сети
или пустые данные — эта часть просто пропускается.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jarvis.ai.format import MONTHS_GEN, WEEKDAYS
from jarvis.commands import numbers
from jarvis.commands.executor import DAY_ORDINALS
from jarvis.scenarios.schedule import day_bounds, local

log = logging.getLogger(__name__)

MODES = ("", "short", "tasks", "today", "missed", "welcome", "hello")
FETCH_TIMEOUT_S = 8.0
MAX_EVENTS = 5
MAX_TASKS = 5
MAX_MISSED = 5
NEWS_COUNT = 3
DEFAULT_SINCE_S = 3600.0  # «что пропустили» без известного ухода — за последний час
UPCOMING_S = 3 * 3600.0  # «я вернулся»: ближайшие напоминания и встречи — на 3 часа вперёд
MAX_UPCOMING = 3
AWAY_IDLE_S = 120.0  # «я вернулся», а клавиатуру и мышь не трогали дольше — человек отходил с тех пор
AWAY_KEEP_S = 12 * 3600.0  # известный уход старше — не в счёт
QUICK_WEATHER_S = 0.4  # погода к приветствию — только если уже под рукой: ответ не ждёт сеть

MEETINGS = ("встреча", "встречи", "встреч")
OPEN_TASKS = ("незакрытая задача", "незакрытые задачи", "незакрытых задач")
REMINDERS = ("напоминание", "напоминания", "напоминаний")
TASKS = ("задача", "задачи", "задач")
DEEDS = ("дело", "дела", "дел")
TIMER_PREFIX = "таймер на"  # таймеры — тоже напоминания, но о них в сводке не говорим
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


def spoken_clock(ts: float) -> str:
    """Время для голоса по-человечески: «в 7 вечера», «в полдень», «в 9 часов 30 минут»."""
    moment = local(ts)
    if moment.minute:
        return "в " + _clock_spoken(ts)
    hour = moment.hour
    if hour == 0:
        return "в полночь"
    if hour == 12:
        return "в полдень"
    part = "утра" if 4 <= hour < 12 else "дня" if 12 < hour < 17 else "вечера" if hour >= 17 else "ночи"
    return f"в {hour % 12 or 12} {part}"


def _is_timer(text: str) -> bool:
    return text.strip().casefold().startswith(TIMER_PREFIX)


def _has_time(ts: float) -> bool:
    moment = local(ts)
    return bool(moment.hour or moment.minute)


def _upcoming(ctx: Any, now: float) -> list[tuple[float, str]]:
    """Напоминания (кроме таймеров) и встречи на ближайшие UPCOMING_S: [(когда, что)] по времени."""
    out: list[tuple[float, str]] = []
    reminders_list = _notes(ctx, "reminders_list")
    if callable(reminders_list):
        try:
            for item in reminders_list(include_done=False):
                at, text = float(item.get("at") or 0), str(item.get("text") or "").strip()
                if text and now < at <= now + UPCOMING_S and not _is_timer(text):
                    out.append((at, text))
        except Exception:
            log.exception("сводка: напоминания не прочитаны")
    events_list = _notes(ctx, "events_list")
    if callable(events_list):
        try:
            for event in events_list(now, now + UPCOMING_S):
                start, title = float(event.get("start") or 0), str(event.get("title") or "").strip()
                if title and not event.get("all_day") and now < start <= now + UPCOMING_S:
                    out.append((start, title))
        except Exception:
            log.exception("сводка: события не прочитаны")
    return sorted(out)


def _tasks_due_today(ctx: Any, now: float) -> list[dict[str, Any]]:
    _, end = day_bounds(now)
    due = [t for t in _open_tasks(ctx) if t.get("due") is not None and float(t["due"]) < end]
    return sorted(due, key=lambda t: float(t["due"]))


def _lower(text: str) -> str:
    return text[0].lower() + text[1:] if len(text) > 1 and not text[1].isupper() else text


@dataclass
class Welcome:
    """Приветствие «я вернулся» / «доброе утро»: тексты и есть ли новости (напоминания — пропущенные или
    скорые); без новостей приветствие по возвращении (настройка «только если есть что сказать») молчит."""

    shown: str
    spoken: str
    news: bool


async def _quick_weather(ctx: Any, city: str, hour: int) -> Part | None:
    """Погода к приветствию, только если она под рукой: запрос не дольше QUICK_WEATHER_S, а не успел —
    данные дойдут в кэш виджетов к следующему разу."""
    task = asyncio.ensure_future(_weather(ctx, city, hour))
    try:
        return await asyncio.wait_for(asyncio.shield(task), QUICK_WEATHER_S)
    except TimeoutError:
        return None


async def away_since(ctx: Any, now: float) -> float:
    """Когда человек отошёл: давно не трогал клавиатуру и мышь — с тех пор; иначе последний известный уход
    (приветствие по возвращении, сценарии); не знаем — час назад."""
    with contextlib.suppress(Exception):
        idle = float(await ctx.run_platform(ctx.platform.idle_seconds))
        if idle >= AWAY_IDLE_S:
            return now - idle
    for name in ("presence", "scenarios"):
        get = getattr(getattr(ctx, name, None), "away_since", None)
        value = get() if callable(get) else None
        if isinstance(value, int | float) and now - AWAY_KEEP_S <= value <= now:
            return float(value)
    return now - DEFAULT_SINCE_S


async def welcome(
    ctx: Any,
    kind: str = "back",
    *,
    now: float | None = None,
    since: float | None = None,
    weather: bool | None = None,
) -> Welcome:
    """«С возвращением, сэр.» + пропущенные и ближайшие напоминания + задачи на сегодня (+ погода у hello)."""
    ts = _now(ctx) if now is None else now
    moment = local(ts)
    address = _address(ctx)
    character = getattr(ctx, "character", None)
    if character is not None:
        greet = character.greeting(kind, moment.hour)
    else:
        greet = _hello("С возвращением" if kind == "back" else greeting(moment.hour), address)
    shown, spoken = [greet], [greet]
    if since is None:
        since = await away_since(ctx, ts)
    service = getattr(ctx, "scenarios", None)
    missed_since = getattr(service, "missed_since", None)
    missed = missed_since(float(since)) if callable(missed_since) else []
    missed = [m for m in missed if not _is_timer(str(m.get("text") or ""))][-MAX_MISSED:]
    if missed:
        texts = [str(m.get("text") or "").rstrip(".") for m in missed]
        if len(texts) == 1:
            shown.append(f"Пока вас не было: {texts[0]}.")
            spoken.append(f"Пока вас не было, сработало напоминание: {_lower(texts[0])}.")
        else:
            shown.append("Пока вас не было: " + "; ".join(texts) + ".")
            spoken.append(
                f"Пока вас не было, сработали {numbers.count_text(len(texts), REMINDERS)}: "
                + "; ".join(_lower(t) for t in texts)
                + "."
            )
    upcoming = _upcoming(ctx, ts)[:MAX_UPCOMING]
    if upcoming:
        lead = "Также напоминаю" if missed else "Напоминаю"
        shown.append(f"{lead}: " + "; ".join(f"{_clock(at)} — {text}" for at, text in upcoming) + ".")
        spoken.append(
            f"{lead}: " + "; ".join(f"{spoken_clock(at)} — {_lower(text)}" for at, text in upcoming) + "."
        )
    tasks = _tasks_due_today(ctx, ts)
    if tasks:
        names = [str(t.get("title") or "").strip() for t in tasks[:2]]
        count = numbers.count_text(len(tasks), TASKS)
        shown.append(
            f"На сегодня {count}: " + ", ".join(names) + (" и другие" if len(tasks) > 2 else "") + "."
        )
        spoken.append(
            f"На сегодня {count}: "
            + ", ".join(_lower(n) for n in names)
            + (" и другие" if len(tasks) > 2 else "")
            + "."
        )
    if weather if weather is not None else kind != "back":
        city = str(ctx.settings.value.profile.city or "").strip()
        part = await _quick_weather(ctx, city, moment.hour)
        if part is not None:
            shown.append(part[0])
            spoken.append(part[1])
    news = bool(missed or upcoming)
    if not (missed or upcoming or tasks):
        clear = character.all_clear() if character is not None else "Новых напоминаний нет."
        shown.append(clear)
        spoken.append(clear)
    return Welcome(" ".join(shown), " ".join(spoken), news)


def today_text(ctx: Any, now: float) -> Part:
    """Режим today: «У вас 4 дела на сегодня: в 7 вечера — сходить в банк, …» — задачи со сроком сегодня (и
    просроченные), встречи и напоминания по времени; ничего не назначено — задачи без срока."""
    start, end = day_bounds(now)
    address = _address(ctx)
    items: list[tuple[int, float, str, str, bool]] = []  # (порядок, когда, для тоста, для голоса, задача ли)
    for task in _tasks_due_today(ctx, now):
        due, title = float(task["due"]), str(task.get("title") or "").strip()
        if due < start:
            items.append((0, due, f"{title} (просрочено)", f"{_lower(title)} — просрочено", True))
        elif _has_time(due):
            items.append((1, due, f"{_clock(due)} {title}", f"{spoken_clock(due)} — {_lower(title)}", True))
        else:
            items.append((2, due, title, _lower(title), True))
    events_list = _notes(ctx, "events_list")
    if callable(events_list):
        with contextlib.suppress(Exception):
            for event in events_list(start, end):
                title = str(event.get("title") or "").strip()
                if event.get("all_day"):
                    items.append((2, start, f"{title} (весь день)", f"весь день — {_lower(title)}", False))
                elif float(event.get("end") or event.get("start") or 0) >= now:
                    at = float(event["start"])
                    items.append(
                        (1, at, f"{_clock(at)} {title}", f"{spoken_clock(at)} — {_lower(title)}", False)
                    )
    reminders_list = _notes(ctx, "reminders_list")
    if callable(reminders_list):
        with contextlib.suppress(Exception):
            for item in reminders_list(include_done=False):
                at, text = float(item.get("at") or 0), str(item.get("text") or "").strip()
                if text and now <= at < end and not _is_timer(text):
                    items.append(
                        (1, at, f"{_clock(at)} {text}", f"{spoken_clock(at)} — {_lower(text)}", False)
                    )
    lead = f"{address[:1].upper()}{address[1:]}, на сегодня" if address else "На сегодня"
    # с персоной «Джарвис» первая фраза — готовая (её звук заранее), список — следом
    character = getattr(ctx, "character", None)
    today = character.today if character is not None else (lambda kind: "")
    if items:
        items.sort(key=lambda i: (i[0], i[1]))
        noun = TASKS if all(i[4] for i in items) else DEEDS
        count = numbers.count_text(len(items), noun)
        shown_items = [i[2] for i in items[:MAX_TASKS]]
        spoken_items = ", ".join(i[3] for i in items[:MAX_TASKS])
        extra = len(items) - MAX_TASKS
        tail = f" и ещё {extra}" if extra > 0 else ""
        first = today("lead")
        spoken = f"{first} У вас {count}: " if first else f"{lead} у вас {count}: "
        return f"На сегодня {count}: " + "; ".join(
            shown_items
        ) + tail + ".", spoken + spoken_items + tail + "."
    undated = [t for t in _open_tasks(ctx) if t.get("due") is None]
    if undated:
        names = [str(t.get("title") or "").strip() for t in undated[:3]]
        count = numbers.count_text(len(undated), TASKS)
        tail = " и другие" if len(undated) > 3 else ""
        first = today("undated") or f"{lead} ничего не назначено."
        return (
            f"На сегодня ничего не назначено. Без срока — {count}: " + ", ".join(names) + tail + ".",
            f"{first} Без срока — {count}: " + ", ".join(_lower(n) for n in names) + tail + ".",
        )
    text = today("empty") or f"{lead} дел нет. Можно выдохнуть."
    return text, text


async def build(
    ctx: Any, mode: str = "", slots: dict[str, Any] | None = None, *, now: float | None = None
) -> Part:
    """Сводка: (текст для тоста, текст для голоса)."""
    kind = (mode or "").strip().casefold()
    ts = _now(ctx) if now is None else now
    address = _address(ctx)
    if kind == "tasks":
        return open_tasks_text(ctx, ts)
    if kind == "today":
        return today_text(ctx, ts)
    if kind == "missed":
        return missed_text(ctx, ts, slots or {}, address)
    if kind in ("welcome", "hello"):
        since = (slots or {}).get("_away_since")
        found = await welcome(
            ctx, "back" if kind == "welcome" else "hello", now=ts, since=float(since) if since else None
        )
        return found.shown, found.spoken
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
