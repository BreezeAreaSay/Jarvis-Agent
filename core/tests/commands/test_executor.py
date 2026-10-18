"""Выполнение команд на платформе-заглушке: вызовы ОС, слоты, информация, подтверждения, ошибки."""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.commands.model import CommandIn
from jarvis.commands.service import Commands
from jarvis.context import AppContext
from jarvis.platform.base import PlatformError
from jarvis.services import AskResult, DynamicCommand, ExecResult
from tests.commands.conftest import calls
from tests.conftest import EventLog


async def run(cmds: Commands, text: str, source: Any = "text") -> ExecResult:
    result = await cmds.run_text(text, source)
    assert result is not None, f"не распознано: {text!r}"
    return result


def user(cmds: Commands, *actions: tuple[str, str], **kw: Any) -> str:
    data = CommandIn(
        name=kw.pop("name", "Тест"),
        phrases=kw.pop("phrases", ["тестовая команда"]),
        actions=[{"type": t, "value": v} for t, v in actions],  # type: ignore[misc]
        **kw,
    )
    return cmds.create(data)["id"]


async def test_volume(cmds: Commands, ctx: AppContext) -> None:
    result = await run(cmds, "громкость на пятьдесят")
    assert result.ok and result.title == "Громкость на 50" and result.message == "Громкость 50%"
    assert calls(ctx, "volume_set")[-1] == (50,)
    await run(cmds, "сделай громче")
    assert calls(ctx, "volume_change")[-1] == (10,)
    await run(cmds, "тише на 20")
    assert calls(ctx, "volume_change")[-1] == (-20,)
    result = await run(cmds, "выключи звук")
    assert result.message == "Звук выключен" and calls(ctx, "mute_set")[-1] == (True,)
    info = await run(cmds, "какая громкость")
    assert info.message.startswith("Громкость 40%") and info.say.startswith("Громкость 40 процентов")


async def test_media_respects_state(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run(cmds, "включи музыку")  # уже играет — клавишу не жмём
    assert result.message == "Музыка уже играет" and calls(ctx, "media_key") == []
    await run(cmds, "пауза")
    assert calls(ctx, "media_key") == [("play_pause",)]
    result = await run(cmds, "пауза")
    assert result.message == "Уже на паузе" and len(calls(ctx, "media_key")) == 1
    await run(cmds, "продолжи")
    await run(cmds, "следующий трек")
    assert calls(ctx, "media_key")[-2:] == [("play_pause",), ("next",)]
    # ничего не играет и плеера нет — запускается установленная музыкальная программа
    monkeypatch.setattr(ctx.platform, "now_playing", lambda: None)
    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("jarvis.commands.executor.asyncio.sleep", fast_sleep)
    result = await run(cmds, "включи музыку")
    assert result.message == "Запускаю Spotify"
    assert calls(ctx, "launch")[-1] == ("Spotify.exe", "") and calls(ctx, "media_key")[-1] == ("play_pause",)


async def test_now_playing_and_info(cmds: Commands) -> None:
    media = await run(cmds, "что играет")
    assert (
        media.message.startswith("Neon Coast — Night Drive")
        and media.say == "Сейчас играет Night Drive, Neon Coast"
    )
    clock = await run(cmds, "который час")
    assert clock.say.startswith("Сейчас ") and ":" in clock.message
    date = await run(cmds, "какое сегодня число")
    assert date.say.startswith("Сегодня ")
    battery = await run(cmds, "заряд батареи")
    assert "Батареи нет" in battery.message
    load = await run(cmds, "загрузка процессора")
    assert load.message.startswith("Процессор") and "память 46%" in load.message


async def test_weather(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    async def data(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        asked.append(params)
        if params.get("city") == "Казани":
            return {"error": "Город «Казани» не найден"}
        return {
            "city": params.get("city") or "Москва",
            "temp": -3,
            "feels": -8,
            "condition": "снег",
            "wind": 4.2,
        }

    monkeypatch.setattr(ctx.widgets, "data", data)
    result = await run(cmds, "какая погода")
    assert result.ok and result.message == "Москва: -3°, снег, ощущается как -8°, ветер 4.2 м/с"
    assert result.say == "Сейчас минус 3 градуса, снег, ощущается как минус 8 градусов"
    assert asked[-1] == {}
    result = await run(cmds, "какая погода в питере")
    assert asked[-1] == {"city": "Санкт-Петербург"} and result.message.startswith("Санкт-Петербург:")
    await run(cmds, "погода в казани")  # склонение: «Казани» не найден → «Казань»
    assert asked[-1] == {"city": "Казань"}

    async def empty(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(ctx.widgets, "data", empty)
    result = await run(cmds, "какая погода")
    assert not result.ok and "погод" in result.message


async def test_apps_and_windows(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    await run(cmds, "открой спотифай")
    assert calls(ctx, "launch")[-1] == ("Spotify.exe", "")
    await run(cmds, "открой зум")  # нет в «Пуске» — ссылка приложения
    assert calls(ctx, "open_url")[-1] == ("zoommtg://",)
    await run(cmds, "закрой телеграм")
    assert calls(ctx, "close_app")[-1] == ("Telegram.exe",)
    await run(cmds, "переключись на спотифай")
    assert calls(ctx, "window_action")[-1] == ("focus", "Spotify.exe")
    await run(cmds, "сверни все окна")
    assert calls(ctx, "window_action")[-1] == ("minimize_all", None)
    await run(cmds, "разверни окно")
    assert calls(ctx, "window_action")[-1] == ("maximize", None)
    monkeypatch.setattr(ctx.platform, "close_app", lambda name: 0)
    result = await run(cmds, "закрой телеграм")
    assert not result.ok and result.message == "Telegram не запущен"

    def no_window(action: str, target: Any = None) -> None:
        raise PlatformError("окно не найдено")

    monkeypatch.setattr(ctx.platform, "window_action", no_window)
    monkeypatch.setattr(ctx.platform, "is_running", lambda name: False)
    result = await run(cmds, "переключись на дискорд")
    assert not result.ok and result.message == "Discord не запущен"


async def test_sites_search_folders(cmds: Commands, ctx: AppContext) -> None:
    await run(cmds, "открой ютуб")
    assert calls(ctx, "open_url")[-1] == ("https://www.youtube.com",)
    result = await run(cmds, "найди на ютубе котиков")
    url = calls(ctx, "open_url")[-1][0]
    assert url == "https://www.youtube.com/results?search_query=%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%BE%D0%B2"
    assert result.message == "Ищу: котиков" and result.title == "Поиск на YouTube: котиков"
    await run(cmds, "загугли рецепт блинов")
    assert calls(ctx, "open_url")[-1] == (
        "https://www.google.com/search?q=%D1%80%D0%B5%D1%86%D0%B5%D0%BF%D1%82+%D0%B1%D0%BB%D0%B8%D0%BD%D0%BE%D0%B2",
    )
    await run(cmds, "открой загрузки")
    assert calls(ctx, "open_path")[-1] == (r"C:\Users\me\Downloads",)
    await run(cmds, "открой скриншоты")
    assert calls(ctx, "open_path")[-1] == (r"C:\Users\me\Pictures\Screenshots",)


async def test_keys_and_typing(cmds: Commands, ctx: AppContext) -> None:
    await run(cmds, "нажми контрол шифт эс")
    assert calls(ctx, "press_keys")[-1] == ("ctrl+shift+s",)
    await run(cmds, "скопируй")
    assert calls(ctx, "press_keys")[-1] == ("ctrl+c",)
    await run(cmds, "напечатай привет мир")
    assert calls(ctx, "type_text")[-1] == ("привет мир",)
    user(cmds, ("Keys.Press", "ctrl+k ctrl+s"), ("Keys.Press", "down*3"))
    before = len(calls(ctx, "press_keys"))
    await run(cmds, "тестовая команда")
    assert calls(ctx, "press_keys")[before:] == [("ctrl+k",), ("ctrl+s",), ("down",), ("down",), ("down",)]


async def test_mouse_clipboard_brightness(cmds: Commands, ctx: AppContext) -> None:
    user(
        cmds,
        ("Mouse.ClickLeft", "100 200"),
        ("Mouse.DoubleClick", ""),
        ("Mouse.Move", "center"),
        ("Mouse.Scroll", "-5"),
        ("Mouse.ClickRight", "5,6"),
    )
    await run(cmds, "тестовая команда")
    assert calls(ctx, "mouse_click") == [
        (100, 200, "left", False),
        (None, None, "left", True),
        (5, 6, "right", False),
    ]
    assert calls(ctx, "mouse_move") == [(960, 540)] and calls(ctx, "mouse_scroll") == [(-5,)]
    await run(cmds, "скопируй в буфер обмена привет")
    assert calls(ctx, "clipboard_set")[-1] == ("привет",)
    result = await run(cmds, "сделай ярче")
    assert calls(ctx, "brightness_set")[-1] == (80,) and result.message == "Яркость 80%"


async def test_screenshot_saved(cmds: Commands, ctx: AppContext, data_dir: Path) -> None:
    result = await run(cmds, "сделай скриншот")
    path = Path(result.data["path"])
    assert result.ok and path.exists() and path.parent == data_dir / "screenshots"
    assert path.read_bytes().startswith(b"\x89PNG")


async def test_timer_and_reminder(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    added: list[tuple[str, float]] = []
    monkeypatch.setattr(
        ctx.notes, "reminder_add", lambda text, at, repeat=None: added.append((text, at)) or {}
    )
    result = await run(cmds, "поставь таймер на 5 минут")
    assert result.ok and result.say == "Поставил таймер на 5 минут"
    assert added[-1][0] == "Таймер на 5 минут" and abs(added[-1][1] - (time.time() + 300)) < 5
    result = await run(cmds, "напомни через 10 минут позвонить маме")
    assert added[-1][0] == "Позвонить маме" and abs(added[-1][1] - (time.time() + 600)) < 5
    assert result.say == "Напомню через 10 минут"
    await run(cmds, "таймер на полчаса")
    assert abs(added[-1][1] - (time.time() + 1800)) < 5


async def test_microphone_off_and_on(cmds: Commands, ctx: AppContext) -> None:
    result = await run(cmds, "не слушай меня")
    assert ctx.settings.value.voice.enabled is False
    assert result.say == "Больше не слушаю" and "Ctrl+Alt+M" in result.message
    result = await run(cmds, "включи прослушивание")
    assert ctx.settings.value.voice.enabled is True and result.say == "Слушаю"


async def test_jarvis_actions(cmds: Commands, ctx: AppContext, events: EventLog) -> None:
    await run(cmds, "тихий режим")
    assert ctx.settings.value.voice.silent_mode is True
    result = await run(cmds, "выключи тихий режим")
    assert ctx.settings.value.voice.silent_mode is False and result.say == "Тихий режим выключен"
    await run(cmds, "открой окно джарвиса")
    await run(cmds, "открой настройки джарвиса")
    await run(cmds, "скрой окно")
    assert events.of("ui.show") == [{}, {"page": "settings"}]
    assert events.of("ui.hide") == [{}]
    result = await run(cmds, "открой стол игры")
    assert not result.ok and result.message == "Стол «игры» не найден"


async def test_speak_notify_plugin_ask(
    cmds: Commands, ctx: AppContext, events: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[str] = []
    plugin_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def say(text: str, **kw: Any) -> None:
        said.append(text)

    async def action(plugin_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        plugin_calls.append((plugin_id, name, args))
        return {"ok": True}

    async def ask(text: str, **kw: Any) -> AskResult:
        return AskResult("chat1", "Столица Франции — Париж.", spoken="Париж.")

    monkeypatch.setattr(ctx.tts, "say", say)
    monkeypatch.setattr(ctx.plugins, "action", action)
    monkeypatch.setattr(type(ctx.ai), "configured", property(lambda self: True))
    monkeypatch.setattr(ctx.ai, "ask", ask)
    user(
        cmds,
        ("Speak", "Готово, сэр"),
        ("Notify", "Сценарий выполнен"),
        ("Jarvis.Plugin", "dictation:toggle"),
        ("Jarvis.Plugin", "reader.read"),
    )
    result = await run(cmds, "тестовая команда")
    assert result.ok and said == ["Готово, сэр"]
    assert events.of("toast")[-1]["message"] == "Сценарий выполнен"
    assert plugin_calls == [("dictation", "toggle", {}), ("reader", "read", {})]
    answer = await run(cmds, "спроси у ии какая столица франции")
    assert answer.ok and answer.say == "Париж." and answer.data["chat_id"] == "chat1"


async def test_nested_commands(cmds: Commands, ctx: AppContext) -> None:
    user(cmds, ("Jarvis.Command", "builtin.volume.mute"), ("Jarvis.Command", "сделай громче"))
    result = await run(cmds, "тестовая команда")
    assert result.ok and calls(ctx, "mute_set")[-1] == (True,) and calls(ctx, "volume_change")[-1] == (10,)
    looped = user(cmds, ("Jarvis.Command", "петля"), name="Петля", phrases=["петля"])
    result = await cmds.run_command(looped)
    assert not result.ok and "цепочка" in result.message


async def test_shell_only_in_user_commands(cmds: Commands, ctx: AppContext) -> None:
    command_id = user(cmds, ("Launch.Shell", "exit 0"))
    result = await cmds.run_command(command_id)
    assert result.ok and result.data["pid"] > 0
    entry = cmds.get_entry("builtin.media.play")
    assert entry is not None
    from jarvis.commands.model import Action

    state_entry = entry.__class__(
        entry.command.model_copy(update={"actions": [Action(type="Launch.Shell", value="exit 0")]}),
        "pack",
        pack="x",
    )
    result = await cmds.executor.run_entry(state_entry, {}, "text")
    assert not result.ok and "своих команд" in result.message
    await asyncio.sleep(0.05)


async def test_confirmation_allow_and_deny(cmds: Commands, ctx: AppContext, events: EventLog) -> None:
    async def answer(allow: bool) -> ExecResult:
        task = asyncio.create_task(cmds.run_text("выключи компьютер", "text"))  # type: ignore[arg-type]
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ctx.confirmations.pending():
                break
        pending = ctx.confirmations.pending()
        assert pending and pending[0]["title"] == "Выключить компьютер?" and pending[0]["kind"] == "command"
        ctx.confirmations.resolve(pending[0]["id"], allow)
        result = await task
        assert result is not None
        return result

    denied = await answer(False)
    assert not denied.ok and denied.data == {"cancelled": True} and calls(ctx, "power") == []
    allowed = await answer(True)
    assert allowed.ok and calls(ctx, "power") == [("shutdown", 0)]
    assert len(events.of("confirm.request")) == 2


async def test_shutdown_with_delay_says_when(cmds: Commands, ctx: AppContext) -> None:
    task = asyncio.create_task(cmds.run_text("выключи компьютер через 30 минут", "text"))  # type: ignore[arg-type]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ctx.confirmations.pending():
            break
    ctx.confirmations.resolve(ctx.confirmations.pending()[0]["id"], True)
    result = await task
    assert result is not None and result.ok
    assert calls(ctx, "power") == [("shutdown", 1800)] and result.say == "Выключение через 30 минут"


async def test_compound_execution(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run(cmds, "сверни все окна и сделай громче")
    assert result.ok and result.title == "Свернуть все окна и Громче"
    assert result.command_ids == ["builtin.windows.minimize_all", "builtin.volume.up"]
    assert result.message == "Громкость 50%"
    monkeypatch.setattr(ctx.platform, "close_app", lambda name: 0)
    before = len(calls(ctx, "volume_change"))
    result = await run(cmds, "закрой телеграм и сделай громче")
    assert not result.ok and len(result.data["parts"]) == 1 and len(calls(ctx, "volume_change")) == before


async def test_dynamic_handler_gets_source(cmds: Commands) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(slots: dict[str, Any]) -> ExecResult:
        seen.append(slots)
        return ExecResult(True, "Свет", "Включён")

    cmds.register_provider(
        "test",
        lambda: [
            DynamicCommand(
                "test:light", "Включить свет", ["включи свет в {text}"], handler, category="smarthome"
            ),
        ],
    )
    cmds.refresh_dynamic()
    result = await run(cmds, "включи свет в спальне", "voice")
    assert result.ok and result.command_ids == ["test:light"]
    assert seen == [{"text": "спальне", "_source": "voice"}]


async def test_missing_slot_and_disabled(cmds: Commands) -> None:
    result = await cmds.run_command("builtin.volume.set")
    assert not result.ok and "число" in result.message
    result = await cmds.run_command("builtin.volume.set", {"number": 30})
    assert result.ok and result.message == "Громкость 30%"
    cmds.set_enabled("builtin.volume.mute", False)
    result = await cmds.run_command("builtin.volume.mute")
    assert not result.ok and result.message == "Команда выключена"
