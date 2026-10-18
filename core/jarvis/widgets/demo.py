"""Демо-данные виджетов для разработки и скриншотов без интернета (JARVIS_DEMO=1).

В обычной работе не используются: погода, курсы, крипта, акции и новости берутся из сети.
"""

import os
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any


def enabled() -> bool:
    return os.environ.get("JARVIS_DEMO") == "1"


def _weather(params: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    icons = [
        ("sun", "ясно"),
        ("partly", "переменная облачность"),
        ("cloud", "облачно"),
        ("rain", "дождь"),
        ("sun", "ясно"),
    ]
    days = []
    for i, (icon, condition) in enumerate(icons):
        d = today + timedelta(days=i)
        days.append(
            {
                "date": d.isoformat(),
                "weekday": "",
                "min": 6 + i,
                "max": 14 - i // 2,
                "code": 0,
                "condition": condition,
                "icon": icon,
            }
        )
    return {
        "city": str(params.get("city") or "Москва"),
        "temp": 14,
        "feels": 12,
        "code": 0,
        "condition": "ясно",
        "icon": "sun",
        "is_day": True,
        "humidity": 58,
        "wind": 3.2,
        "timezone": "Europe/Moscow",
        "daily": days,
        "updated": time.time(),
    }


def _rates(_: dict[str, Any]) -> dict[str, Any]:
    rows = [
        ("USD", "Доллар США", "$", 81.42, 0.31),
        ("EUR", "Евро", "€", 94.10, -0.18),
        ("CNY", "Юань", "¥", 11.35, 0.04),
        ("KZT", "Тенге", "₸", 0.16, 0.0),
    ]
    items = [
        {
            "code": c,
            "name": n,
            "symbol": s,
            "value": v,
            "previous": round(v - d, 4),
            "delta": d,
            "delta_pct": round(d / (v - d) * 100, 2) if v - d else 0.0,
            "nominal": 1,
        }
        for c, n, s, v, d in rows
    ]
    return {"date": date.today().isoformat(), "base": "RUB", "items": items, "updated": time.time()}


def _crypto(_: dict[str, Any]) -> dict[str, Any]:
    rows = [
        ("bitcoin", "BTC", "Bitcoin", 104250.0, 2.4),
        ("ethereum", "ETH", "Ethereum", 3890.5, -1.2),
        ("the-open-network", "TON", "Toncoin", 5.42, 3.8),
        ("solana", "SOL", "Solana", 182.3, 0.6),
    ]
    items = [
        {
            "id": i,
            "symbol": s,
            "name": n,
            "usd": usd,
            "rub": round(usd * 81.42, 2),
            "change_24h": ch,
            "change_24h_rub": ch,
        }
        for i, s, n, usd, ch in rows
    ]
    return {"items": items, "updated": time.time()}


def _stocks(_: dict[str, Any]) -> dict[str, Any]:
    rows = [
        ("SBER", "Сбербанк", 312.4, 1.2),
        ("GAZP", "ГАЗПРОМ ао", 128.9, -0.8),
        ("LKOH", "ЛУКОЙЛ", 7012.0, 0.4),
        ("YDEX", "Яндекс", 4321.5, 2.1),
    ]
    items = [
        {
            "ticker": t,
            "name": n,
            "last": last,
            "prev": round(last / (1 + p / 100), 2),
            "change": round(last - last / (1 + p / 100), 2),
            "change_pct": p,
            "trading": True,
        }
        for t, n, last, p in rows
    ]
    return {"items": items, "currency": "RUB", "updated": time.time()}


def _news(_: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    titles = [
        ("Учёные представили новый голосовой ИИ для дома", "Хабр"),
        ("Курс рубля укрепился на открытии торгов", "РБК"),
        ("В Москве ожидается тёплая и солнечная погода", "Lenta.ru"),
        ("Вышло большое обновление популярного редактора кода", "3DNews"),
        ("Сборная выиграла товарищеский матч со счётом 2:1", "Чемпионат"),
    ]
    items = [
        {"title": t, "url": "https://example.com/news", "source": s, "ts": now - i * 1800}
        for i, (t, s) in enumerate(titles)
    ]
    return {"items": items, "provider": "demo", "updated": now}


DEMO: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "weather": _weather,
    "rates": _rates,
    "crypto": _crypto,
    "stocks": _stocks,
    "news": _news,
}
