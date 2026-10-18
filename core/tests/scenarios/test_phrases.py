"""Фразы сценариев — динамические команды: регистрация и разбор, запуск через диспетчер, «запусти /
включи / выключи сценарий …», условия для фразы, сценарии в списке команд."""

import asyncio
from typing import Any

from jarvis.context import AppContext
from jarvis.scenarios.service import Scenarios
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.scenarios.conftest import MONDAY, Clock, calls, scenario


def movie(**over: Any) -> dict[str, Any]:
    return scenario(
        name="Кино",
        icon="moon",
        triggers=[{"type": "phrase", "phrases": ["включи режим кино", "режим кино"]}],
        actions=[{"type": "System.VolumeSet", "value": "70"}, {"type": "Media.PlayPause", "value": "play"}],
        **over,
    )


def matched(ctx: AppContext, text: str) -> list[tuple[str, dict[str, Any]]]:
    found = ctx.commands.match(text)
    return [] if found is None else [(p.command_id, p.slots) for p in found.parts]


async def test_phrases_register_as_dynamic_commands(ctx: AppContext, svc: Scenarios) -> None:
    assert svc.dynamic_commands() == []  # без сценариев — ни одной команды
    item = svc.create(movie())
    command_id = f"scenarios:{item['id']}"
    assert matched(ctx, "включи режим кино") == [(command_id, {})]
    assert matched(ctx, "джарвис, режим кино") == [(command_id, {})]
    info = ctx.commands.info(command_id)  # type: ignore[attr-defined]
    assert (
        info["name"] == "Сценарий «Кино»" and info["source"] == "dynamic" and info["category"] == "scenarios"
    )
    assert info["icon"] == "clock" and info["phrases"] == ["включи режим кино", "режим кино"]
    listed = ctx.commands.list_commands(category="scenarios")  # type: ignore[attr-defined]
    assert {c["id"]: c["title"] for c in listed["categories"]}["scenarios"] == "Сценарии"
    assert {c["name"] for c in listed["items"]} == {
        "Сценарий «Кино»",
        "Запустить сценарий",
        "Включить сценарий",
        "Выключить сценарий",
    }

    # выключенный сценарий фразу не слушает, новая фраза — сразу в индексе, удалённый — пропадает
    svc.update(item["id"], {"enabled": False})
    assert command_id not in {cid for cid, _ in matched(ctx, "включи режим кино")}
    svc.update(item["id"], {"enabled": True, "triggers": [{"type": "phrase", "phrases": ["время фильма"]}]})
    assert matched(ctx, "время фильма") == [(command_id, {})]
    svc.delete(item["id"])
    assert matched(ctx, "время фильма") == []
    assert svc.dynamic_commands() == []


async def test_phrase_runs_through_dispatcher(
    ctx: AppContext, svc: Scenarios, events: EventLog, spoken: list[str]
) -> None:
    item = svc.create(movie(say="Приятного просмотра, {address}"))
    result = await ctx.dispatcher.handle("включи режим кино", "voice")
    assert result["kind"] == "command"
    exec_result = result["result"]
    assert exec_result["ok"] is True and exec_result["title"] == "Сценарий «Кино»"
    assert exec_result["say"] == "Приятного просмотра, сэр"
    assert calls(ctx, "volume_set") == [(70,)]
    # историю и тост пишет диспетчер (один раз), голос — по итогу команды
    assert [h["item"]["title"] for h in events.of("history.added")] == ["Сценарий «Кино»"]
    assert [t["title"] for t in events.of("toast")] == ["Сценарий «Кино»"]
    assert spoken == ["Приятного просмотра, сэр"]
    assert events.of("scenario.run") == [{"id": item["id"], "name": "Кино", "trigger": "phrase", "ok": True}]
    assert svc.require(item["id"])["runs"] == 1


async def test_phrase_slots_reach_actions(ctx: AppContext, svc: Scenarios) -> None:
    svc.create(
        scenario(
            name="Громкость по фразе",
            triggers=[{"type": "phrase", "phrases": ["сделай атмосферу на {number}"]}],
            actions=[{"type": "System.VolumeSet", "value": "{number}"}],
        )
    )
    result = await ctx.dispatcher.handle("сделай атмосферу на сорок", "text")
    assert result["result"]["ok"] is True
    assert calls(ctx, "volume_set") == [(40,)]


async def test_phrase_respects_conditions_and_running(ctx: AppContext, svc: Scenarios, clock: Clock) -> None:
    item = svc.create(movie(conditions={"time_from": "18:00", "time_to": "23:59"}))
    clock.set(MONDAY.replace(hour=12))
    result = (await ctx.dispatcher.handle("режим кино", "text"))["result"]
    assert result["ok"] is False and result["message"] == "Сейчас условия сценария не выполняются"
    clock.set(MONDAY.replace(hour=20))
    assert (await ctx.dispatcher.handle("режим кино", "text"))["result"]["ok"] is True
    # «запусти сценарий …» — просьба вручную: условия не проверяются
    clock.set(MONDAY.replace(hour=12))
    assert (await ctx.dispatcher.handle("запусти сценарий кино", "text"))["result"]["ok"] is True
    assert svc.require(item["id"])["last_trigger"] == "manual"
    # «Выполнить» в списке команд — тоже вручную
    commands: Any = ctx.commands
    via_list = await commands.run_command(f"scenarios:{item['id']}")
    assert via_list.ok and via_list.title == "Сценарий «Кино»"
    assert svc.require(item["id"])["runs"] == 3

    slow = svc.create(
        scenario(
            name="Долгий",
            triggers=[{"type": "phrase", "phrases": ["долгий сценарий"]}],
            actions=[{"type": "Wait", "value": "0.3"}],
        )
    )
    first = asyncio.ensure_future(svc.run(slow["id"]))
    await asyncio.sleep(0.05)
    busy = (await ctx.dispatcher.handle("долгий сценарий", "text"))["result"]
    assert busy["ok"] is False and busy["message"] == "Сценарий уже выполняется"
    assert (await first).ok


async def test_voice_run_enable_disable_by_name(
    ctx: AppContext, svc: Scenarios, events: EventLog, spoken: list[str]
) -> None:
    item = svc.create(scenario(name="Игровой режим", triggers=[{"type": "app_open", "app": "Steam"}]))

    off = (await ctx.dispatcher.handle("выключи сценарий игровой режим", "voice"))["result"]
    assert off["ok"] is True and off["title"] == "Сценарий «Игровой режим»" and off["message"] == "Выключен"
    assert svc.require(item["id"])["enabled"] is False
    again = (await ctx.dispatcher.handle("отключи сценарий игровой режим", "text"))["result"]
    assert again["message"] == "Уже выключен"
    on = (await ctx.dispatcher.handle("включи сценарий игровой", "text"))["result"]
    assert on["ok"] is True and svc.require(item["id"])["enabled"] is True

    run = (await ctx.dispatcher.handle("запусти сценарий игровой режим", "voice"))["result"]
    assert run["ok"] is True and run["title"] == "Сценарий «Игровой режим»"
    assert calls(ctx, "volume_set") == [(30,)]
    assert events.of("scenario.run")[-1]["trigger"] == "manual"

    missing = (await ctx.dispatcher.handle("запусти сценарий полёт на марс", "text"))["result"]
    assert missing["ok"] is False and missing["message"] == "Не нашёл сценарий «полет на марс»"
    # голосом: «выключи сценарий …» отвечает своей фразой, запуск — коротким «Готово» (по настройкам)
    assert len(spoken) == 2 and spoken[0] == "Сценарий «Игровой режим» выключен"


async def test_find_by_name(svc: Scenarios) -> None:
    svc.create(scenario(name="Доброе утро"))
    svc.create(scenario(name="Игровой режим"))
    found = svc.find("доброе утро")
    assert found is not None and found["name"] == "Доброе утро"
    assert (svc.find("сценарий игровой режим") or {}).get("name") == "Игровой режим"
    assert (svc.find("игровой") or {}).get("name") == "Игровой режим"
    assert svc.find("совсем другое") is None
    assert svc.find("") is None


async def test_manual_run_records_history_toast_and_voice(
    ctx: AppContext, svc: Scenarios, events: EventLog, spoken: list[str]
) -> None:
    item = svc.create(movie(say="Готово"))
    result = await svc.run(item["id"])
    assert result.ok and result.title == "Сценарий «Кино»"
    history = events.of("history.added")[-1]["item"]
    assert history["text"] == "Запуск вручную" and history["status"] == "ok"
    toast = events.of("toast")[-1]
    assert toast == {**toast, "title": "Сценарий «Кино»", "status": "ok", "icon": "clock"}
    assert spoken == ["Готово"]
    # в тихом режиме — только тост
    ctx.settings.patch({"voice": {"silent_mode": True}})
    await svc.run(item["id"])
    assert spoken == ["Готово"]
    try:
        await svc.run("нет-такого")
    except UserFacingError as e:
        assert str(e) == "Сценарий не найден"
    else:
        raise AssertionError("ожидалась ошибка")
