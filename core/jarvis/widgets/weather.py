"""Погода Open-Meteo (без ключа API): поиск города, текущая погода и прогноз на 5 дней.

Здесь только адреса и разбор ответов — сеть и кэш в jarvis.widgets.service.
"""

import datetime as dt
import math
import time
from typing import Any

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_FIELDS = "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,is_day"
DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min"

# Коды погоды WMO → (как сказать по-русски, ключ иконки: sun, partly, cloud, fog, rain, snow, storm)
WMO: dict[int, tuple[str, str]] = {
    0: ("ясно", "sun"),
    1: ("малооблачно", "partly"),
    2: ("облачно", "partly"),
    3: ("пасмурно", "cloud"),
    45: ("туман", "fog"),
    48: ("туман, изморозь", "fog"),
    51: ("слабая морось", "rain"),
    53: ("морось", "rain"),
    55: ("сильная морось", "rain"),
    56: ("ледяная морось", "rain"),
    57: ("ледяная морось", "rain"),
    61: ("небольшой дождь", "rain"),
    63: ("дождь", "rain"),
    65: ("сильный дождь", "rain"),
    66: ("ледяной дождь", "rain"),
    67: ("ледяной дождь", "rain"),
    71: ("небольшой снег", "snow"),
    73: ("снег", "snow"),
    75: ("сильный снег", "snow"),
    77: ("снежная крупа", "snow"),
    80: ("небольшой ливень", "rain"),
    81: ("ливень", "rain"),
    82: ("сильный ливень", "rain"),
    85: ("снегопад", "snow"),
    86: ("сильный снегопад", "snow"),
    95: ("гроза", "storm"),
    96: ("гроза с градом", "storm"),
    99: ("гроза с градом", "storm"),
}

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def describe(code: Any) -> tuple[str, str]:
    """Код WMO → (условие, иконка). Незнакомый код — «облачно» без подробностей."""
    try:
        return WMO[int(code)]
    except (KeyError, TypeError, ValueError):
        return ("", "cloud")


def round_half_up(value: Any) -> int:
    """14.5 → 15, −0.4 → 0: как округляют температуру на экране (round() в Python — банковское)."""
    return math.floor(float(value) + 0.5)


def geocode_params(city: str) -> dict[str, Any]:
    return {"name": city, "language": "ru", "count": 1, "format": "json"}


def parse_geocode(data: dict[str, Any]) -> dict[str, Any] | None:
    """Первый найденный город или None."""
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    return {
        "name": str(first.get("name") or ""),
        "lat": float(first["latitude"]),
        "lon": float(first["longitude"]),
        "country": str(first.get("country") or ""),
        "region": str(first.get("admin1") or ""),
        "timezone": str(first.get("timezone") or ""),
    }


def forecast_params(lat: float, lon: float) -> dict[str, Any]:
    return {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "current": CURRENT_FIELDS,
        "daily": DAILY_FIELDS,
        "timezone": "auto",
        "forecast_days": 5,
        "wind_speed_unit": "ms",  # ветер в м/с — так привычнее
    }


def parse_forecast(data: dict[str, Any], city: str) -> dict[str, Any]:
    """Ответ /v1/forecast → данные виджета. Нет нужных полей — KeyError/TypeError/ValueError."""
    current = data["current"]
    code = int(current["weather_code"])
    condition, icon = describe(code)
    daily_raw = data.get("daily") or {}
    days: list[dict[str, Any]] = []
    dates = daily_raw.get("time") or []
    codes = daily_raw.get("weather_code") or []
    highs = daily_raw.get("temperature_2m_max") or []
    lows = daily_raw.get("temperature_2m_min") or []
    for i, date in enumerate(dates):
        if i >= len(codes) or i >= len(highs) or i >= len(lows):
            break
        if highs[i] is None or lows[i] is None:
            continue
        day_condition, day_icon = describe(codes[i])
        days.append(
            {
                "date": date,
                "weekday": _weekday(date),
                "min": round_half_up(lows[i]),
                "max": round_half_up(highs[i]),
                "code": int(codes[i]) if codes[i] is not None else None,
                "condition": day_condition,
                "icon": day_icon,
            }
        )
    return {
        "city": city,
        "temp": round_half_up(current["temperature_2m"]),
        "feels": round_half_up(current.get("apparent_temperature", current["temperature_2m"])),
        "code": code,
        "condition": condition,
        "icon": icon,
        "is_day": bool(current.get("is_day", 1)),
        "humidity": _int_or_none(current.get("relative_humidity_2m")),
        "wind": _round1(current.get("wind_speed_10m")),
        "timezone": str(data.get("timezone") or ""),
        "daily": days,
        "updated": time.time(),
    }


def _weekday(date: str) -> str:
    try:
        return WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    except ValueError:
        return ""


def _int_or_none(value: Any) -> int | None:
    return None if value is None else round_half_up(value)


def _round1(value: Any) -> float | None:
    return None if value is None else round(float(value), 1)
