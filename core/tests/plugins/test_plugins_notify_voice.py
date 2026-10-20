"""«Озвучка уведомлений»: что и как читается, фильтры программ, тихий режим, доступ (источник — заглушка)."""

import pytest

from jarvis.context import AppContext
from jarvis.plugins import notify_voice as notify_module
from jarvis.plugins.notify_voice import ACCESS_HINT, NotifyVoice, Toast
from tests.plugins.conftest import FakeTts, enable, manager, wait_until


class FakeNotifications:
    def __init__(self, access: str = "allowed") -> None:
        self.access_status = access
        self.items: list[Toast] = []
        self.requested = 0
        self.closed = False

    async def access(self, request: bool = False) -> str:
        self.requested += int(request)
        return self.access_status

    async def fetch(self) -> list[Toast]:
        return list(self.items)

    async def close(self) -> None:
        self.closed = True


def plugin_of(ctx: AppContext) -> NotifyVoice:
    plugin = manager(ctx).get("notify_voice")
    assert isinstance(plugin, NotifyVoice)
    return plugin


def said(ctx: AppContext) -> list[str]:
    tts = ctx.tts
    assert isinstance(tts, FakeTts)
    return tts.said


OLD = Toast(1, "Почта", "Старое письмо", "Давно пришло", created=1.0)
TELEGRAM = Toast(2, "Telegram", "Аня", "Привет! Как дела?", created=2.0)
DISCORD = Toast(3, "Discord", "Сервер", "Новое сообщение", created=3.0)


async def test_reads_only_new_and_allowed(pctx: AppContext) -> None:
    pctx.settings.patch({"plugins": {"notify_voice": {"config": {"apps_block": "discord"}}}})
    plugin = plugin_of(pctx)
    assert await plugin.process([OLD]) == []  # первый проход только запоминает то, что уже было
    spoken = await plugin.process([OLD, DISCORD, TELEGRAM])
    assert spoken == ["Telegram: Аня. Привет! Как дела?"] == said(pctx)
    assert await plugin.process([OLD, DISCORD, TELEGRAM]) == []  # повторно не читаем
    status = plugin.status()
    assert status["read"] == 1 and status["last"]["app"] == "Telegram"


async def test_allow_list_and_formatting(pctx: AppContext) -> None:
    pctx.settings.patch(
        {
            "plugins": {
                "notify_voice": {
                    "config": {"apps_allow": "Telegram, Почта", "read_body": False, "max_chars": 40}
                }
            }
        }
    )
    plugin = plugin_of(pctx)
    await plugin.process([])
    spoken = await plugin.process(
        [
            Toast(10, "Почта", "Почта", "Письмо от банка https://bank.example/x"),
            Toast(11, "Telegram", "Очень длинное имя отправителя сообщения в группе", "текст"),
            Toast(12, "Slack", "Канал", "Сообщение"),
            Toast(13, "Jarvis", "Готово", "Команда выполнена"),
        ]
    )
    assert spoken == ["Почта", "Telegram: Очень длинное имя отправителя…"]


async def test_too_many_are_summarized(pctx: AppContext) -> None:
    plugin = plugin_of(pctx)
    await plugin.process([])
    toasts = [Toast(100 + i, "Telegram", f"Чат {i}", "сообщение", created=float(i)) for i in range(5)]
    spoken = await plugin.process(toasts)
    assert spoken[:3] == [
        "Telegram: Чат 0. сообщение",
        "Telegram: Чат 1. сообщение",
        "Telegram: Чат 2. сообщение",
    ]
    assert spoken[3] == "И ещё 2 уведомления."


@pytest.mark.parametrize("quiet", ["silent_mode", "muted", "fullscreen"])
async def test_quiet_conditions(pctx: AppContext, monkeypatch: pytest.MonkeyPatch, quiet: str) -> None:
    plugin = plugin_of(pctx)
    if quiet == "silent_mode":
        pctx.settings.patch({"voice": {"silent_mode": True}})
    elif quiet == "muted":
        await plugin.handle("mute", {})
    else:
        monkeypatch.setattr(pctx.platform, "is_fullscreen_app", lambda: True)
    await plugin.process([])
    assert await plugin.process([TELEGRAM]) == [] and said(pctx) == []


async def test_polls_source_when_enabled(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify_module, "POLL_S", 0.05)
    source = FakeNotifications()
    source.items = [OLD]
    plugin = plugin_of(pctx)
    plugin.source_factory = lambda: source
    info = await enable(pctx, "notify_voice")
    assert info["status"]["supported"] is True
    await wait_until(lambda: plugin.status()["access"] == "allowed" and plugin._primed)
    assert source.requested == 1
    source.items = [OLD, TELEGRAM]
    await wait_until(lambda: said(pctx) == ["Telegram: Аня. Привет! Как дела?"])
    await manager(pctx).set_enabled("notify_voice", False)
    assert source.closed


async def test_access_denied_shows_hint(pctx: AppContext) -> None:
    source = FakeNotifications(access="denied")
    plugin = plugin_of(pctx)
    plugin.source_factory = lambda: source
    await enable(pctx, "notify_voice")
    await wait_until(lambda: plugin.status()["access"] == "denied")
    assert plugin.status()["hint"] == ACCESS_HINT
    source.access_status = "allowed"
    result = await manager(pctx).action("notify_voice", "request_access", {})
    assert result == {"ok": True, "access": "allowed", "hint": ""}


async def test_unsupported_without_windows(pctx: AppContext) -> None:
    info = await enable(pctx, "notify_voice")
    status = info["status"]
    assert status["running"] is True and "error" not in status
    assert status["supported"] is False and status["access"] == "unsupported" and "Windows" in status["hint"]
    result = await manager(pctx).action("notify_voice", "test", {})
    assert result["text"] == "Telegram: Аня. Привет! Как дела?"


async def test_screenshot_tools_are_never_read(pctx: AppContext) -> None:
    """«Ножницы» показывают уведомление после каждого снимка экрана — его не читаем, даже без фильтров."""
    plugin = plugin_of(pctx)
    await plugin.process([])
    spoken = await plugin.process(
        [
            Toast(20, "Ножницы", "Снимок экрана скопирован в буфер обмена", "Выберите здесь, чтобы изменить"),
            Toast(21, "Snipping Tool", "Screenshot copied to clipboard", "", created=1.0),
            Toast(22, "Набросок на фрагменте экрана", "Фрагмент сохранён в буфер обмена", created=2.0),
            Toast(23, "Xbox Game Bar", "Снимок экрана сохранён", created=3.0),
            Toast(24, "Telegram", "Аня", "Скинь скриншот, пожалуйста", created=4.0),
        ]
    )
    assert spoken == ["Telegram: Аня. Скинь скриншот, пожалуйста"] == said(pctx)
