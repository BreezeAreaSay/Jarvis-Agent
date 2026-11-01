"""Окна, скрытые от ИИ: менеджеры паролей, окна ввода пароля и «Не показывать ИИ» — агент их не смотрит и в
них не печатает, весь экран с таким окном не снимается, «что на экране?» отвечает без снимка."""

from typing import Any

import pytest

from jarvis.ai import private_windows, screen
from jarvis.ai.agent import AgentRun
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from jarvis.services import UserFacingError
from tests.ai.fakes import FakeUi


def window(title: str, exe: str, *, minimized: bool = False, foreground: bool = False) -> WindowInfo:
    return WindowInfo(500, title, exe, 9000, (100, 100, 900, 700), minimized=minimized, foreground=foreground)


def test_private_reason() -> None:
    assert private_windows.private_reason(window("Пароли.kdbx - KeePass", "KeePass.exe"))
    assert private_windows.private_reason(
        window("Мой сейф — Bitwarden", "chrome.exe")
    )  # расширение в браузере
    assert private_windows.private_reason(window("Безопасность Windows", "CredentialUIBroker.exe"))
    assert private_windows.private_reason(window("Чат — Telegram", "Telegram.exe"), ["telegram"])
    assert private_windows.private_reason(window("Сбербанк Онлайн — Chrome", "chrome.exe"), ["Сбербанк"])
    assert not private_windows.private_reason(window("Блокнот", "notepad.exe"), ["telegram", "", "  "])
    assert not private_windows.private_reason(None)


def test_visible_private_skips_minimized_and_other_areas() -> None:
    keepass = window("KeePass", "keepass.exe")
    hidden = window("KeePass", "keepass.exe", minimized=True)
    notepad = window("Блокнот", "notepad.exe")
    assert [w for w, _ in private_windows.visible_private([keepass, hidden, notepad])] == [keepass]
    assert private_windows.visible_private([keepass], area=(1000, 0, 1500, 500)) == []
    assert private_windows.visible_private([keepass], area=(800, 600, 1000, 800))


def _run(task: str) -> AgentRun:
    run = AgentRun(id="r", task=task, source="text", chat_id="", turn_id="", reply_id="", speak=False)
    run.vision = True
    return run


@pytest.fixture
def agent(ctx: AppContext) -> Any:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    return agent


async def test_agent_does_not_look_or_type_into_password_manager(ctx: AppContext, agent: Any) -> None:
    platform: Any = ctx.platform
    for w in platform.windows:
        w.foreground = False
    platform.windows.append(window("Пароли.kdbx - KeePass", "KeePass.exe", foreground=True))
    run = _run("Скопируй пароль от почты")
    text, extras = await agent._look(run)
    assert "скрыто от ИИ" in text and extras == [] and run.snapshot is None
    assert agent.ui.snapshots == 0  # элементы окна не читались
    with pytest.raises(UserFacingError, match="скрыто от ИИ"):
        await agent._target(run)


async def test_whole_screen_not_shot_when_private_window_visible(ctx: AppContext, agent: Any) -> None:
    platform: Any = ctx.platform
    platform.windows.append(window("KeePass", "keepass.exe"))  # открыт, но не впереди
    run = _run("Что в браузере?")
    text, _extras = await agent._look(run)
    assert private_windows.SCREEN_NOTE in text  # только окно задачи, без всего экрана
    assert agent.ui.snapshots == 1  # окно задачи (браузер) — как обычно


async def test_screen_question_without_shot_for_private_window(ctx: AppContext) -> None:
    platform: Any = ctx.platform
    for w in platform.windows:
        w.foreground = False
    platform.windows.append(window("1Password", "1Password.exe", foreground=True))
    pictures, note = await screen.capture(ctx)
    assert pictures == [] and "скрытое от ИИ" in note
    ctx.settings.patch({"ai": {"private_apps": ["chrome"]}})
    platform.windows[-1].foreground = False
    chrome = next(w for w in platform.windows if w.exe == "chrome.exe")
    chrome.foreground = True
    pictures, note = await screen.capture(ctx)
    assert pictures == [] and "скрытое от ИИ" in note  # Chrome — в «Не показывать ИИ»
