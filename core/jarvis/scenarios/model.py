"""Формат сценария: проверка того, что пришло из редактора, и подписи для людей («Будни 8:00»).

Сценарий — {id, name, icon, enabled, triggers, conditions, actions, say, template, last_run, last_ok,
last_trigger, last_error, runs, created, updated}. Триггер — словарь с полем type:

- time {at: "HH:MM", weekdays: [0..6]} — по расписанию (0 — понедельник; пусто — каждый день);
- interval {minutes, only_active} — каждые N минут (only_active — считать только время за компьютером);
- phrase {phrases: [..]} — фраза, как у команды (те же слоты: {number}, {text}…);
- jarvis_start {} — при запуске Jarvis;
- app_open / app_close {app} — программа открылась / закрылась;
- idle_return {after_min} — человек вернулся после N минут отсутствия;
- battery_low {percent} — заряд упал до N% (один раз за разрядку);
- calendar_event {before_min} — за N минут до события календаря.

Условия — {weekdays: [..], time_from: "HH:MM" | "", time_to: "HH:MM" | ""}; пусто — без ограничения.
Действия — те же {type, value}, что у команд (jarvis.commands.model.Action).
"""

import hashlib
import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jarvis.commands import validate
from jarvis.commands.model import Action
from jarvis.services import UserFacingError
from jarvis.text import normalize

MAX_NAME = 60
MAX_TRIGGERS = 10
MAX_PHRASES = 20
MAX_PHRASE = 100
MAX_SAY = 300
MAX_APP = 120
DEFAULT_ICON = "sparkles"

TRIGGER_TYPES: dict[str, str] = {
    "time": "По расписанию",
    "interval": "Каждые N минут",
    "phrase": "Фраза",
    "jarvis_start": "Запуск Jarvis",
    "app_open": "Открыта программа",
    "app_close": "Закрыта программа",
    "idle_return": "Возвращение за компьютер",
    "battery_low": "Низкий заряд",
    "calendar_event": "Перед событием календаря",
}
# кто запустил сценарий (поле trigger события scenario.run): тип триггера или ручной запуск
MANUAL = "manual"

WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WORKDAYS = [0, 1, 2, 3, 4]
WEEKEND = [5, 6]

# значки сценариев (интерфейс рисует их сам) → ближайший общий значок для тостов и истории (CONTRACTS §1);
# значок программы («steam», «spotify») годится и там, и там
ICONS: dict[str, str] = {
    "sparkles": "ai",
    "sunrise": "weather",
    "sunset": "clock",
    "moon": "clock",
    "door": "lock",
    "gamepad": "media",
    "eye": "timer",
    "wave": "home",
    "coffee": "timer",
    "bolt": "system",
    "briefcase": "task",
    "battery": "power",
    "calendar": "calendar",
    "bell": "bell",
    "music": "media",
    "home": "home",
    "rocket": "command",
    "heart": "home",
    "book": "note",
    "monitor": "system",
}

_HHMM = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*$")
_TRUE = frozenset({"1", "true", "yes", "on", "да"})


def toast_icon(icon: str) -> str:
    """Значок сценария → ключ значка тоста и истории."""
    return ICONS.get(icon, icon or ICONS[DEFAULT_ICON])


# --- время и дни ---------------------------------------------------------------------------------------


def parse_hhmm(value: Any) -> tuple[int, int] | None:
    """«8:00», «08.30» → (8, 0), (8, 30); неверное — None."""
    m = _HHMM.match(str(value or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def fmt_hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def clock_label(value: str) -> str:
    """«08:00» → «8:00» (как пишут люди)."""
    hm = parse_hhmm(value)
    return f"{hm[0]}:{hm[1]:02d}" if hm else str(value)


def clean_weekdays(raw: Any) -> list[int]:
    """Дни недели 0–6 без повторов по порядку; все семь — то же, что «каждый день» (пустой список)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list | tuple):
        raise UserFacingError("Дни недели — это список чисел от 0 (понедельник) до 6 (воскресенье)")
    days: set[int] = set()
    for item in raw:
        try:
            day = int(item)
        except (TypeError, ValueError):
            raise UserFacingError("Дни недели — числа от 0 (понедельник) до 6 (воскресенье)") from None
        if not 0 <= day <= 6:
            raise UserFacingError("Дни недели — числа от 0 (понедельник) до 6 (воскресенье)")
        days.add(day)
    out = sorted(days)
    return [] if len(out) == 7 else out


def weekdays_label(days: list[int] | None) -> str:
    """[0..4] → «Будни», [5, 6] → «Выходные», [] → «Каждый день», иначе «Пн, Ср, Пт»."""
    d = sorted(set(days or []))
    if not d or len(d) == 7:
        return "Каждый день"
    if d == WORKDAYS:
        return "Будни"
    if d == WEEKEND:
        return "Выходные"
    return ", ".join(WEEKDAYS_SHORT[i] for i in d)


# --- триггеры --------------------------------------------------------------------------------------------


def _int(raw: Any, what: str, lo: int, hi: int) -> int:
    try:
        value = float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        raise UserFacingError(f"{what}: нужно число") from None
    if not math.isfinite(value) or not lo <= round(value) <= hi:
        raise UserFacingError(f"{what}: число от {lo} до {hi}")
    return round(value)


def _bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().casefold() in _TRUE
    return bool(raw)


def clean_phrases(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list | tuple) else [raw] if isinstance(raw, str) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        phrase = " ".join(str(item or "").split()).strip(" .,!?…")
        key = normalize(phrase)
        if not phrase or not key or key in seen:
            continue
        if len(phrase) > MAX_PHRASE:
            raise UserFacingError(f"Фраза «{phrase[:30]}…» слишком длинная")
        seen.add(key)
        out.append(phrase)
    if not out:
        raise UserFacingError("Добавьте фразу, по которой запускать сценарий")
    if len(out) > MAX_PHRASES:
        raise UserFacingError(f"Фраз — не больше {MAX_PHRASES}")
    return out


def clean_trigger(raw: Any) -> dict[str, Any]:
    """Триггер из редактора → нормальный вид. Ошибка — UserFacingError с понятным текстом."""
    if not isinstance(raw, dict):
        raise UserFacingError("Неверный формат триггера")
    kind = str(raw.get("type") or "").strip()
    if kind == "time":
        hm = parse_hhmm(raw.get("at"))
        if hm is None:
            raise UserFacingError("Время запуска пишется как «08:00»")
        return {"type": kind, "at": fmt_hhmm(*hm), "weekdays": clean_weekdays(raw.get("weekdays"))}
    if kind == "interval":
        return {
            "type": kind,
            "minutes": _int(raw.get("minutes"), "Интервал в минутах", 1, 1440),
            "only_active": _bool(raw.get("only_active")),
        }
    if kind == "phrase":
        return {"type": kind, "phrases": clean_phrases(raw.get("phrases"))}
    if kind == "jarvis_start":
        return {"type": kind}
    if kind in ("app_open", "app_close"):
        app = " ".join(str(raw.get("app") or "").split())
        if not app:
            raise UserFacingError("Выберите программу для триггера")
        if len(app) > MAX_APP:
            raise UserFacingError("Слишком длинное название программы")
        return {"type": kind, "app": app}
    if kind == "idle_return":
        return {"type": kind, "after_min": _int(raw.get("after_min"), "Сколько минут отсутствия", 1, 1440)}
    if kind == "battery_low":
        return {"type": kind, "percent": _int(raw.get("percent"), "Порог заряда, %", 1, 99)}
    if kind == "calendar_event":
        return {
            "type": kind,
            "before_min": _int(raw.get("before_min"), "За сколько минут до события", 0, 1440),
        }
    raise UserFacingError(f"Неизвестный триггер «{kind or '—'}»")


def clean_triggers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise UserFacingError("Триггеры — это список")
    if not raw:
        raise UserFacingError("Добавьте хотя бы один триггер — когда запускать сценарий")
    if len(raw) > MAX_TRIGGERS:
        raise UserFacingError(f"Триггеров — не больше {MAX_TRIGGERS}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, 1):
        try:
            trigger = clean_trigger(item)
        except UserFacingError as e:
            raise UserFacingError(f"Триггер {i}: {e}" if len(raw) > 1 else str(e)) from None
        key = trigger_key(trigger)
        if key not in seen:  # одинаковые триггеры не нужны дважды
            seen.add(key)
            out.append(trigger)
    return out


def trigger_key(trigger: dict[str, Any]) -> str:
    """Ключ триггера по содержимому: изменили время — это уже другой триггер со своим состоянием."""
    raw = json.dumps(trigger, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def trigger_label(trigger: dict[str, Any]) -> str:
    """Подпись триггера для людей: «Будни 8:00», «Фраза: доброе утро», «Открыт Steam»."""
    kind = trigger.get("type")
    if kind == "time":
        return f"{weekdays_label(trigger.get('weekdays'))} {clock_label(str(trigger.get('at', '')))}"
    if kind == "interval":
        minutes = int(trigger.get("minutes") or 0)
        base = "Каждую минуту" if minutes == 1 else f"Каждые {minutes} мин"
        return base + (" активности" if trigger.get("only_active") else "")
    if kind == "phrase":
        phrases = list(trigger.get("phrases") or [])
        extra = len(phrases) - 1
        return f"Фраза: {phrases[0] if phrases else '—'}" + (f" +{extra}" if extra > 0 else "")
    if kind == "jarvis_start":
        return "При запуске Jarvis"
    if kind == "app_open":
        return f"Открыт {trigger.get('app', '')}"
    if kind == "app_close":
        return f"Закрыт {trigger.get('app', '')}"
    if kind == "idle_return":
        return f"Возврат после {trigger.get('after_min')} мин"
    if kind == "battery_low":
        return f"Заряд ниже {trigger.get('percent')}%"
    if kind == "calendar_event":
        minutes = int(trigger.get("before_min") or 0)
        return "В начале события" if minutes == 0 else f"За {minutes} мин до события"
    return TRIGGER_TYPES.get(str(kind), str(kind))


# --- условия и остальное ----------------------------------------------------------------------------------


def clean_conditions(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise UserFacingError("Неверный формат условий")
    out: dict[str, Any] = {"weekdays": clean_weekdays(raw.get("weekdays")), "time_from": "", "time_to": ""}
    for key, what in (("time_from", "«с»"), ("time_to", "«до»")):
        value = raw.get(key)
        if value in (None, ""):
            continue
        hm = parse_hhmm(value)
        if hm is None:
            raise UserFacingError(f"Условия: время {what} пишется как «09:00»")
        out[key] = fmt_hhmm(*hm)
    if out["time_from"] and out["time_from"] == out["time_to"]:
        raise UserFacingError("Условия: время «с» и «до» совпадает")
    return out


def clean_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise UserFacingError("Укажите название сценария")
    if len(name) > MAX_NAME:
        raise UserFacingError(f"Название — не длиннее {MAX_NAME} символов")
    return name


def clean_icon(raw: Any) -> str:
    return str(raw or "").strip()[:40] or DEFAULT_ICON


def clean_say(raw: Any) -> str:
    say = " ".join(str(raw or "").split())
    if len(say) > MAX_SAY:
        raise UserFacingError(f"Фраза после сценария — не длиннее {MAX_SAY} символов")
    return say


def clean_actions(actions: list[Action]) -> list[dict[str, str]]:
    """Действия сценария проверяются как в своих командах (там же разрешена «Команда консоли»)."""
    if len(actions) > validate.MAX_ACTIONS:
        raise UserFacingError(f"Действий — не больше {validate.MAX_ACTIONS}")
    errors = validate.check_actions(actions, user=True)
    if errors:
        raise UserFacingError("; ".join(errors[:3]))
    return [a.model_dump(mode="json") for a in validate.clean_actions(actions)]


def fill_address(text: str, address: str) -> str:
    """«Отдохните, {address}: …» → «Отдохните, сэр: …»; без обращения — «Отдохните: …»."""
    if "{address}" not in text:
        return text
    if address:
        return text.replace("{address}", address)
    return re.sub(r",\s*\{address\}", "", text).replace("{address}", "").strip()


class ScenarioIn(BaseModel):
    """Тело POST /api/scenarios."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    icon: str = ""
    enabled: bool = True
    triggers: list[dict[str, Any]] = Field(default_factory=list)
    conditions: dict[str, Any] | None = None
    actions: list[Action] = Field(default_factory=list)
    say: str = ""
    template: str = ""  # из какого шаблона (редактор, открытый из витрины); незнакомый — не запоминается


class ScenarioPatch(BaseModel):
    """Тело PATCH /api/scenarios/{id}: только поля, которые меняются."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    triggers: list[dict[str, Any]] | None = None
    conditions: dict[str, Any] | None = None
    actions: list[Action] | None = None
    say: str | None = None
