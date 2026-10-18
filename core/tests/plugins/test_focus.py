"""«Фокус»: цикл фокус → перерыв → длинный перерыв на поддельных часах, пауза и автопауза по бездействию,
тихий режим и озвучка уведомлений, отвлекающие программы и сайты, статистика и серия дней, голосовые
команды (и через настоящий разбор фраз), действия API."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.desks import layout
from jarvis.platform.base import WindowInfo
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.focus import Focus, app_key, app_matches, find_distraction, left_text, span_text
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import FakeCommands, FakeTts, enable, manager

START = 10_000.0
WALL0 = datetime(2026, 10, 14, 10, 0).timestamp()  # среда, 10:00 по местному времени


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def focus(pctx: AppContext) -> tuple[Focus, Clock]:
    """Плагин с поддельными часами; проверки зовём сами (tick), фоновый цикл (если включат) спит час."""
    plugin = manager(pctx).get("focus")
    assert isinstance(plugin, Focus)
    clock = Clock()
    plugin.clock = clock
    plugin.wall = lambda: WALL0 + clock.now - START
    plugin.tick_s = 3600.0
    return plugin, clock


async def advance(plugin: Focus, clock: Clock, seconds: float) -> None:
    clock.now += seconds
    await plugin.tick()


def silent(ctx: AppContext) -> bool:
    return ctx.settings.value.voice.silent_mode


def tts(ctx: AppContext) -> FakeTts:
    assert isinstance(ctx.tts, FakeTts)
    return ctx.tts


def platform(ctx: AppContext) -> FakePlatform:
    assert isinstance(ctx.platform, FakePlatform)
    return ctx.platform


def show(ctx: AppContext, exe: str, title: str) -> None:
    """Сделать активным окно программы exe с заголовком title."""
    windows = platform(ctx).windows
    for window in windows:
        window.foreground = False
    windows.append(WindowInfo(500 + len(windows), title, exe, foreground=True))


def configure(ctx: AppContext, **config: object) -> None:
    ctx.settings.patch({"plugins": {"focus": {"config": config}}})


# --- цикл --------------------------------------------------------------------------------------------------
async def test_focus_break_and_long_break_cycle(
    pctx: AppContext, focus: tuple[Focus, Clock], pevents: EventLog
) -> None:
    plugin, clock = focus
    earcons: list[str] = []
    pctx.tts.play_earcon = earcons.append  # type: ignore[attr-defined]
    await plugin.handle("start", {})
    state = plugin.snapshot()
    assert state["phase"] == "focus" and state["total_s"] == 1500 and state["cycle"] == 1
    assert state["next"] == "break" and state["ends_at"] == pytest.approx(plugin.wall() + 1500)
    await advance(plugin, clock, 1499)
    assert plugin.snapshot()["phase"] == "focus" and plugin.snapshot()["left_s"] == 1
    for cycle in range(1, 5):
        if cycle > 1:
            await plugin.handle("resume", {})  # «продолжи фокус» после перерыва — следующий фокус серии
            assert plugin.snapshot()["cycle"] == cycle
            assert plugin.snapshot()["next"] == ("long_break" if cycle == 4 else "break")
            await advance(plugin, clock, 1500)
        else:
            await advance(plugin, clock, 1)
        state = plugin.snapshot()
        long = cycle == 4
        assert state["phase"] == "break" and state["long_break"] is long
        assert state["total_s"] == (900 if long else 300)
        await advance(plugin, clock, state["total_s"])
        state = plugin.snapshot()
        assert state["phase"] == "idle" and state["waiting"] is True and state["next"] == "focus"
    said = tts(pctx).said
    assert said.count("Время перерыва, сэр. Вы сосредоточенно работали 25 минут.") == 3
    long_break = "Время длинного перерыва, сэр. Вы сосредоточенно работали 25 минут — отдохните 15 минут."
    assert said.count(long_break) == 1
    assert said.count("Перерыв окончен — возвращаемся, сэр?") == 4
    assert earcons == ["done"] * 8
    alerts = pevents.of("plugin.focus.alert")
    assert [a["kind"] for a in alerts] == ["focus_done", "break_done"] * 4
    assert alerts[0]["title"] == "Время перерыва, сэр" and alerts[-2]["long"] is True
    stats = plugin.stats()
    assert stats["today"]["min"] == 100 and stats["today"]["sessions"] == 4


async def test_auto_continue_starts_next_focus(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    configure(pctx, auto_continue=True, break_min=3)
    await plugin.handle("start", {"minutes": 10})
    await advance(plugin, clock, 600)
    assert plugin.snapshot()["phase"] == "break" and plugin.snapshot()["total_s"] == 180
    assert not silent(pctx)  # на перерыве Jarvis снова говорит
    await advance(plugin, clock, 180)
    state = plugin.snapshot()
    assert state["phase"] == "focus" and state["total_s"] == 1500 and state["cycle"] == 2
    assert silent(pctx)
    assert tts(pctx).said[-1] == "Перерыв окончен, сэр. Возвращаемся к работе: фокус на 25 минут."


async def test_pause_and_resume_keep_time(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    await advance(plugin, clock, 600)
    await plugin.handle("pause", {})
    state = plugin.snapshot()
    assert state["phase"] == "paused" and state["paused_from"] == "focus" and state["reason"] == "user"
    assert state["left_s"] == 900 and state["ends_at"] is None
    await advance(plugin, clock, 3600)  # на паузе время стоит
    assert plugin.snapshot()["left_s"] == 900 and silent(pctx)
    await plugin.handle("toggle", {})  # хоткей снимает с паузы…
    assert plugin.snapshot()["phase"] == "focus"
    await plugin.handle("toggle", {})  # …и ставит снова
    assert plugin.snapshot()["phase"] == "paused"
    await plugin.handle("resume", {})
    await advance(plugin, clock, 899)
    assert plugin.snapshot()["phase"] == "focus"
    await advance(plugin, clock, 1)
    assert plugin.snapshot()["phase"] == "break"
    assert plugin.stats()["today"]["min"] == 25  # пауза не в счёт


async def test_longer_focus_on_the_fly(focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    await advance(plugin, clock, 600)
    result = await plugin.handle("start", {"minutes": 50})  # «фокус на 50 минут», когда он уже идёт
    assert result["result"] == "changed" and plugin.snapshot()["left_s"] == 2400
    assert (await plugin.handle("start", {}))["result"] == "running"
    await plugin.handle("pause", {})
    assert (await plugin.handle("start", {}))["result"] == "resumed"  # «начни фокус» на паузе — продолжить
    result = await plugin.handle("start", {"minutes": 5})  # короче, чем уже отработано, — фокус окончен
    assert result["result"] == "done" and plugin.snapshot()["phase"] == "break"
    assert plugin.stats()["today"]["min"] == 5


async def test_stop_counts_partial_focus(focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    await advance(plugin, clock, 120)
    result = await plugin.handle("stop", {})
    assert result["worked_min"] == 2.0 and plugin.snapshot()["phase"] == "idle"
    today = plugin.stats()["today"]
    assert today["min"] == 2 and today["sessions"] == 0  # меньше 5 минут — не сессия
    await plugin.handle("start", {})
    await advance(plugin, clock, 420)
    await plugin.handle("stop", {})
    today = plugin.stats()["today"]
    assert today["min"] == 9 and today["sessions"] == 1
    with pytest.raises(UserFacingError, match="не запущен"):
        await plugin.handle("stop", {})
    with pytest.raises(UserFacingError, match="не запущен"):
        await plugin.handle("pause", {})
    with pytest.raises(UserFacingError, match="не перерыв"):
        await plugin.handle("skip_break", {})


async def test_break_now_and_skip_break(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    await advance(plugin, clock, 360)
    await plugin.handle("break", {})  # «перерыв» посреди фокуса: 6 минут работы засчитаны сессией
    state = plugin.snapshot()
    assert state["phase"] == "break" and state["total_s"] == 300 and not silent(pctx)
    assert plugin.stats()["today"]["min"] == 6 and plugin.stats()["today"]["sessions"] == 1
    assert (await plugin.handle("break", {}))["result"] == "already"
    await plugin.handle("skip_break", {})
    state = plugin.snapshot()
    assert state["phase"] == "focus" and state["cycle"] == 2 and silent(pctx)


async def test_new_series_after_long_wait(focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    await advance(plugin, clock, 1500)
    await advance(plugin, clock, 300)
    await plugin.handle("start", {})
    assert plugin.snapshot()["cycle"] == 2  # сразу после перерыва — та же серия
    await plugin.handle("break", {})
    await advance(plugin, clock, 300)
    clock.now += 2 * 3600  # вернулись через два часа — серия начинается заново
    await plugin.handle("start", {})
    assert plugin.snapshot()["cycle"] == 1


# --- бездействие ------------------------------------------------------------------------------------------
async def test_idle_pauses_focus_and_welcomes_back(
    pctx: AppContext, focus: tuple[Focus, Clock], pevents: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, clock = focus
    idle = [0.0]
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: idle[0])
    await plugin.handle("start", {})
    await advance(plugin, clock, 300)
    assert plugin.snapshot()["phase"] == "focus"
    idle[0] = 301.0  # человек отошёл 5 минут назад
    await advance(plugin, clock, 300)
    state = plugin.snapshot()
    assert state["phase"] == "paused" and state["reason"] == "idle"
    assert state["left_s"] == 1500 - 299  # время без человека фокусом не считается
    await advance(plugin, clock, 60)  # всё ещё нет
    assert not pevents.of("plugin.focus.alert")
    idle[0] = 1.0  # вернулся
    await advance(plugin, clock, 2)
    alerts = pevents.of("plugin.focus.alert")
    assert [a["kind"] for a in alerts] == ["back"] and alerts[0]["title"] == "С возвращением, сэр"
    welcome = "С возвращением, сэр. Пока вас не было, я поставил фокус на паузу. Продолжим?"
    assert tts(pctx).said[-1] == welcome
    await advance(plugin, clock, 2)
    assert len(pevents.of("plugin.focus.alert")) == 1  # здороваемся один раз
    assert plugin.snapshot()["phase"] == "paused"  # продолжает человек сам
    await plugin.handle("resume", {})
    assert plugin.snapshot()["phase"] == "focus" and plugin.snapshot()["left_s"] == 1201


async def test_idle_pause_can_be_off(
    pctx: AppContext, focus: tuple[Focus, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, clock = focus
    configure(pctx, idle_pause_min=0)
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: 3600.0)
    await plugin.handle("start", {})
    await advance(plugin, clock, 600)
    assert plugin.snapshot()["phase"] == "focus"


async def test_sleep_mid_focus_pauses_instead_of_finishing(
    pctx: AppContext, focus: tuple[Focus, Clock], monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, clock = focus
    idle = [0.0]
    monkeypatch.setattr(pctx.platform, "idle_seconds", lambda: idle[0])
    await plugin.handle("start", {})
    await advance(plugin, clock, 300)
    idle[0] = 7200.0  # компьютер проспал два часа: часы ушли далеко за конец фокуса
    await advance(plugin, clock, 7200)
    state = plugin.snapshot()
    assert state["phase"] == "paused" and state["reason"] == "idle" and state["left_s"] == 1200
    assert plugin.stats()["today"]["sessions"] == 0  # сон фокусом не засчитан


# --- тишина -----------------------------------------------------------------------------------------------
async def test_silent_mode_is_restored(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    assert not silent(pctx)
    await plugin.handle("start", {})
    assert silent(pctx) and pctx.db.kv_get("focus:restore") == {"silent_mode": False}
    await advance(plugin, clock, 1500)  # перерыв — обычный режим
    assert not silent(pctx) and pctx.db.kv_get("focus:restore") is None
    await plugin.handle("skip_break", {})
    assert silent(pctx)
    await plugin.handle("stop", {})
    assert not silent(pctx)


async def test_silent_mode_respects_user_choice(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, _ = focus
    await enable(pctx, "focus")
    pctx.settings.patch({"voice": {"silent_mode": True}})  # человек сам в тихом режиме
    await plugin.handle("start", {})
    await plugin.handle("stop", {})
    assert silent(pctx)  # выключать нечего — режим не наш
    pctx.settings.patch({"voice": {"silent_mode": False}})
    await plugin.handle("start", {})
    pctx.settings.patch({"voice": {"silent_mode": False}})  # человек передумал посреди фокуса…
    pctx.settings.patch({"voice": {"silent_mode": True}})  # …и снова включил
    await plugin.handle("stop", {})
    assert silent(pctx)  # его выбор остаётся
    pctx.settings.patch({"voice": {"silent_mode": False}})
    configure(pctx, silent=False)
    await plugin.handle("start", {})
    assert not silent(pctx)
    await manager(pctx).update("focus", config={"silent": True})  # включили посреди фокуса — сразу
    assert silent(pctx)
    await manager(pctx).update("focus", config={"silent": False})  # и выключили — сразу
    assert not silent(pctx)


async def test_silent_mode_recovered_after_crash(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    pctx.settings.patch({"voice": {"silent_mode": True}})
    pctx.db.kv_set("focus:restore", {"silent_mode": False})  # ядро упало посреди фокуса
    await enable(pctx, "focus")
    assert not silent(pctx) and pctx.db.kv_get("focus:restore") is None


async def test_disable_mid_focus_restores_and_records(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await enable(pctx, "focus")
    await plugin.handle("start", {})
    clock.now += 400
    await manager(pctx).update("focus", enabled=False)
    assert not silent(pctx) and plugin.snapshot()["phase"] == "idle"
    today = plugin.stats()["today"]
    assert (today["min"], today["sessions"]) == (7, 1)  # недоработанный фокус засчитан


async def test_notification_voice_paused_during_focus(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await enable(pctx, "notify_voice")
    notify = manager(pctx).get("notify_voice")
    await plugin.handle("start", {})
    assert notify.status()["muted"] is True
    await advance(plugin, clock, 1500)
    assert notify.status()["muted"] is False  # на перерыве снова читает
    await plugin.handle("skip_break", {})
    assert notify.status()["muted"] is True
    await plugin.handle("stop", {})
    assert notify.status()["muted"] is False
    await manager(pctx).action("notify_voice", "mute", {})  # человек сам выключил озвучку
    await plugin.handle("start", {})
    await plugin.handle("stop", {})
    assert notify.status()["muted"] is True  # «Фокус» её не включает


async def test_hotkey_default_is_saved(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    info = await enable(pctx, "focus")
    assert info["config"]["hotkey"] == "Ctrl+Alt+P"
    assert pctx.settings.value.plugins["focus"].config["hotkey"] == "Ctrl+Alt+P"  # Electron видит сохранённое
    await manager(pctx).update("focus", config={"hotkey": "Ctrl+Alt+F"})
    await manager(pctx).update("focus", enabled=False)
    await manager(pctx).update("focus", enabled=True)
    assert pctx.settings.value.plugins["focus"].config["hotkey"] == "Ctrl+Alt+F"


# --- отвлечения -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("item", "exe", "hit"),
    [
        ("Telegram", "Telegram.exe", True),
        ("Телеграм", r"C:\Users\me\AppData\Roaming\Telegram Desktop\Telegram.exe", True),
        ("Telegram Desktop", "Telegram.exe", True),
        ("steam", "steamwebhelper.exe", True),
        ("Discord", "DiscordPTB.exe", True),
        ("VK", "VK Messenger.exe", True),
        ("VK", "vkteams.exe", False),  # короткое имя — только целиком
        ("Steam", "Code.exe", False),
    ],
)
def test_app_matching(item: str, exe: str, hit: bool) -> None:
    assert app_matches(app_key(item), exe) is hit


def test_find_distraction_sites() -> None:
    sites = ["youtube", "vk.com", "https://www.twitch.tv/"]
    found = find_distraction(WindowInfo(1, "Котики - YouTube — Google Chrome", "chrome.exe"), [], sites)
    assert found is not None and (found.key, found.name, found.icon) == ("site:youtube", "YouTube", "youtube")
    found = find_distraction(WindowInfo(2, "Новости | ВКонтакте — Яндекс Браузер", "browser.exe"), [], sites)
    assert found is not None and found.key == "site:vk.com" and found.name == "VK"
    found = find_distraction(WindowInfo(3, "Стрим — Twitch — Microsoft Edge", "msedge.exe"), [], sites)
    assert found is not None and found.key == "site:twitch.tv"
    assert find_distraction(WindowInfo(4, "youtube.ts — Visual Studio Code", "Code.exe"), [], sites) is None
    vkusvill = WindowInfo(5, "VkusVill — доставка — Google Chrome", "chrome.exe")
    assert find_distraction(vkusvill, [], sites) is None  # «vk» — только отдельным словом


async def test_distracting_app_reminder(
    pctx: AppContext, focus: tuple[Focus, Clock], pevents: EventLog
) -> None:
    plugin, clock = focus
    await plugin.handle("start", {})
    assert await plugin.guard() is None  # Chrome с Jarvis — работа
    show(pctx, "Telegram.exe", "Telegram")
    alert = await plugin.guard()
    assert alert is not None and alert["kind"] == "distraction"
    assert alert["title"] == "Telegram подождёт до перерыва, сэр"
    assert alert["text"] == "До перерыва — 25 минут. Вы справитесь."
    assert (alert["key"], alert["icon"], alert["closed"], alert["site"]) == (
        "app:telegram",
        "telegram",
        False,
        False,
    )
    assert pevents.of("plugin.focus.alert")[-1] == alert
    clock.now += 3
    assert await plugin.guard() is None  # всё ещё в Telegram — не надоедаем
    show(pctx, "Code.exe", "focus.py — Visual Studio Code")
    assert await plugin.guard() is None
    clock.now += 10
    show(pctx, "Telegram.exe", "Telegram")
    assert await plugin.guard() is None  # вернулся почти сразу — молчим
    show(pctx, "Code.exe", "focus.py — Visual Studio Code")
    await plugin.guard()
    clock.now += 60
    show(pctx, "Telegram.exe", "Telegram")
    assert await plugin.guard() is not None  # открыл снова через минуту — напоминаем
    clock.now += 301
    assert await plugin.guard() is not None  # сидит в Telegram — раз в 5 минут
    assert not [c for c in platform(pctx).calls if c[0] == "close_app"]
    await plugin.handle("allow", {"key": "app:telegram"})  # «Нужно по работе»
    clock.now += 400
    assert await plugin.guard() is None
    assert plugin.snapshot()["allowed"] == ["app:telegram"]
    assert plugin.stats()["today"]["distractions"] == 3
    await plugin.handle("stop", {})
    assert plugin.snapshot()["allowed"] == []  # до конца серии


async def test_distracting_app_closed(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    configure(pctx, guard="close", apps="Телеграм, Steam")
    await plugin.handle("start", {})
    show(pctx, "steamwebhelper.exe", "Steam")
    alert = await plugin.guard()
    assert alert is not None and alert["closed"] is True and alert["name"] == "Steam"
    assert alert["text"] == "Я закрыл Steam. До перерыва — 25 минут."
    assert ("close_app", ("steamwebhelper.exe",)) in platform(pctx).calls
    clock.now += 3
    alert = await plugin.guard()  # открыл снова — снова закрываем и говорим об этом
    assert alert is not None and alert["closed"] is True
    show(pctx, "Telegram.exe", "Telegram")
    alert = await plugin.guard()
    assert alert is not None and alert["name"] == "Telegram"
    show(pctx, "chrome.exe", "Котики - YouTube — Google Chrome")
    clock.now += 600
    alert = await plugin.guard()  # сайт браузером не закрываем — только напоминаем
    assert alert is not None and alert["site"] is True and alert["closed"] is False
    assert len([c for c in platform(pctx).calls if c[0] == "close_app"]) == 3
    configure(pctx, guard="off")
    clock.now += 600
    assert await plugin.guard() is None


async def test_guard_only_during_focus(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    show(pctx, "Telegram.exe", "Telegram")
    assert await plugin.guard() is None
    await plugin.handle("start", {"minutes": 5})
    await advance(plugin, clock, 300)  # перерыв — можно и в Telegram
    assert plugin.snapshot()["phase"] == "break"
    assert await plugin.guard() is None


# --- статистика -------------------------------------------------------------------------------------------
async def test_stats_week_streak_and_best_day(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    today = date.fromtimestamp(plugin.wall())

    def day(back: int) -> str:
        return (today - timedelta(days=back)).isoformat()

    pctx.db.kv_set(
        "focus:days",
        {
            day(1): {"min": 50, "sessions": 2},
            day(2): {"min": 130.4, "sessions": 5},
            day(3): {"min": 25, "sessions": 1},
            day(5): {"min": 75, "sessions": 3},
            day(9): {"min": 10, "sessions": 0},
            "мусор": {"min": "много"},
        },
    )
    stats = plugin.stats()
    assert stats["streak"] == 3  # сегодня сессии ещё нет — серия жива со вчера
    assert [d["min"] for d in stats["week"]] == [0, 75, 0, 25, 130, 50, 0]
    assert stats["week"][-1]["date"] == today.isoformat()
    assert stats["best"] == {"date": day(2), "min": 130}
    assert stats["total_sessions"] == 11 and stats["total_min"] == 290
    await plugin.handle("start", {})
    await advance(plugin, clock, 1500)
    stats = plugin.stats()
    assert stats["streak"] == 4
    assert stats["today"] == {"date": today.isoformat(), "min": 25, "sessions": 1, "distractions": 0}
    await plugin.handle("skip_break", {})
    clock.now += 600
    assert plugin.stats()["today"]["min"] == 35  # идущий фокус — тоже сегодня
    assert plugin.snapshot()["today_min"] == 35
    status = plugin.status()
    assert len(status["week"]) == 7 and status["week"][-1]["min"] == 35 and status["streak"] == 4


async def test_state_events(pctx: AppContext, focus: tuple[Focus, Clock], pevents: EventLog) -> None:
    plugin, clock = focus
    await plugin.tick()
    await plugin.handle("start", {})
    states = pevents.of("plugin.focus.state")
    assert {"phase", "left_s", "total_s", "cycle", "today_min", "ends_at", "next"} <= set(states[-1])
    count = len(states)
    await advance(plugin, clock, 30)
    assert len(pevents.of("plugin.focus.state")) == count  # секунды интерфейс считает сам
    await advance(plugin, clock, 31)
    states = pevents.of("plugin.focus.state")
    assert len(states) == count + 1 and states[-1]["today_min"] == 1  # раз в минуту, пока идёт таймер
    await plugin.handle("pause", {})
    count = len(pevents.of("plugin.focus.state"))
    await advance(plugin, clock, 600)
    assert len(pevents.of("plugin.focus.state")) == count  # на паузе — тишина
    clock.now += 86400
    await plugin.tick()  # новый день — «сегодня» с нуля
    assert len(pevents.of("plugin.focus.state")) == count + 1


def test_russian_durations() -> None:
    assert left_text(30) == "меньше минуты"
    assert left_text(60) == "1 минута"
    assert left_text(61) == "2 минуты"
    assert left_text(3900) == "1 час 5 минут"
    assert span_text(21) == "21 минуту"
    assert span_text(75) == "1 час 15 минут"
    assert span_text(25, accusative=False) == "25 минут"


def test_focus_widget_in_desk_catalog() -> None:
    spec = layout.WIDGETS["focus"]
    assert (spec.w, spec.h, spec.min_w, spec.min_h, spec.max_w, spec.max_h) == (1, 2, 1, 1, 2, 2)
    assert spec.category == "time" and spec.name == "Фокус"


# --- голос ------------------------------------------------------------------------------------------------
async def test_voice_commands(pctx: AppContext, focus: tuple[Focus, Clock]) -> None:
    plugin, clock = focus
    await enable(pctx, "focus")
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    voice = {"_source": "voice"}
    said = tts(pctx).said

    result = await commands.by_phrase("начни фокус на {number} минут").handler({**voice, "number": 50})
    assert result.ok and result.title == "Фокус" and plugin.snapshot()["total_s"] == 3000
    # тихий режим включил сам «Фокус» — диспетчер промолчит, поэтому ответ говорит плагин
    assert said[-1] == "Начинаю фокус на 50 минут, сэр. Не буду отвлекать." == result.say
    clock.now += 600
    result = await commands.by_phrase("сколько осталось").handler(voice)
    assert result.message == "До перерыва — 40 минут" and said[-1] == "До перерыва 40 минут, сэр."
    result = await commands.by_phrase("пауза фокуса").handler(voice)
    assert plugin.snapshot()["phase"] == "paused" and said[-1] == "Ставлю на паузу, сэр. Осталось — 40 минут."
    result = await commands.by_phrase("сколько осталось").handler(voice)
    assert result.say == "Фокус на паузе, сэр: до перерыва — 40 минут."
    await commands.by_phrase("продолжи фокус").handler(voice)
    assert plugin.snapshot()["phase"] == "focus"
    result = await commands.by_phrase("начни фокус").handler(voice)
    assert result.say == "Фокус уже идёт, сэр. До перерыва 40 минут."
    spoken = len(said)
    result = await commands.by_phrase("перерыв").handler(voice)
    assert result.say == "Перерыв 5 минут, сэр. Отдохните." and plugin.snapshot()["phase"] == "break"
    assert len(said) == spoken  # тишина снята — ответ скажет диспетчер
    result = await commands.by_phrase("пропусти перерыв").handler(voice)
    assert result.say == "Без перерыва так без перерыва, сэр. Фокус на 25 минут."
    assert plugin.snapshot()["cycle"] == 2
    result = await commands.by_phrase("как я сегодня поработал").handler(voice)
    assert result.say == "Сегодня вы сосредоточенно работали 10 минут, сэр: 1 сессия."
    result = await commands.by_phrase("стоп фокус").handler(voice)
    assert result.say == "Фокус остановлен, сэр. Сегодня — 10 минут сосредоточенной работы."
    result = await commands.by_phrase("стоп фокус").handler(voice)
    assert result.say == "Фокус и так не запущен, сэр."
    result = await commands.by_phrase("начни фокус на {number} минут").handler({**voice, "number": 500})
    assert not result.ok and result.message == "Фокус — от 1 до 240 минут"
    result = await commands.by_phrase("начни фокус на час").handler(voice)
    assert result.ok and plugin.snapshot()["total_s"] == 3600


async def test_stats_phrase_mentions_streak_and_best_day(
    pctx: AppContext, focus: tuple[Focus, Clock]
) -> None:
    plugin, clock = focus
    await enable(pctx, "focus")
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    today = date.fromtimestamp(plugin.wall())
    yesterday = (today - timedelta(days=1)).isoformat()
    pctx.db.kv_set("focus:days", {yesterday: {"min": 40, "sessions": 2}})
    handler = commands.by_phrase("как я сегодня поработал").handler
    assert (await handler({})).say == "Сегодня фокуса ещё не было, сэр. Начнём?"
    await plugin.handle("start", {"minutes": 50})
    await advance(plugin, clock, 3000)
    result = await handler({})
    assert result.say == (
        "Сегодня вы сосредоточенно работали 50 минут, сэр: 1 сессия. "
        "Серия — 2 дня подряд. Это ваш лучший день!"
    )


def test_voice_phrases_reach_focus_commands(client: TestClient) -> None:
    """Настоящий разбор фраз со всеми встроенными командами: фразы «Фокуса» не путаются с чужими."""
    assert client.put("/api/plugins/focus", json={"enabled": True}).status_code == 200
    cases = [
        ("начни фокус", "start", None),
        ("включи режим фокуса", "start", None),
        ("начни фокус на 50 минут", "start_minutes", 50),
        ("начни фокус на пятьдесят минут", "start_minutes", 50),
        ("фокус на 90 минут", "start_minutes", 90),
        ("начни фокус на час", "start_hour", None),
        ("перерыв", "break", None),
        ("пауза фокуса", "pause", None),
        ("продолжи фокус", "resume", None),
        ("стоп фокус", "stop", None),
        ("пропусти перерыв", "skip_break", None),
        ("сколько осталось", "left", None),
        ("как я сегодня поработал", "stats", None),
    ]
    for text, key, number in cases:
        match = client.post("/api/commands/test", json={"text": text}).json()["match"]
        assert match is not None, text
        part = match["parts"][0]
        assert part["command_id"] == f"plugin.focus:{key}", (text, part)
        if number is not None:
            assert part["slots"]["number"] == number
    match = client.post("/api/commands/test", json={"text": "пауза"}).json()["match"]
    assert match is not None and not match["parts"][0]["command_id"].startswith("plugin.focus")
    result = client.post("/api/dispatch", json={"text": "начни фокус на 50 минут", "source": "text"}).json()
    assert result["kind"] == "command" and result["result"]["ok"] is True
    state = client.post("/api/plugins/focus/action", json={"action": "state"}).json()["state"]
    assert state["phase"] == "focus" and state["total_s"] == 3000
    assert client.post("/api/plugins/focus/action", json={"action": "stop"}).status_code == 200


# --- API --------------------------------------------------------------------------------------------------
def test_api_actions(client: TestClient) -> None:
    def act(action: str, **args: object) -> dict:
        r = client.post("/api/plugins/focus/action", json={"action": action, "args": args})
        return {"status": r.status_code, **r.json()}

    assert act("start")["status"] == 400  # плагин выключен
    plugin = client.put("/api/plugins/focus", json={"enabled": True}).json()
    assert plugin["status"]["running"] is True and plugin["status"]["phase"] == "idle"
    assert plugin["config"]["hotkey"] == "Ctrl+Alt+P" and plugin["config"]["focus_min"] == 25
    assert len(plugin["status"]["week"]) == 7 and plugin["actions"] == []
    assert {f["key"] for f in plugin["schema"]} >= {"focus_min", "guard", "apps", "sites", "hotkey"}

    state = act("start", minutes=50)["state"]
    assert state["phase"] == "focus" and state["total_s"] == 3000
    assert client.get("/api/settings").json()["settings"]["voice"]["silent_mode"] is True
    skip = act("skip_break")
    assert skip["status"] == 400 and skip["detail"] == "Сейчас не перерыв"
    assert act("pause")["state"]["phase"] == "paused"
    assert act("toggle")["state"]["phase"] == "focus"
    assert act("break")["state"]["phase"] == "break"
    assert act("skip_break")["state"]["phase"] == "focus"
    stats = act("stats")
    assert stats["ok"] is True and len(stats["week"]) == 7 and stats["today"]["sessions"] == 0
    assert act("allow", key="app:telegram")["state"]["allowed"] == ["app:telegram"]
    assert act("stop")["state"]["phase"] == "idle"
    assert client.get("/api/settings").json()["settings"]["voice"]["silent_mode"] is False

    assert act("start", minutes=0)["detail"] == "Фокус — от 1 до 240 минут"
    assert act("start", minutes="много")["detail"] == "Сколько минут фокуса — нужно число"
    assert act("allow")["status"] == 400
    assert act("fly_away")["status"] == 400
