"""Сетка стола: каталог виджетов, проверка раскладки и уплотнение.

Стол — сетка из 4 колонок; высоту строки задаёт интерфейс, строк сколько угодно (стол прокручивается).
Виджет занимает прямоугольник клеток: x 0..3, y ≥ 0, ширина и высота 1..4. Виджеты не перекрываются.
"""

import copy
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

COLUMNS = 4
MAX_SPAN = 4
MAX_WIDGETS = 60
MAX_ROWS = 200  # дальше раскладку не ищем — защита от мусорных координат
PLUGIN_PREFIX = "plugin:"  # виджеты плагинов: "plugin:<id>"


@dataclass(frozen=True)
class WidgetSpec:
    type: str
    name: str  # как называется в галерее «Добавить виджет»
    description: str
    category: str
    icon: str
    w: int  # размер по умолчанию, в клетках
    h: int
    min_w: int = 1
    min_h: int = 1
    max_w: int = MAX_SPAN
    max_h: int = MAX_SPAN
    config: dict[str, Any] = field(default_factory=dict)  # настройки по умолчанию

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "icon": self.icon,
            "w": self.w,
            "h": self.h,
            "min_w": self.min_w,
            "min_h": self.min_h,
            "max_w": self.max_w,
            "max_h": self.max_h,
            "config": copy.deepcopy(self.config),
        }


CATEGORIES = {
    "main": "Основное",
    "time": "Время и дела",
    "info": "Информация",
    "pc": "Компьютер",
    "home": "Дом и плагины",
}

_SPECS = (
    WidgetSpec(
        "avatar", "Аватар", "Живой аватар Jarvis: пульсирует, когда слушает и говорит", "main", "ai", 2, 3
    ),
    WidgetSpec(
        "control",
        "Управление",
        "Строка «Скажите Джарвису…», микрофон и быстрые кнопки",
        "main",
        "keyboard",
        2,
        1,
        min_w=2,
        max_h=2,
    ),
    WidgetSpec(
        "history",
        "История",
        "Последние команды и ответы Jarvis",
        "main",
        "history",
        2,
        2,
        config={"limit": 10},
    ),
    WidgetSpec("ai", "Jarvis AI", "Быстрый вопрос ИИ прямо со стола", "main", "ai", 2, 2, min_w=2),
    WidgetSpec(
        "quick",
        "Быстрые команды",
        "Кнопки для частых команд и сайтов",
        "main",
        "keyboard",
        2,
        1,
        max_h=2,
        config={"items": []},
    ),
    WidgetSpec("desk_links", "Столы", "Переход на другие столы", "main", "desk", 2, 1, max_h=2),
    WidgetSpec("clock", "Часы", "Время и дата", "time", "timer", 1, 1, max_h=2, config={"seconds": False}),
    WidgetSpec(
        "worldclock",
        "Мировое время",
        "Время в других городах",
        "time",
        "timer",
        2,
        1,
        max_h=2,
        config={"zones": ["Europe/London", "America/New_York", "Asia/Tokyo"]},
    ),
    WidgetSpec("calendar", "Календарь", "Месяц и ближайшие события", "time", "calendar", 2, 2),
    WidgetSpec(
        "tasks", "Задачи", "Список дел с отметками", "time", "task", 1, 2, config={"show_done": False}
    ),
    WidgetSpec("notes", "Заметки", "Последние заметки", "time", "note", 2, 2),
    WidgetSpec(
        "sticky",
        "Стикер",
        "Короткая записка на столе",
        "time",
        "note",
        1,
        1,
        max_w=2,
        max_h=2,
        config={"text": "", "color": "yellow"},
    ),
    WidgetSpec(
        "timer",
        "Таймер",
        "Обратный отсчёт: помидор, готовка, перерыв",
        "time",
        "timer",
        1,
        1,
        max_w=2,
        max_h=2,
        config={"seconds": 300},
    ),
    WidgetSpec("stopwatch", "Секундомер", "Секундомер с кругами", "time", "timer", 1, 1, max_w=2, max_h=2),
    WidgetSpec(
        "focus", "Фокус", "Помодоро: таймер фокуса и перерывов", "time", "timer", 1, 2, max_w=2, max_h=2
    ),
    WidgetSpec(
        "countdown",
        "Обратный отсчёт",
        "Сколько дней осталось до события",
        "time",
        "calendar",
        1,
        1,
        max_w=2,
        max_h=2,
        config={"title": "", "date": ""},
    ),
    WidgetSpec(
        "weather",
        "Погода",
        "Температура сейчас и прогноз на 5 дней",
        "info",
        "weather",
        1,
        1,
        max_h=2,
        config={"city": ""},
    ),
    WidgetSpec(
        "news", "Новости", "Главные новости или новости по теме", "info", "news", 2, 2, config={"topic": ""}
    ),
    WidgetSpec(
        "rates",
        "Курсы валют",
        "Курсы ЦБ: доллар, евро, юань…",
        "info",
        "finance",
        1,
        1,
        max_w=2,
        config={"codes": ["USD", "EUR", "CNY"]},
    ),
    WidgetSpec(
        "crypto",
        "Криптовалюта",
        "Bitcoin, Ethereum, Toncoin, Solana",
        "info",
        "finance",
        1,
        2,
        max_w=2,
        config={"ids": ["bitcoin", "ethereum", "the-open-network", "solana"]},
    ),
    WidgetSpec(
        "stocks",
        "Акции",
        "Котировки Мосбиржи",
        "info",
        "finance",
        1,
        2,
        max_w=2,
        config={"tickers": ["SBER", "GAZP", "LKOH", "YDEX", "T", "VTBR"]},
    ),
    WidgetSpec("media", "Плеер", "Что играет сейчас и кнопки управления", "pc", "media", 2, 1, max_h=2),
    WidgetSpec(
        "volume", "Громкость", "Ползунок громкости и выключение звука", "pc", "volume", 1, 1, max_w=2, max_h=1
    ),
    WidgetSpec("system", "Нагрузка ПК", "Процессор, видеокарта и память", "pc", "system", 2, 1, max_h=2),
    WidgetSpec("network", "Сеть", "Скорость загрузки и отдачи", "pc", "system", 1, 1, max_w=2, max_h=2),
    WidgetSpec("battery", "Батарея", "Заряд ноутбука", "pc", "power", 1, 1, max_w=2, max_h=1),
    WidgetSpec(
        "apps", "Программы", "Любимые программы в один клик", "pc", "window", 2, 1, config={"pinned": []}
    ),
    WidgetSpec(
        "recent", "Недавние файлы", "Файлы, которые вы открывали", "pc", "explorer", 2, 1, config={"limit": 6}
    ),
    WidgetSpec("smarthome", "Умный дом", "Свет, розетки и датчики", "home", "home", 2, 2),
    WidgetSpec("pet", "Питомец", "Виртуальный питомец на столе", "home", "pet", 2, 2),
    WidgetSpec(
        "plugins",
        "Плагины",
        "Быстрый доступ к плагинам",
        "home",
        "plugin",
        2,
        1,
        max_h=2,
        config={"items": []},
    ),
)
WIDGETS: dict[str, WidgetSpec] = {spec.type: spec for spec in _SPECS}
WIDGET_TYPES = tuple(WIDGETS)


def catalog() -> list[dict[str, Any]]:
    """Каталог для галереи «Добавить виджет»."""
    return [spec.to_json() | {"category_title": CATEGORIES[spec.category]} for spec in _SPECS]


def is_known_type(widget_type: str) -> bool:
    return widget_type in WIDGETS or (
        widget_type.startswith(PLUGIN_PREFIX) and 0 < len(widget_type) - len(PLUGIN_PREFIX) <= 40
    )


def spec_for(widget_type: str) -> WidgetSpec:
    spec = WIDGETS.get(widget_type)
    if spec is not None:
        return spec
    return WidgetSpec(widget_type, widget_type, "", "home", "plugin", 2, 1)


def new_widget_id() -> str:
    return "w" + uuid.uuid4().hex[:10]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_widget(raw: dict[str, Any]) -> dict[str, Any]:
    """Привести виджет к виду {id, type, x, y, w, h, config}: размеры в пределах типа, x+w ≤ 4, y ≥ 0."""
    widget_type = str(raw.get("type") or "")
    spec = spec_for(widget_type)
    w = max(spec.min_w, min(spec.max_w, COLUMNS, _int(raw.get("w"), spec.w)))
    h = max(spec.min_h, min(spec.max_h, _int(raw.get("h"), spec.h)))
    x = max(0, min(COLUMNS - w, _int(raw.get("x"), 0)))
    y = max(0, min(MAX_ROWS, _int(raw.get("y"), 0)))
    config = raw.get("config")
    widget_id = str(raw.get("id") or "").strip()[:40] or new_widget_id()
    return {
        "id": widget_id,
        "type": widget_type,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "config": copy.deepcopy(config) if isinstance(config, dict) else copy.deepcopy(spec.config),
    }


def cells(widget: dict[str, Any]) -> set[tuple[int, int]]:
    return {
        (x, y)
        for x in range(widget["x"], widget["x"] + widget["w"])
        for y in range(widget["y"], widget["y"] + widget["h"])
    }


def overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def validate(widgets: list[dict[str, Any]]) -> list[str]:
    """Что не так с раскладкой (пустой список — всё в порядке). Тексты — для журнала и тестов."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for i, w in enumerate(widgets):
        label = f"виджет {i} ({w.get('type')})"
        if not is_known_type(str(w.get("type") or "")):
            problems.append(f"{label}: неизвестный тип")
        for key in ("x", "y", "w", "h"):
            if not isinstance(w.get(key), int):
                problems.append(f"{label}: {key} не целое число")
        if problems and problems[-1].startswith(label):
            continue
        spec = spec_for(w["type"])
        if not (spec.min_w <= w["w"] <= min(spec.max_w, COLUMNS)):
            problems.append(f"{label}: ширина {w['w']} вне {spec.min_w}..{min(spec.max_w, COLUMNS)}")
        if not (spec.min_h <= w["h"] <= spec.max_h):
            problems.append(f"{label}: высота {w['h']} вне {spec.min_h}..{spec.max_h}")
        if w["x"] < 0 or w["x"] + w["w"] > COLUMNS:
            problems.append(f"{label}: выходит за {COLUMNS} колонки")
        if w["y"] < 0:
            problems.append(f"{label}: y < 0")
        widget_id = w.get("id")
        if widget_id is not None:
            if widget_id in seen_ids:
                problems.append(f"{label}: повторяется id {widget_id}")
            seen_ids.add(widget_id)
    if problems:
        return problems
    for i, a in enumerate(widgets):
        for j in range(i + 1, len(widgets)):
            if overlaps(a, widgets[j]):
                problems.append(f"виджеты {i} ({a['type']}) и {j} ({widgets[j]['type']}) перекрываются")
    return problems


def _fits(occupied: set[tuple[int, int]], x: int, y: int, w: int, h: int) -> bool:
    if x < 0 or x + w > COLUMNS:
        return False
    return all((cx, cy) not in occupied for cx in range(x, x + w) for cy in range(y, y + h))


def find_spot(occupied: set[tuple[int, int]], w: int, h: int, start_row: int = 0) -> tuple[int, int]:
    """Первое свободное место для w×h: сверху вниз, слева направо, начиная со строки start_row."""
    for y in range(max(0, start_row), MAX_ROWS + MAX_SPAN):
        for x in range(0, COLUMNS - w + 1):
            if _fits(occupied, x, y, w, h):
                return x, y
    return 0, max((cy for _, cy in occupied), default=-1) + 1


def _was_clamped(raw: dict[str, Any], widget: dict[str, Any]) -> bool:
    """Пришлось ли поправить координаты или размер (мусор из файла, старый формат, чужой клиент)."""
    return any(_int(raw.get(key), -1) != widget[key] for key in ("x", "y", "w", "h"))


def _push_down(occupied: set[tuple[int, int]], widget: dict[str, Any]) -> tuple[int, int]:
    """Место для виджета, налезшего на соседа: та же колонка, ниже; не вышло — первое свободное место."""
    for y in range(widget["y"], MAX_ROWS + MAX_SPAN):
        if _fits(occupied, widget["x"], y, widget["w"], widget["h"]):
            return widget["x"], y
    return find_spot(occupied, widget["w"], widget["h"])


def compact(widgets: list[dict[str, Any]], first: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Починить раскладку: размеры в пределах, всё внутри 4 колонок, без перекрытий.

    Сначала ставятся виджеты с id из first (их только что передвинули — место за ними), потом виджеты с
    правильными координатами, потом те, что пришлось поправить; внутри группы — сверху вниз. Кто стоит на
    свободном месте — остаётся, кто налез на соседа — сдвигается вниз в своей колонке. Порядок списка и id
    сохраняются (повтор id получает новый), неизвестные типы отбрасываются.
    """
    raws = [w for w in widgets if is_known_type(str(w.get("type") or ""))][:MAX_WIDGETS]
    normalized = [clamp_widget(w) for w in raws]
    clamped = [_was_clamped(raw, widget) for raw, widget in zip(raws, normalized, strict=True)]
    priority = {widget_id: rank for rank, widget_id in enumerate(first)}

    def sort_key(i: int) -> tuple[int, int, int, int, int]:
        widget = normalized[i]
        rank = priority.get(widget["id"])
        group = 0 if rank is not None else (2 if clamped[i] else 1)
        return (group, rank or 0, widget["y"], widget["x"], i)

    occupied: set[tuple[int, int]] = set()
    seen_ids: set[str] = set()
    for i in sorted(range(len(normalized)), key=sort_key):
        widget = normalized[i]
        if widget["id"] in seen_ids:
            widget["id"] = new_widget_id()
        seen_ids.add(widget["id"])
        if not _fits(occupied, widget["x"], widget["y"], widget["w"], widget["h"]):
            widget["x"], widget["y"] = _push_down(occupied, widget)
        occupied |= cells(widget)
    return normalized


def place(widgets: list[dict[str, Any]], new: dict[str, Any]) -> dict[str, Any]:
    """Поставить новый виджет на свободное место (или на указанные x, y, если они свободны)."""
    occupied: set[tuple[int, int]] = set()
    for w in widgets:
        occupied |= cells(w)
    widget = clamp_widget(new)
    wants_spot = new.get("x") is not None and new.get("y") is not None
    if not (wants_spot and _fits(occupied, widget["x"], widget["y"], widget["w"], widget["h"])):
        widget["x"], widget["y"] = find_spot(occupied, widget["w"], widget["h"])
    return widget


def rows_used(widgets: list[dict[str, Any]]) -> int:
    return max((w["y"] + w["h"] for w in widgets), default=0)
