"""Триггеры сценариев на подменённых часах и платформе-заглушке: расписание (и пропуск во сне), интервалы,
программы, возвращение за компьютер, батарея, календарь, запуск Jarvis, условия, без двойных запусков."""

import asyncio
from datetime import timedelta
from typing import Any

from jarvis.bus import Event
from jarvis.context import AppContext
from jarvis.scenarios.service import Scenarios
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.scenarios.conftest import MONDAY, Clock, Machine, calls, scenario

TUESDAY = MONDAY + timedelta(days=1)
SATURDAY = MONDAY + timedelta(days=5)


async def tick(svc: Scenarios) -> list[str]:
    fired = await svc.tick()
    await svc.wait_idle()
    return fired


def runs(events: EventLog) -> list[dict[str, Any]]:
    return events.of("scenario.run")


# --- расписание ------------------------------------------------------------------------------------------


async def test_time_trigger_fires_once_with_history_toast_and_voice(
    ctx: AppContext, svc: Scenarios, clock: Clock, events: EventLog, spoken: list[str]
) -> None:
    item = svc.create(
        scenario(
            triggers=[{"type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4]}],
            say="Доброе утро, {address}",
        )
    )
    clock.set(MONDAY.replace(hour=7, minute=59, second=59))
    assert await tick(svc) == []
    clock.set(MONDAY.replace(hour=8, second=2))
    assert await tick(svc) == [f"{item['id']}:time"]
    assert calls(ctx, "volume_set") == [(30,)]
    assert runs(events) == [{"id": item["id"], "name": "Тест", "trigger": "time", "ok": True}]
    history = events.of("history.added")[-1]["item"]
    assert history["title"] == "Сценарий «Тест»" and history["status"] == "ok"
    assert history["text"] == "Будни 8:00"
    assert history["source"] == "scenario" and history["command_id"] == f"scenarios:{item['id']}"
    toast = events.of("toast")[-1]
    assert toast["title"] == "Сценарий «Тест»" and toast["status"] == "ok" and toast["icon"] == "weather"
    assert spoken == ["Доброе утро, сэр"]
    saved = svc.require(item["id"])
    assert saved["runs"] == 1 and saved["last_ok"] is True and saved["last_trigger"] == "time"
    assert saved["last_run"] == clock()

    # те же 8:00 повторно не срабатывают — ни на следующей проверке, ни позже утром
    for minutes in (0.1, 1, 30):
        clock.set(MONDAY.replace(hour=8) + timedelta(minutes=minutes))
        assert await tick(svc) == []
    # завтра — снова
    clock.set(TUESDAY.replace(hour=8, second=1))
    assert await tick(svc) == [f"{item['id']}:time"]
    # в субботу «по будням» молчит
    clock.set(SATURDAY.replace(hour=8, second=1))
    assert await tick(svc) == []
    assert len(runs(events)) == 2


async def test_missed_time_fires_after_sleep_only_if_less_than_10_minutes_late(
    svc: Scenarios, clock: Clock, events: EventLog
) -> None:
    item = svc.create(scenario())
    clock.set(MONDAY.replace(hour=7, minute=55))
    assert await tick(svc) == []
    # компьютер спал 7:55 → 8:07: опоздание 7 минут — сработает при пробуждении
    clock.set(MONDAY.replace(hour=8, minute=7))
    assert await tick(svc) == [f"{item['id']}:time"]
    # на следующий день спал 7:55 → 8:15: опоздание 15 минут — пропуск, и потом тоже не догоняет
    clock.set(TUESDAY.replace(hour=7, minute=55))
    assert await tick(svc) == []
    clock.set(TUESDAY.replace(hour=8, minute=15))
    assert await tick(svc) == []
    clock.advance(3)
    assert await tick(svc) == []
    assert len(runs(events)) == 1


async def test_time_after_creation_does_not_fire_and_survives_restart(
    ctx: AppContext, svc: Scenarios, clock: Clock
) -> None:
    clock.set(MONDAY.replace(hour=8, minute=5))
    item = svc.create(scenario())  # создан в 8:05 — сегодняшние 8:00 уже прошли
    assert await tick(svc) == []
    clock.set(TUESDAY.replace(hour=8, second=1))
    assert await tick(svc) == [f"{item['id']}:time"]
    # перезапуск ядра в 8:01: отметка «8:00 уже было» сохранилась в базе
    again = Scenarios(ctx)
    again.clock = clock
    clock.set(TUESDAY.replace(hour=8, minute=1))
    assert await again.tick() == []
    # а пропущенное при выключенном Jarvis (включили в 8:04 на следующий день) — срабатывает
    third = Scenarios(ctx)
    third.clock = clock
    clock.set(TUESDAY.replace(hour=8, minute=4) + timedelta(days=1))
    assert await third.tick() == [f"{item['id']}:time"]
    await third.wait_idle()


async def test_changed_or_reenabled_time_is_armed_from_now(svc: Scenarios, clock: Clock) -> None:
    item = svc.create(scenario())
    clock.set(MONDAY.replace(hour=7, minute=50))
    svc.update(item["id"], {"enabled": False})
    clock.set(MONDAY.replace(hour=8, minute=3))
    assert await tick(svc) == []  # выключен
    svc.update(item["id"], {"enabled": True})
    assert await tick(svc) == []  # включили в 8:03 — 8:00 было раньше
    svc.update(item["id"], {"triggers": [{"type": "time", "at": "08:10"}]})
    clock.set(MONDAY.replace(hour=8, minute=10, second=1))
    assert await tick(svc) == [f"{item['id']}:time"]


# --- интервалы -------------------------------------------------------------------------------------------


async def test_interval_every_n_minutes(svc: Scenarios, clock: Clock, events: EventLog) -> None:
    item = svc.create(scenario(triggers=[{"type": "interval", "minutes": 10}]))
    assert await tick(svc) == []
    clock.advance(9 * 60)
    assert await tick(svc) == []
    clock.advance(60)
    assert await tick(svc) == [f"{item['id']}:interval"]
    clock.advance(5 * 60)
    assert await tick(svc) == []
    # компьютер проспал час — один запуск, без «догонялок»
    clock.advance(60 * 60)
    assert await tick(svc) == [f"{item['id']}:interval"]
    clock.advance(3)
    assert await tick(svc) == []
    assert len(runs(events)) == 2


async def test_interval_counts_only_active_time(svc: Scenarios, clock: Clock, machine: Machine) -> None:
    svc.tick_s = 60  # проверка раз в минуту, чтобы не крутить тысячу итераций
    item = svc.create(scenario(triggers=[{"type": "interval", "minutes": 50, "only_active": True}]))
    fired: list[str] = []
    machine.idle = 5
    assert await tick(svc) == []  # первая проверка — точка отсчёта
    for _ in range(30):  # 30 минут работы
        clock.advance(60)
        fired += await tick(svc)
    machine.idle = 300  # отошёл на 5 минут — счёт стоит
    for _ in range(5):
        clock.advance(60)
        fired += await tick(svc)
    assert fired == []
    machine.idle = 5
    for _ in range(19):
        clock.advance(60)
        fired += await tick(svc)
    assert fired == []
    clock.advance(60)  # 50-я минута работы
    assert await tick(svc) == [f"{item['id']}:interval"]
    # долгий перерыв (больше 10 минут) обнуляет счёт
    for _ in range(40):
        clock.advance(60)
        fired += await tick(svc)
    machine.idle = 700
    clock.advance(60)
    fired += await tick(svc)
    machine.idle = 5
    for _ in range(20):
        clock.advance(60)
        fired += await tick(svc)
    assert fired == []


# --- программы -------------------------------------------------------------------------------------------


async def test_app_open_and_close_edges(
    ctx: AppContext, svc: Scenarios, clock: Clock, machine: Machine, events: EventLog
) -> None:
    opened = svc.create(
        scenario(
            name="Игровой режим",
            triggers=[{"type": "app_open", "app": "Steam"}],
            actions=[
                {"type": "Jarvis.SilentMode", "value": "on"},
                {"type": "Notify", "value": "Открыт {app}"},
            ],
        )
    )
    closed = svc.create(
        scenario(
            name="После игры",
            triggers=[{"type": "app_close", "app": "Steam"}],
            actions=[{"type": "Jarvis.SilentMode", "value": "off"}],
        )
    )
    assert await tick(svc) == []  # первая проверка — только запоминаем
    machine.open("steam.exe")
    clock.advance(3)
    assert await tick(svc) == [f"{opened['id']}:app_open"]
    assert ctx.settings.value.voice.silent_mode is True
    assert any(t["message"] == "Открыт Steam" and t["title"] == "Игровой режим" for t in events.of("toast"))
    clock.advance(3)
    assert await tick(svc) == []  # всё ещё открыт — повторно не срабатывает
    machine.close("steam.exe")
    clock.advance(3)
    assert await tick(svc) == [f"{closed['id']}:app_close"]
    assert ctx.settings.value.voice.silent_mode is False


async def test_app_already_running_at_start_is_not_an_edge(
    svc: Scenarios, clock: Clock, machine: Machine
) -> None:
    machine.open("steam.exe")
    svc.create(scenario(triggers=[{"type": "app_open", "app": "Steam"}]))
    for _ in range(3):
        clock.advance(3)
        assert await tick(svc) == []


# --- возвращение за компьютер ---------------------------------------------------------------------------


async def test_idle_return_greets_and_tells_what_was_missed(
    ctx: AppContext, svc: Scenarios, clock: Clock, machine: Machine, events: EventLog, spoken: list[str]
) -> None:
    svc.tick_s = 600  # редкие проверки — без признаков сна компьютера: человек просто отошёл
    item = svc.create(
        scenario(
            name="С возвращением",
            triggers=[{"type": "idle_return", "after_min": 15}],
            actions=[{"type": "Jarvis.Briefing", "value": "missed"}],
        )
    )
    clock.set(MONDAY.replace(hour=14))
    machine.idle = 2
    assert await tick(svc) == []
    # ушёл в 14:00, через 10 минут сработало напоминание
    clock.set(MONDAY.replace(hour=14, minute=10))
    machine.idle = 600
    assert await tick(svc) == []
    ctx.bus.publish(
        "reminder.fired", {"item": {"text": "Позвонить маме", "kind": "reminder", "missed": False}}
    )
    clock.set(MONDAY.replace(hour=14, minute=20))
    machine.idle = 1200
    assert await tick(svc) == []  # отсутствует 20 минут — ждём, когда вернётся
    clock.set(MONDAY.replace(hour=14, minute=25))
    machine.idle = 3
    assert await tick(svc) == [f"{item['id']}:idle_return"]
    toast = events.of("toast")[-1]
    assert toast["message"].startswith("С возвращением, сэр! Вас не было 25 минут.")
    assert "14:10 — Позвонить маме" in toast["message"]
    assert "в 14 часов 10 минут — Позвонить маме" in spoken[-1]
    # продолжает работать — повторно не здоровается
    for _ in range(3):
        clock.advance(3)
        assert await tick(svc) == []


async def test_idle_return_after_sleep(svc: Scenarios, clock: Clock, machine: Machine) -> None:
    item = svc.create(scenario(triggers=[{"type": "idle_return", "after_min": 15}]))
    machine.idle = 1
    assert await tick(svc) == []
    clock.advance(40 * 60)  # ноутбук закрыли на 40 минут, открыли — ввод только что был
    assert await tick(svc) == [f"{item['id']}:idle_return"]


# --- батарея ---------------------------------------------------------------------------------------------


async def test_battery_low_fires_once_per_discharge(
    svc: Scenarios, clock: Clock, machine: Machine, events: EventLog
) -> None:
    item = svc.create(
        scenario(
            triggers=[{"type": "battery_low", "percent": 20}],
            actions=[{"type": "Notify", "value": "Заряд {number}%"}],
        )
    )
    machine.battery, machine.charging = 25, False
    assert await tick(svc) == []
    clock.advance(30)
    machine.battery = 19
    assert await tick(svc) == []  # батарея проверяется раз в минуту
    clock.advance(30)
    assert await tick(svc) == [f"{item['id']}:battery_low"]
    assert events.of("toast")[-2]["message"] == "Заряд 19%"
    for level in (18, 12, 21):  # та же разрядка (21 — ещё не «зарядили»)
        clock.advance(60)
        machine.battery = level
        assert await tick(svc) == []
    clock.advance(60)
    machine.battery, machine.charging = 30, True
    assert await tick(svc) == []
    clock.advance(60)
    machine.battery, machine.charging = 20, False
    assert await tick(svc) == [f"{item['id']}:battery_low"]


async def test_no_battery_no_trigger(svc: Scenarios, clock: Clock, machine: Machine) -> None:
    svc.create(scenario(triggers=[{"type": "battery_low", "percent": 90}]))
    machine.battery = None
    for _ in range(3):
        clock.advance(61)
        assert await tick(svc) == []


# --- календарь -------------------------------------------------------------------------------------------


async def test_calendar_event_before_start(
    ctx: AppContext, svc: Scenarios, clock: Clock, events: EventLog
) -> None:
    item = svc.create(
        scenario(
            triggers=[{"type": "calendar_event", "before_min": 10}],
            actions=[{"type": "Notify", "value": "Скоро: {text}"}],
        )
    )
    notes: Any = ctx.notes
    clock.set(MONDAY.replace(hour=9))
    notes.event_add("Планёрка", MONDAY.replace(hour=10).timestamp())
    notes.event_add("Весь день отпуск", MONDAY.timestamp(), all_day=True)
    late = notes.event_add("Давний созвон", MONDAY.replace(hour=9, minute=20).timestamp())
    assert await tick(svc) == []
    # 9:20 — «Давний созвон» должен был напомнить в 9:10: опоздание 10 минут ещё допустимо
    clock.set(MONDAY.replace(hour=9, minute=20))
    assert await tick(svc) == [f"{item['id']}:calendar_event"]
    assert events.of("toast")[-2]["message"] == f"Скоро: {late['title']}"
    clock.set(MONDAY.replace(hour=9, minute=50, second=1))
    assert await tick(svc) == [f"{item['id']}:calendar_event"]
    assert events.of("toast")[-2]["message"] == "Скоро: Планёрка"
    clock.advance(3)
    assert await tick(svc) == []
    # событие перенесли на 11:00 — о нём напомнят снова
    planning = next(e for e in notes.events_list() if e["title"] == "Планёрка")
    notes.event_update(planning["id"], start=MONDAY.replace(hour=11).timestamp())
    clock.set(MONDAY.replace(hour=10, minute=50))
    assert await tick(svc) == [f"{item['id']}:calendar_event"]


async def test_calendar_event_too_late_is_skipped(ctx: AppContext, svc: Scenarios, clock: Clock) -> None:
    svc.create(scenario(triggers=[{"type": "calendar_event", "before_min": 5}]))
    notes: Any = ctx.notes
    notes.event_add("Встреча", MONDAY.replace(hour=10).timestamp())
    clock.set(MONDAY.replace(hour=9))
    assert await tick(svc) == []
    clock.set(MONDAY.replace(hour=10, minute=10))  # проспал: напоминание должно было быть в 9:55
    assert await tick(svc) == []


# --- запуск Jarvis ---------------------------------------------------------------------------------------


async def test_jarvis_start_fires_once_on_core_ready(
    ctx: AppContext, svc: Scenarios, events: EventLog
) -> None:
    item = svc.create(scenario(triggers=[{"type": "jarvis_start"}]))
    svc.create(scenario(name="Выключенный", enabled=False, triggers=[{"type": "jarvis_start"}]))
    svc.create(scenario(name="Другой", triggers=[{"type": "interval", "minutes": 5}]))
    svc._on_ready(Event("core.ready"))
    svc._on_ready(Event("core.ready"))
    await svc.wait_idle()
    assert [r["id"] for r in runs(events)] == [item["id"]]
    assert runs(events)[0]["trigger"] == "jarvis_start"


async def test_jarvis_start_in_running_core(running_ctx: AppContext) -> None:
    service = running_ctx.scenarios
    log = EventLog()
    running_ctx.bus.on("scenario.run", log)
    item = service.create(scenario(triggers=[{"type": "jarvis_start"}]))
    service._start_fired = False  # как будто ядро только запускается
    running_ctx.bus.publish("core.ready", {})
    for _ in range(100):
        if log.items:
            break
        await asyncio.sleep(0.02)
    assert log.of("scenario.run")[0]["id"] == item["id"]


# --- условия и двойные запуски ---------------------------------------------------------------------------


async def test_conditions_gate_triggers(svc: Scenarios, clock: Clock, events: EventLog) -> None:
    weekend = svc.create(scenario(name="Выходные", conditions={"weekdays": [5, 6]}))
    night = svc.create(
        scenario(
            name="Ночь",
            triggers=[{"type": "interval", "minutes": 60}],
            conditions={"time_from": "22:00", "time_to": "06:00"},
        )
    )
    clock.set(MONDAY.replace(hour=8, second=1))
    assert await tick(svc) == []  # понедельник — не выходной (а 8:00 отмечено как пройденное)
    clock.set(SATURDAY.replace(hour=8, second=1))
    assert await tick(svc) == [f"{weekend['id']}:time"]
    clock.set(SATURDAY.replace(hour=23))
    assert await tick(svc) == [f"{night['id']}:interval"]
    assert {r["id"] for r in runs(events)} == {weekend["id"], night["id"]}


async def test_no_overlapping_runs(ctx: AppContext, svc: Scenarios, clock: Clock, machine: Machine) -> None:
    item = svc.create(
        scenario(
            triggers=[{"type": "time", "at": "08:00"}, {"type": "app_open", "app": "Steam"}],
            actions=[{"type": "Wait", "value": "0.3"}, {"type": "System.VolumeSet", "value": "55"}],
        )
    )
    clock.set(MONDAY.replace(hour=7, minute=59, second=58))
    assert await svc.tick() == []
    machine.open("steam.exe")
    clock.set(MONDAY.replace(hour=8, second=1))
    # в одну проверку сработали два триггера — запуск один
    assert await svc.tick() == [f"{item['id']}:time"]
    assert svc.running(item["id"])
    try:
        await svc.run(item["id"])
    except UserFacingError as e:
        assert "уже выполняется" in str(e)
    else:
        raise AssertionError("второй запуск во время первого должен быть отклонён")
    await svc.wait_idle()
    assert not svc.running(item["id"])
    assert calls(ctx, "volume_set") == [(55,)]


async def test_failed_action_is_reported(
    ctx: AppContext, svc: Scenarios, clock: Clock, events: EventLog, spoken: list[str]
) -> None:
    item = svc.create(scenario(actions=[{"type": "Jarvis.OpenDesk", "value": "Нет такого"}], say="Готово"))
    clock.set(MONDAY.replace(hour=8, second=1))
    assert await tick(svc) == [f"{item['id']}:time"]
    run = runs(events)[-1]
    assert run["ok"] is False and "не найден" in run["error"]
    assert events.of("toast")[-1]["status"] == "error"
    assert events.of("history.added")[-1]["item"]["status"] == "error"
    assert svc.require(item["id"])["last_error"] == run["error"]
    assert spoken == []  # ошибку автоматического сценария не проговариваем


async def test_stop_cancels_running_scenario(
    ctx: AppContext, svc: Scenarios, clock: Clock, events: EventLog
) -> None:
    waits = [{"type": "Wait", "value": "300"}, {"type": "System.VolumeSet", "value": "40"}]
    item = svc.create(scenario(actions=waits))
    sid = item["id"]

    async def started() -> None:
        for _ in range(100):
            if sid in svc._actions:  # действия уже идут (ждут 5 минут)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("сценарий не начал выполняться")

    # сработал сам — «Стоп», пока ещё не дошёл до действий
    clock.set(MONDAY.replace(hour=8, second=1))
    assert await svc.tick() == [f"{sid}:time"]
    assert svc.stop_all() == 1
    await svc.wait_idle()
    assert runs(events)[-1]["cancelled"] is True and calls(ctx, "volume_set") == []
    # сработал сам — «Стоп» посреди ожидания
    clock.set(TUESDAY.replace(hour=8, second=1))
    assert await svc.tick() == [f"{sid}:time"]
    await started()
    assert svc.stop_all() == 1
    await svc.wait_idle()
    run = runs(events)[-1]
    assert run["ok"] is False and run["cancelled"] is True and run["error"] == "Остановлено"
    assert events.of("history.added")[-1]["item"]["status"] == "cancelled"
    assert calls(ctx, "volume_set") == [] and not svc.running(sid)
    assert svc.stop_all() == 0 and svc.stop_run(sid) is False
    # запуск кнопкой: «Стоп» возвращает итог тому, кто запустил
    task = asyncio.create_task(svc.run(sid))
    await started()
    assert svc.stop_run(sid) is True
    result = await task
    assert not result.ok and result.message == "Остановлено" and result.data["cancelled"] is True
    assert not svc.running(sid) and svc.require(sid)["last_error"] == "Остановлено"


async def test_disabled_and_deleted_scenarios_do_not_fire(svc: Scenarios, clock: Clock) -> None:
    item = svc.create(scenario(enabled=False))
    clock.set(MONDAY.replace(hour=8, second=1))
    assert await tick(svc) == []
    other = svc.create(scenario(name="Удалённый", triggers=[{"type": "interval", "minutes": 1}]))
    svc.delete(other["id"])
    clock.advance(120)
    assert await tick(svc) == []
    assert svc.require(item["id"])["runs"] == 0
