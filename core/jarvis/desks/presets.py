"""Заготовки столов: 12 готовых раскладок для «Создать стол» и стол «Главная» при первом запуске.

Раскладки — на сетке 4 колонки (см. jarvis.desks.layout); тест проверяет, что виджеты не перекрываются и не
выходят за колонки. aliases — дополнительные слова, по которым стол находится голосом («открой стол музыка»).
"""

import copy
import datetime as dt
from typing import Any

HOME_PRESET = "home"


def _w(widget_type: str, x: int, y: int, w: int, h: int, **config: Any) -> dict[str, Any]:
    return {"type": widget_type, "x": x, "y": y, "w": w, "h": h, "config": config}


def next_session_date(today: dt.date | None = None) -> str:
    """Ближайшая сессия: зимняя — с 10 января, летняя — с 1 июня."""
    today = today or dt.date.today()
    candidates = (
        dt.date(today.year, 1, 10),
        dt.date(today.year, 6, 1),
        dt.date(today.year + 1, 1, 10),
    )
    return next(d for d in candidates if d > today).isoformat()


QUICK_HOME = [
    {"title": "Сверни всё", "icon": "window", "text": "сверни все окна"},
    {"title": "Скриншот", "icon": "screenshot", "text": "сделай скриншот"},
    {"title": "Музыка", "icon": "media", "text": "включи музыку"},
    {"title": "Тихий режим", "icon": "mute", "text": "включи тихий режим"},
]
QUICK_SITES = [
    {"title": "YouTube", "icon": "youtube", "url": "https://www.youtube.com"},
    {"title": "VK Видео", "icon": "vk", "url": "https://vkvideo.ru"},
    {"title": "Яндекс Музыка", "icon": "yandex", "url": "https://music.yandex.ru"},
    {"title": "Кинопоиск", "icon": "yandex", "url": "https://www.kinopoisk.ru"},
]
QUICK_GAMES = [
    {"title": "Steam", "icon": "steam", "text": "открой steam"},
    {"title": "Discord", "icon": "discord", "text": "открой discord"},
    {"title": "Скриншот", "icon": "screenshot", "text": "сделай скриншот"},
    {"title": "Без звука", "icon": "mute", "text": "выключи звук"},
]
QUICK_HOME_DEVICES = [
    {"title": "Включи свет", "icon": "home", "text": "включи свет"},
    {"title": "Выключи свет", "icon": "home", "text": "выключи свет"},
    {"title": "Погода", "icon": "weather", "text": "какая погода"},
]


def _home() -> list[dict[str, Any]]:
    # аватар справа во всю высоту верха; слева строка управления, история и плеер; ниже — дела и система
    return [
        _w("control", 0, 0, 2, 1),
        _w("avatar", 2, 0, 2, 3),
        _w("history", 0, 1, 2, 2, limit=10),
        _w("media", 0, 3, 2, 1),
        _w("weather", 2, 3, 1, 1, city=""),
        _w("rates", 3, 3, 1, 1, codes=["USD", "EUR", "CNY"]),
        _w("tasks", 0, 4, 1, 2, show_done=False),
        _w("calendar", 1, 4, 1, 2),
        _w("system", 2, 4, 2, 1),
        _w("quick", 2, 5, 2, 1, items=copy.deepcopy(QUICK_HOME)),
    ]


def _morning() -> list[dict[str, Any]]:
    return [
        _w("weather", 0, 0, 2, 2, city=""),
        _w("clock", 2, 0, 1, 1, seconds=False),
        _w("calendar", 3, 0, 1, 2),
        _w("rates", 2, 1, 1, 1, codes=["USD", "EUR", "CNY"]),
        _w("tasks", 0, 2, 2, 2, show_done=False),
        _w("news", 2, 2, 2, 2, topic=""),
    ]


def _work() -> list[dict[str, Any]]:
    return [
        _w("tasks", 0, 0, 1, 2, show_done=False),
        _w("calendar", 1, 0, 1, 2),
        _w("apps", 2, 0, 2, 1, pinned=["Google Chrome", "Telegram", "Word", "Excel", "Visual Studio Code"]),
        _w("recent", 2, 1, 2, 2, limit=8),
        _w("notes", 0, 2, 2, 2),
        _w("timer", 2, 3, 1, 1, seconds=1500),
        _w("clock", 3, 3, 1, 1, seconds=False),
    ]


def _study() -> list[dict[str, Any]]:
    return [
        _w("timer", 0, 0, 2, 2, seconds=1500, title="Помидор"),
        _w("countdown", 2, 0, 2, 1, title="До сессии", date=next_session_date()),
        _w("stopwatch", 2, 1, 1, 1),
        _w("clock", 3, 1, 1, 1, seconds=False),
        _w("tasks", 0, 2, 2, 2, show_done=False),
        _w("notes", 2, 2, 2, 2),
    ]


def _time_notes() -> list[dict[str, Any]]:
    return [
        _w("clock", 0, 0, 2, 1, seconds=True),
        _w("worldclock", 2, 0, 2, 1, zones=["Europe/London", "America/New_York", "Asia/Tokyo"]),
        _w("calendar", 0, 1, 2, 2),
        _w("tasks", 2, 1, 2, 2, show_done=False),
        _w("notes", 0, 3, 2, 2),
        _w("sticky", 2, 3, 1, 1, text="", color="yellow"),
        _w("timer", 3, 3, 1, 1, seconds=300),
        _w("stopwatch", 2, 4, 1, 1),
        _w("countdown", 3, 4, 1, 1, title="", date=""),
    ]


def _finance() -> list[dict[str, Any]]:
    return [
        _w("rates", 0, 0, 2, 2, codes=["USD", "EUR", "CNY", "GBP", "KZT", "TRY"]),
        _w("crypto", 2, 0, 2, 2, ids=["bitcoin", "ethereum", "the-open-network", "solana"]),
        _w("stocks", 0, 2, 2, 2, tickers=["SBER", "GAZP", "LKOH", "YDEX", "T", "VTBR"]),
        _w("news", 2, 2, 2, 2, topic="экономика"),
    ]


def _media() -> list[dict[str, Any]]:
    return [
        _w("media", 0, 0, 2, 2),
        _w("volume", 2, 0, 1, 1),
        _w("clock", 3, 0, 1, 1, seconds=False),
        _w("quick", 2, 1, 2, 1, items=copy.deepcopy(QUICK_SITES)),
        _w("news", 0, 2, 4, 2, topic=""),
    ]


def _games() -> list[dict[str, Any]]:
    return [
        _w("apps", 0, 0, 2, 2, pinned=["Steam", "Discord", "OBS Studio", "Minecraft Launcher"]),
        _w("system", 2, 0, 2, 1),
        _w("network", 2, 1, 1, 1),
        _w("volume", 3, 1, 1, 1),
        _w("quick", 0, 2, 2, 1, items=copy.deepcopy(QUICK_GAMES)),
        _w("media", 2, 2, 2, 1),
    ]


def _computer() -> list[dict[str, Any]]:
    return [
        _w("system", 0, 0, 2, 2),
        _w("calendar", 2, 0, 2, 2),
        _w("apps", 0, 2, 2, 1, pinned=["Проводник", "Google Chrome", "Блокнот", "Калькулятор"]),
        _w("recent", 2, 2, 2, 2, limit=8),
        _w("network", 0, 3, 1, 1),
        _w("battery", 1, 3, 1, 1),
    ]


def _smart_home() -> list[dict[str, Any]]:
    return [
        _w("smarthome", 0, 0, 2, 3),
        _w("weather", 2, 0, 2, 1, city=""),
        _w("quick", 2, 1, 2, 2, items=copy.deepcopy(QUICK_HOME_DEVICES)),
        _w("control", 0, 3, 4, 1),
    ]


def _pet() -> list[dict[str, Any]]:
    return [
        _w("pet", 0, 0, 2, 3),
        _w("plugins", 2, 0, 2, 1, items=["pet", "dictation", "reader", "summary"]),
        _w("quick", 2, 1, 2, 1, items=copy.deepcopy(QUICK_HOME)),
        _w("clock", 2, 2, 1, 1, seconds=False),
        _w("weather", 3, 2, 1, 1, city=""),
    ]


def _avatar_only() -> list[dict[str, Any]]:
    return [_w("avatar", 0, 0, 4, 4, style="circles", fill=True)]


# id, название, описание, иконка, слова для голоса, раскладка
_PRESETS: tuple[tuple[str, str, str, str, tuple[str, ...], Any], ...] = (
    ("empty", "Пустой", "Чистый стол — добавьте виджеты сами", "desk", (), list),
    ("morning", "Утро", "Погода, события, задачи и курсы", "sunrise", ("утренний", "завтрак"), _morning),
    ("work", "Работа", "Задачи, календарь, файлы и программы", "briefcase", ("рабочий", "офис"), _work),
    ("study", "Учёба", "Таймер, отсчёт до сессии, задачи", "book", ("учебный", "универ", "школа"), _study),
    (
        "time_notes",
        "Время и заметки",
        "Часы, календарь, задачи и заметки",
        "clock",
        ("часы", "календарь", "дела"),
        _time_notes,
    ),
    (
        "finance",
        "Финансы",
        "Криптовалюта, акции и курсы ЦБ",
        "chart",
        ("деньги", "биржа", "крипта"),
        _finance,
    ),
    (
        "media",
        "Медиа",
        "Плеер, громкость, новости и сайты",
        "music",
        ("музыка", "плеер", "видео", "кино"),
        _media,
    ),
    (
        "games",
        "Игры",
        "Программы, команды и нагрузка ПК",
        "gamepad",
        ("игровой", "гейминг", "геймерский"),
        _games,
    ),
    (
        "computer",
        "Компьютер",
        "Система, расписание, программы, файлы",
        "monitor",
        ("система", "пк", "комп"),
        _computer,
    ),
    (
        "smart_home",
        "Умный дом",
        "Устройства, команды и погода",
        "home",
        ("дом", "устройства", "свет"),
        _smart_home,
    ),
    ("pet", "Питомец", "Питомец и быстрые плагины", "paw", ("тамагочи", "зверек", "плагины"), _pet),
    (
        "avatar_only",
        "Только аватар",
        "Большие круги на весь стол",
        "avatar",
        ("аватар", "круги"),
        _avatar_only,
    ),
)

PRESET_IDS = tuple(p[0] for p in _PRESETS)
HOME = {"id": HOME_PRESET, "name": "Главная", "icon": "home", "aliases": ("главный", "домашний", "основной")}


def presets() -> list[dict[str, Any]]:
    """12 заготовок со свежими раскладками (даты в отсчётах считаются на сегодня)."""
    return [
        {
            "id": preset_id,
            "name": name,
            "description": description,
            "icon": icon,
            "aliases": list(aliases),
            "widgets": build(),
        }
        for preset_id, name, description, icon, aliases, build in _PRESETS
    ]


def preset(preset_id: str) -> dict[str, Any] | None:
    if preset_id == HOME_PRESET:
        return {
            "id": HOME_PRESET,
            "name": HOME["name"],
            "description": "Аватар, управление, история, плеер и главное на каждый день",
            "icon": HOME["icon"],
            "aliases": list(HOME["aliases"]),
            "widgets": _home(),
        }
    return next((p for p in presets() if p["id"] == preset_id), None)


def aliases_for(preset_id: str) -> tuple[str, ...]:
    if preset_id == HOME_PRESET:
        return tuple(HOME["aliases"])
    for pid, _, _, _, aliases, _ in _PRESETS:
        if pid == preset_id:
            return aliases
    return ()
