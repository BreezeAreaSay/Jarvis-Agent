"""Браузер агента фоновых задач: Chrome без окна (поддельный сервер CDP), вкладка задачи, действия browser_*,
разрешения, пароли — только человеку, живая картинка и «Взять управление» в панели."""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.browser import launch
from jarvis.ai.browser.agent_browser import HEADLESS, AgentBrowser
from jarvis.background import runner
from jarvis.background.model import Task
from jarvis.context import AppContext
from tests.ai.cdp_fake import APP, FakeChrome, shop
from tests.ai.fakes import ScriptedLlm, calls
from tests.background.conftest import service, until
from tests.conftest import EventLog

Bg = tuple[AppContext, ScriptedLlm, EventLog]


def use_agent_browser(ctx: Any, chrome: FakeChrome) -> list[list[str]]:
    browser: AgentBrowser = ctx.agent_browser
    launched: list[list[str]] = []

    async def launcher(app: launch.BrowserApp, args: list[str]) -> None:
        launched.append(args)
        await chrome.start(browser.profile)

    browser.launcher = launcher
    browser.find = lambda choice: APP
    return launched


@pytest.fixture
async def chrome(bg: Bg) -> AsyncIterator[FakeChrome]:
    fake = FakeChrome(shop())
    yield fake
    await fake.stop()


def _ids(text: str) -> dict[str, str]:
    """«b4 поле поиска «Поиск по магазину»» → {"Поиск по магазину": "b4"}."""
    return {m.group(2): m.group(1) for m in re.finditer(r"\b(b\d+)\b[^«\n]*«([^»]+)»", text)}


def _tool(script: ScriptedLlm, index: int) -> str:
    return [str(m["content"]) for m in script.requests[index]["messages"] if m.get("role") == "tool"][-1]


async def test_task_browses_in_its_own_headless_tab(bg: Bg, chrome: FakeChrome) -> None:
    ctx, script, _ = bg
    launched = use_agent_browser(ctx, chrome)
    state: dict[str, str] = {}

    def search(request: Any, body: dict[str, Any]) -> Any:
        from tests.ai.fakes import completion

        state.update(_ids(_tool(script, len(script.requests) - 1)))
        import httpx

        return httpx.Response(
            200,
            json=completion(
                calls(
                    ("browser_type", {"id": state["Поиск по магазину"], "text": "наушники", "submit": True})
                )
            ),
        )

    script.add(
        calls(("browser_open", {"url": "https://shop.test/"})),
        search,
        calls(("finish", {"status": "done", "summary": "Нашёл наушники X1"})),
    )
    task = service(ctx).create("Найди наушники в магазине")
    await until(lambda: task.status == "done", 10)

    assert launched and set(HEADLESS) <= set(launched[0])  # без окна, флаг автоматизации снят
    names = {t["function"]["name"] for t in script.requests[0]["tools"]}
    assert {"browser_open", "browser_click", "browser_type", "browser_read"} <= names
    opened = _tool(script, 1)
    assert "Открыто: https://shop.test/" in opened and "Страница" in opened and "Наушники" in opened
    assert "Найдено по запросу «наушники»" in _tool(script, 2)
    assert any(s["url"].startswith("https://shop.test") for s in task.sources)
    # задача закончилась — её вкладка закрыта (последнюю — уведена на пустую страницу)
    assert task.id not in ctx.agent_browser.task_tabs  # type: ignore[attr-defined]


async def test_buying_needs_permission_and_passwords_are_for_humans(bg: Bg, chrome: FakeChrome) -> None:
    ctx, script, _ = bg
    use_agent_browser(ctx, chrome)
    ids: dict[str, str] = {}

    def act(name: str, **extra: Any) -> Any:
        def respond(request: Any, body: dict[str, Any]) -> Any:
            import httpx

            from tests.ai.fakes import completion

            ids.update(_ids(_tool(script, len(script.requests) - 1)))
            return httpx.Response(
                200, json=completion(calls((name, {"id": ids[extra.pop("what")], **extra})))
            )

        return respond

    script.add(
        calls(("browser_open", {"url": "https://shop.test/"})),
        act("browser_type", what="Пароль", text="hunter2"),
        act("browser_click", what="Купить"),
        calls(("finish", {"status": "done", "summary": "Купил"})),
    )
    task = service(ctx).create("Купи наушники")
    await until(lambda: task.open_question() is not None, 10)
    assert "пароли, коды и данные карт задача не вводит" in _tool(script, 2)
    question = task.open_question()
    assert question is not None and question.kind == "approval" and "Купить" in question.text
    assert chrome.tabs[-1].url == "https://shop.test/"  # пока не разрешили — не нажато
    service(ctx).answer(task.id, question.id, "Разрешить")
    await until(lambda: task.status == "done", 10)
    assert any(t.url.endswith("/checkout") for t in chrome.tabs) or "Заказ оформлен" in _tool(script, 3)


async def test_hold_makes_actions_wait_and_reports(bg: Bg, chrome: FakeChrome) -> None:
    ctx, _, _ = bg
    use_agent_browser(ctx, chrome)
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[assignment]
    task = Task("h1", "x")
    session = runner.Session(service(ctx), task)
    tools = {t.name: t for t in service(ctx).browser_tools().tools()}
    target = await browser.task_tab(task.id)
    browser.hold(target, True)
    work = asyncio.ensure_future(tools["browser_open"].run(session, {"url": "https://shop.test/"}))
    await asyncio.sleep(0.4)
    assert not work.done()  # человек управляет вкладкой — задача ждёт
    browser.hold(target, False)
    with pytest.raises(Exception, match="Пока человек управлял страницей"):
        await asyncio.wait_for(work, 5)


async def test_browser_closes_with_jarvis(bg: Bg, chrome: FakeChrome) -> None:
    ctx, _, _ = bg
    use_agent_browser(ctx, chrome)
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[assignment]
    await browser.task_tab("t")
    assert browser.connected
    await browser.close()
    assert chrome.closed and not browser.connected and browser.task_tabs == {}


def test_panel_streams_frames_and_takes_control(client: TestClient, tmp_path: Path) -> None:
    ctx = client.ctx  # type: ignore[attr-defined]
    chrome = FakeChrome(shop())
    use_agent_browser(ctx, chrome)
    with client.websocket_connect("/ws/agent-browser?token=test-token") as ws:
        state = json.loads(ws.receive_text())
        while state["type"] != "state":
            state = json.loads(ws.receive_text())
        assert state["data"]["task"] == "manual" and state["data"]["target"] and not state["data"]["held"]
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "frame" and frame["data"]["metadata"]["deviceWidth"] > 0
        ws.send_text(json.dumps({"type": "take"}))
        taken = json.loads(ws.receive_text())
        while taken["type"] != "state":
            taken = json.loads(ws.receive_text())
        assert taken["data"]["held"] is True
        ws.send_text(json.dumps({"type": "navigate", "url": "https://shop.test/cart"}))
        moved = json.loads(ws.receive_text())
        while moved["type"] != "state":
            moved = json.loads(ws.receive_text())
        assert moved["data"]["url"] == "https://shop.test/cart"
        target = moved["data"]["target"]
    # панель закрылась — управление вернулось агенту, своя вкладка для входа закрыта (сервер доделывает сам)
    for _ in range(100):
        if "manual" not in ctx.agent_browser.task_tabs:
            break
        time.sleep(0.02)
    assert target not in ctx.agent_browser.held and "manual" not in ctx.agent_browser.task_tabs


def test_panel_for_task_without_tab_does_not_start_browser(client: TestClient) -> None:
    ctx = client.ctx  # type: ignore[attr-defined]
    launched = use_agent_browser(ctx, FakeChrome(shop()))
    with client.websocket_connect("/ws/agent-browser?token=test-token&task=nope") as ws:
        state = json.loads(ws.receive_text())
        assert state == {"type": "state", "data": {**state["data"], "target": "", "task": "nope"}}
    assert launched == []
    assert client.get("/api/agent-browser").json()["status"] == "closed"
