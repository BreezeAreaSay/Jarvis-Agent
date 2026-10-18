"""Формат сценария: проверка триггеров и условий, подписи «Будни 8:00», расписание и окно времени."""

from datetime import datetime, timedelta

import pytest

from jarvis.scenarios import templates
from jarvis.scenarios.model import (
    clean_conditions,
    clean_trigger,
    clean_triggers,
    clean_weekdays,
    fill_address,
    trigger_key,
    trigger_label,
    weekdays_label,
)
from jarvis.scenarios.schedule import conditions_ok, last_occurrence
from jarvis.services import UserFacingError
from tests.scenarios.conftest import MONDAY


def test_clean_triggers_normalize_values() -> None:
    assert clean_trigger({"type": "time", "at": "8.30", "weekdays": [4, 0, 0]}) == {
        "type": "time",
        "at": "08:30",
        "weekdays": [0, 4],
    }
    assert clean_trigger({"type": "interval", "minutes": "50", "only_active": "true"}) == {
        "type": "interval",
        "minutes": 50,
        "only_active": True,
    }
    assert clean_trigger({"type": "phrase", "phrases": [" Доброе  утро! ", "доброе утро", ""]}) == {
        "type": "phrase",
        "phrases": ["Доброе утро"],
    }
    assert clean_trigger({"type": "app_open", "app": " Steam "}) == {"type": "app_open", "app": "Steam"}
    assert clean_trigger({"type": "battery_low", "percent": 20.0}) == {"type": "battery_low", "percent": 20}
    assert clean_trigger({"type": "calendar_event", "before_min": 0})["before_min"] == 0
    assert clean_trigger({"type": "jarvis_start", "junk": 1}) == {"type": "jarvis_start"}
    # одинаковые триггеры остаются один раз
    assert len(clean_triggers([{"type": "jarvis_start"}, {"type": "jarvis_start"}])) == 1
    # все семь дней — то же, что «каждый день»
    assert clean_weekdays([0, 1, 2, 3, 4, 5, 6]) == []


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"type": "time", "at": "25:00"}, "08:00"),
        ({"type": "time", "at": "08:00", "weekdays": [7]}, "Дни недели"),
        ({"type": "interval", "minutes": 0}, "от 1 до 1440"),
        ({"type": "interval", "minutes": "много"}, "нужно число"),
        ({"type": "phrase", "phrases": []}, "Добавьте фразу"),
        ({"type": "app_close", "app": ""}, "Выберите программу"),
        ({"type": "idle_return", "after_min": -5}, "от 1 до 1440"),
        ({"type": "battery_low", "percent": 100}, "от 1 до 99"),
        ({"type": "teleport"}, "Неизвестный триггер «teleport»"),
        ("time", "Неверный формат"),
    ],
)
def test_bad_triggers_explain_in_russian(raw: object, error: str) -> None:
    with pytest.raises(UserFacingError, match=error):
        clean_trigger(raw)


def test_triggers_list_rules() -> None:
    with pytest.raises(UserFacingError, match="хотя бы один триггер"):
        clean_triggers([])
    with pytest.raises(UserFacingError, match="Триггер 2: "):
        clean_triggers([{"type": "jarvis_start"}, {"type": "time", "at": "x"}])


def test_conditions() -> None:
    assert clean_conditions(None) == {"weekdays": [], "time_from": "", "time_to": ""}
    assert clean_conditions({"weekdays": [5, 6], "time_from": "9:00", "time_to": ""}) == {
        "weekdays": [5, 6],
        "time_from": "09:00",
        "time_to": "",
    }
    with pytest.raises(UserFacingError, match="«с» и «до»"):
        clean_conditions({"time_from": "10:00", "time_to": "10:00"})
    with pytest.raises(UserFacingError, match="09:00"):
        clean_conditions({"time_to": "вечером"})


def test_labels() -> None:
    assert weekdays_label([0, 1, 2, 3, 4]) == "Будни"
    assert weekdays_label([5, 6]) == "Выходные"
    assert weekdays_label([]) == "Каждый день"
    assert weekdays_label([0, 2, 4]) == "Пн, Ср, Пт"
    assert trigger_label({"type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4]}) == "Будни 8:00"
    assert trigger_label({"type": "phrase", "phrases": ["доброе утро", "утро"]}) == "Фраза: доброе утро +1"
    assert trigger_label({"type": "app_open", "app": "Steam"}) == "Открыт Steam"
    assert trigger_label({"type": "app_close", "app": "Steam"}) == "Закрыт Steam"
    assert (
        trigger_label({"type": "interval", "minutes": 50, "only_active": True}) == "Каждые 50 мин активности"
    )
    assert trigger_label({"type": "interval", "minutes": 1, "only_active": False}) == "Каждую минуту"
    assert trigger_label({"type": "idle_return", "after_min": 15}) == "Возврат после 15 мин"
    assert trigger_label({"type": "battery_low", "percent": 20}) == "Заряд ниже 20%"
    assert trigger_label({"type": "calendar_event", "before_min": 10}) == "За 10 мин до события"
    assert trigger_label({"type": "calendar_event", "before_min": 0}) == "В начале события"
    assert trigger_label({"type": "jarvis_start"}) == "При запуске Jarvis"


def test_trigger_key_follows_content() -> None:
    a = {"type": "time", "at": "08:00", "weekdays": []}
    assert trigger_key(a) == trigger_key(dict(reversed(list(a.items()))))
    assert trigger_key(a) != trigger_key({**a, "at": "08:01"})


def test_fill_address() -> None:
    assert fill_address("Отдохните, {address}: посмотрите вдаль", "сэр") == "Отдохните, сэр: посмотрите вдаль"
    assert fill_address("Отдохните, {address}: посмотрите вдаль", "") == "Отдохните: посмотрите вдаль"
    assert fill_address("Без обращения", "") == "Без обращения"


def test_templates_are_valid_scenarios() -> None:
    assert [t["id"] for t in templates.TEMPLATES] == [
        "good_morning",
        "leaving",
        "game_mode",
        "end_of_day",
        "eye_break",
        "welcome_back",
    ]
    for template in templates.TEMPLATES:
        rendered = templates.render(template, "сэр")
        assert clean_triggers(rendered["triggers"])
        assert "{address}" not in str(rendered)
        assert rendered["actions"] or rendered["say"]


def test_last_occurrence_respects_weekdays() -> None:
    monday_9 = (MONDAY + timedelta(hours=9)).timestamp()
    assert last_occurrence("08:00", [], monday_9) == (MONDAY + timedelta(hours=8)).timestamp()
    # в понедельник 7:00 последнее «по будням 8:00» — в пятницу
    monday_7 = (MONDAY + timedelta(hours=7)).timestamp()
    friday_8 = datetime(2026, 10, 16, 8, 0).timestamp()
    assert last_occurrence("08:00", [0, 1, 2, 3, 4], monday_7) == friday_8
    # «по выходным» из понедельника — воскресенье
    assert last_occurrence("08:00", [5, 6], monday_9) == datetime(2026, 10, 18, 8, 0).timestamp()
    assert last_occurrence("нет", [], monday_9) is None


def test_conditions_window_and_days() -> None:
    at = lambda h, m=0, day=MONDAY: day.replace(hour=h, minute=m).timestamp()  # noqa: E731
    office = {"weekdays": [0, 1, 2, 3, 4], "time_from": "09:00", "time_to": "18:00"}
    assert conditions_ok(office, at(9))
    assert conditions_ok(office, at(18, 0))
    assert not conditions_ok(office, at(18, 1))
    assert not conditions_ok(office, at(12, day=datetime(2026, 10, 24)))  # суббота
    night = {"time_from": "22:00", "time_to": "06:00"}
    assert conditions_ok(night, at(23)) and conditions_ok(night, at(5, 59))
    assert not conditions_ok(night, at(12))
    assert conditions_ok({"time_from": "20:00"}, at(21)) and not conditions_ok({"time_from": "20:00"}, at(19))
    assert conditions_ok({"time_to": "10:00"}, at(9)) and not conditions_ok({"time_to": "10:00"}, at(11))
    assert conditions_ok({}, at(3)) and conditions_ok(None, at(3))
