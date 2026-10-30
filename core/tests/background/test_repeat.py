"""Повторы фоновых задач: проверка расписания, следующий запуск, расписание словами, фразы «каждый
вечер …»."""

from datetime import datetime

import pytest

from jarvis.background.repeat import clean_repeat, describe, next_run, parse_repeat
from jarvis.services import UserFacingError


def ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def test_clean_repeat() -> None:
    assert clean_repeat(None) == {} and clean_repeat({}) == {}
    assert clean_repeat({"kind": "daily", "at": "9:05"}) == {"kind": "daily", "at": "09:05", "weekdays": []}
    assert clean_repeat({"kind": "daily", "at": "21.30", "weekdays": [4, 0, 0]}) == {
        "kind": "daily",
        "at": "21:30",
        "weekdays": [0, 4],
    }
    assert clean_repeat({"kind": "hours", "hours": "3"}) == {"kind": "hours", "hours": 3}
    for bad in (
        {"kind": "daily", "at": "25:00"},
        {"kind": "hours", "hours": 0},
        {"kind": "hours", "hours": 500},
        {"kind": "weekly"},
        "каждый день",
    ):
        with pytest.raises(UserFacingError):
            clean_repeat(bad)


def test_next_run_daily_and_weekdays() -> None:
    daily = clean_repeat({"kind": "daily", "at": "21:00"})
    # 2026-10-30 — пятница
    assert next_run(daily, ts("2026-10-30 15:00")) == ts("2026-10-30 21:00")
    assert next_run(daily, ts("2026-10-30 21:00")) == ts("2026-10-31 21:00")  # строго позже
    workdays = clean_repeat({"kind": "daily", "at": "09:00", "weekdays": [0, 1, 2, 3, 4]})
    assert next_run(workdays, ts("2026-10-30 10:00")) == ts("2026-11-02 09:00")  # пятница → понедельник
    hours = clean_repeat({"kind": "hours", "hours": 3})
    assert next_run(hours, ts("2026-10-30 10:00")) == ts("2026-10-30 13:00")
    assert next_run({}, ts("2026-10-30 10:00")) == 0.0


def test_describe() -> None:
    assert describe({}) == ""
    assert describe(clean_repeat({"kind": "daily", "at": "21:00"})) == "каждый день в 21:00"
    assert describe(clean_repeat({"kind": "daily", "at": "09:00", "weekdays": [0, 1, 2, 3, 4]})) == (
        "по будням в 9:00"
    )
    weekend = clean_repeat({"kind": "daily", "at": "10:00", "weekdays": [5, 6]})
    assert describe(weekend) == "по выходным в 10:00"
    assert describe(clean_repeat({"kind": "daily", "at": "10:00", "weekdays": [0]})) == (
        "по понедельникам в 10:00"
    )
    assert describe(clean_repeat({"kind": "daily", "at": "10:00", "weekdays": [0, 2]})) == "по Пн, Ср в 10:00"
    assert describe({"kind": "hours", "hours": 1}) == "каждый час"
    assert describe({"kind": "hours", "hours": 3}) == "каждые 3 часа"
    assert describe({"kind": "hours", "hours": 5}) == "каждые 5 часов"
    assert describe({"kind": "hours", "hours": 24}) == "раз в сутки"
    assert describe({"kind": "hours", "hours": 48}) == "раз в 2 дня"


@pytest.mark.parametrize(
    ("text", "repeat", "rest"),
    [
        (
            "каждый вечер записывай курс доллара ЦБ",
            {"kind": "daily", "at": "20:00", "weekdays": []},
            "Записывай курс доллара ЦБ",
        ),
        (
            "Каждый день в 21:30, проверяй цену на RTX 5070",
            {"kind": "daily", "at": "21:30", "weekdays": []},
            "Проверяй цену на RTX 5070",
        ),
        (
            "каждое утро собирай новости про ИИ",
            {"kind": "daily", "at": "09:00", "weekdays": []},
            "Собирай новости про ИИ",
        ),
        (
            "каждый вечер в 9 записывай вес канала",
            {"kind": "daily", "at": "21:00", "weekdays": []},
            "Записывай вес канала",
        ),
        (
            "ежедневно в 7 утра: погода на неделю",
            {"kind": "daily", "at": "07:00", "weekdays": []},
            "Погода на неделю",
        ),
        (
            "по будням в 10 проверяй вакансии",
            {"kind": "daily", "at": "10:00", "weekdays": [0, 1, 2, 3, 4]},
            "Проверяй вакансии",
        ),
        (
            "по пятницам вечером — обзор недели",
            {"kind": "daily", "at": "20:00", "weekdays": [4]},
            "Обзор недели",
        ),
        (
            "каждый понедельник в 10:00 сводка по проекту",
            {"kind": "daily", "at": "10:00", "weekdays": [0]},
            "Сводка по проекту",
        ),
        ("каждые 3 часа смотри курс биткоина", {"kind": "hours", "hours": 3}, "Смотри курс биткоина"),
        ("каждый час проверяй статус заказа", {"kind": "hours", "hours": 1}, "Проверяй статус заказа"),
    ],
)
def test_parse_repeat(text: str, repeat: dict[str, object], rest: str) -> None:
    assert parse_repeat(text) == (repeat, rest)


def test_parse_repeat_weekly_uses_today() -> None:
    friday = ts("2026-10-30 12:00")
    repeat, rest = parse_repeat("раз в неделю: сводка по проекту", now=friday)
    assert repeat == {"kind": "daily", "at": "09:00", "weekdays": [4]} and rest == "Сводка по проекту"


@pytest.mark.parametrize(
    "text",
    [
        "Узнай, что меняется каждый день на рынке",
        "Сравни ноутбуки до 100 000 ₽",
        "каждый день",  # без самой задачи
        "каждые 500 часов проверяй",  # слишком редко
    ],
)
def test_parse_repeat_ignores(text: str) -> None:
    assert parse_repeat(text) == ({}, text)
