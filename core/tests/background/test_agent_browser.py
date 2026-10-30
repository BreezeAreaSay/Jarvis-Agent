"""Браузер агента фоновых задач: Chrome без окна (поддельный сервер CDP), вкладка задачи, действия browser_*,
разрешения, пароли — только человеку, живая картинка и «Взять управление» в панели."""

import asyncio
import contextlib
import json
import re
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.browser import launch, task_tools
from jarvis.ai.browser.agent_browser import HEADLESS, START_PAGE, WINDOW, AgentBrowser, safe_file_name
from jarvis.background import runner
from jarvis.background.model import Task
from jarvis.context import AppContext
from jarvis.services import UserFacingError
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
        if not ctx.agent_browser.task_tabs:
            break
        time.sleep(0.02)
    assert target not in ctx.agent_browser.held and not ctx.agent_browser.task_tabs


def test_panel_for_task_without_tab_does_not_start_browser(client: TestClient) -> None:
    ctx = client.ctx  # type: ignore[attr-defined]
    launched = use_agent_browser(ctx, FakeChrome(shop()))
    with client.websocket_connect("/ws/agent-browser?token=test-token&task=nope") as ws:
        state = json.loads(ws.receive_text())
        assert state == {"type": "state", "data": {**state["data"], "target": "", "task": "nope"}}
    assert launched == []
    assert client.get("/api/agent-browser").json()["status"] == "closed"


# --- «Открыть окном» ---------------------------------------------------------------------------------------


class FakeProc:
    """Процесс окна браузера: живёт, пока окно не закрыли (Browser.close) или процесс не остановили."""

    def __init__(self, chrome: FakeChrome) -> None:
        self.chrome = chrome
        self.code: int | None = None

    def poll(self) -> int | None:
        return 0 if self.chrome.closed else self.code

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + (timeout or 0)
        while self.poll() is None:
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("chrome", timeout or 0)
            time.sleep(0.02)
        return self.poll() or 0

    def kill(self) -> None:
        self.code = -9

    def terminate(self) -> None:
        self.code = -15


def use_window(ctx: Any, window: FakeChrome) -> list[list[str]]:
    browser: AgentBrowser = ctx.agent_browser
    opened: list[list[str]] = []

    async def launcher(app: launch.BrowserApp, args: list[str]) -> FakeProc:
        opened.append(args)
        await window.start(browser.profile)
        return FakeProc(window)

    browser.window_launcher = launcher
    return opened


async def test_open_window_for_login_and_tasks_wait(
    bg: Bg, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, events = bg
    use_agent_browser(ctx, chrome)
    window = FakeChrome(shop(), pid=5300)
    opened = use_window(ctx, window)
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[assignment]
    task = Task("w1", "x")
    session = runner.Session(service(ctx), task)
    tools = {t.name: t for t in service(ctx).browser_tools().tools()}
    await tools["browser_open"].run(session, {"url": "https://shop.test/cart"})

    await browser.open_window(task="w1")  # та же страница, где задача, — обычным окном
    assert browser.windowed and chrome.closed and browser.task_tabs == {}
    assert opened and "https://shop.test/cart" in opened[0] and set(WINDOW) <= set(opened[0])
    assert "--headless=new" not in opened[0] and "--remote-debugging-port=0" in opened[0]
    assert browser.state()["windowed"] and events.of("agent_browser.state")[-1]["windowed"]
    with pytest.raises(UserFacingError, match="открыт отдельным окном"):
        await browser.connect()

    monkeypatch.setattr(task_tools, "WINDOW_WAIT_S", 0.3)
    with pytest.raises(UserFacingError, match="человек входит на сайт"):
        await tools["browser_read"].run(session, {})
    waiting = asyncio.ensure_future(tools["browser_read"].run(session, {}))
    await asyncio.sleep(0.1)
    window.closed = True  # человек закрыл окно
    await window.stop()
    with pytest.raises(UserFacingError, match="вход уже сделан"):  # вкладка закрылась — открыть заново
        await asyncio.wait_for(waiting, 5)
    await until(lambda: not browser.windowed)
    outcome = await tools["browser_open"].run(session, {"url": "https://shop.test/"})  # снова без окна
    assert outcome.ok and browser.connected and not browser.windowed


async def test_close_window_from_jarvis_and_bad_url(bg: Bg, chrome: FakeChrome) -> None:
    ctx, _, _ = bg
    use_agent_browser(ctx, chrome)
    window = FakeChrome(shop(), pid=5300)
    opened = use_window(ctx, window)
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[assignment]
    await browser.open_window(url="--remote-allow-origins=*")  # не адрес сайта — страница «войдите»
    assert START_PAGE in opened[0] and not any(a.startswith("--remote-allow") for a in opened[0])
    await browser.open_window(url="https://accounts.google.com/")  # уже открыто — второго окна нет
    assert len(opened) == 1
    await browser.close_window()
    assert window.closed and not browser.windowed
    await window.stop()
    await browser.task_tab("after")  # профиль свободен — снова без окна
    assert browser.connected


def test_window_api_and_panel_state(client: TestClient) -> None:
    ctx = client.ctx  # type: ignore[attr-defined]
    use_agent_browser(ctx, FakeChrome(shop()))
    window = FakeChrome(shop(), pid=5300)
    use_window(ctx, window)
    state = client.post("/api/agent-browser/window", json={"url": "https://passport.yandex.ru/"}).json()
    assert state["windowed"] is True
    with client.websocket_connect("/ws/agent-browser?token=test-token") as ws:
        panel = json.loads(ws.receive_text())
        assert panel["type"] == "state" and panel["data"]["windowed"] and panel["data"]["target"] == ""
    assert client.post("/api/agent-browser/window/close").json()["windowed"] is False


async def test_window_waits_for_starting_browser_and_task_never_gets_the_window(
    bg: Bg, chrome: FakeChrome
) -> None:
    ctx, _, _ = bg
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[assignment]
    browser.find = lambda choice: APP

    async def slow_launcher(app: launch.BrowserApp, args: list[str]) -> None:
        await asyncio.sleep(0.3)  # браузер без окна ещё запускается…
        await chrome.start(browser.profile)

    browser.launcher = slow_launcher
    window = FakeChrome(shop(), pid=5300)
    use_window(ctx, window)
    starting = asyncio.ensure_future(browser.task_tab("t1"))
    await asyncio.sleep(0.05)
    await browser.open_window(url="https://accounts.google.com/")  # …а человек уже нажал «Открыть окном»
    with contextlib.suppress(Exception):
        await asyncio.wait_for(starting, 5)
    assert browser.windowed and not browser.connected
    assert window.download_behavior == "default"  # задача не подключилась к окну человека
    assert not any(m == "Target.createTarget" for m, _ in window.commands)
    with pytest.raises(UserFacingError, match="открыт отдельным окном"):
        await browser.task_tab("t2")
    await browser.close_window()
    await window.stop()


async def test_download_with_permission_into_own_folder(bg: Bg, chrome: FakeChrome, data_dir: Path) -> None:
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
        act("browser_click", what="Скачать прайс"),
        calls(("finish", {"status": "done", "summary": "Скачал прайс"})),
    )
    task = service(ctx).create("Скачай прайс с shop.test")
    await until(lambda: task.open_question() is not None, 10)
    question = task.open_question()
    assert question is not None and question.kind == "approval"
    assert question.text == "Скачать файл с shop.test: «Скачать прайс»?"
    assert "без открытия и запуска" in question.detail
    assert chrome.downloads == []  # пока не разрешили — не скачано
    service(ctx).answer(task.id, question.id, "Разрешить")
    await until(lambda: task.status == "done", 10)
    folder = data_dir / "downloads"  # «Загрузки» заглушки на этом ПК нет — своя папка Jarvis
    assert chrome.downloads == [{"name": "price.zip", "state": "completed"}]
    assert f"Скачан файл «price.zip»: {folder / 'price.zip'}" in _tool(script, 2)
    assert chrome.download_behavior == "deny"  # после щелчка — снова без скачиваний
    assert any("Скачан файл «price.zip»" in e.text for e in task.log)
    assert task.approved == ["download:shop.test:/price.zip"]  # разрешена эта ссылка, а не все скачивания


def test_downloaded_names_are_safe_for_windows() -> None:
    assert safe_file_name("get?id=7") == "get_id=7"
    assert safe_file_name("..\\..\\Windows\\evil.exe") == "evil.exe"
    assert safe_file_name("CON.txt") == "_CON.txt" and safe_file_name("  .  ") == "файл"


async def test_download_keeps_files_and_takes_only_its_own_tab(
    bg: Bg, chrome: FakeChrome, tmp_path: Path
) -> None:
    ctx, _, _ = bg
    folder = tmp_path / "Загрузки"
    folder.mkdir()
    use_agent_browser(ctx, chrome)
    browser: AgentBrowser = ctx.agent_browser  # type: ignore[attr-defined]
    mine, other = await browser.task_tab("t1"), await browser.task_tab("t2", fresh=True)
    session = await browser.session(mine)
    (folder / "price.zip").write_bytes(b"old")
    tab, foreign = chrome.tab(mine), chrome.tab(other)
    assert tab is not None and foreign is not None

    async def click() -> None:
        await chrome.start_download(foreign, "https://shop.test/other.zip")  # вкладка другой задачи
        await chrome.start_download(tab, "https://shop.test/price.zip")
        await chrome.start_download(tab, "https://shop.test/price.zip")  # второе подряд — уже не наше

    info = await browser.download(folder, click, session=session)
    assert info is not None and info["state"] == "completed"
    assert info["name"] == "price (2).zip" and info["path"] == str(folder / "price (2).zip")
    assert (folder / "price.zip").read_bytes() == b"old"  # прежний файл не затёрт
    assert (folder / "price (2).zip").read_bytes() == b"PK"
    assert [d["state"] for d in chrome.downloads] == ["canceled", "completed", "canceled"]
    assert sorted(p.name for p in folder.iterdir()) == ["price (2).zip", "price.zip"]  # без файлов-guid
    assert chrome.download_behavior == "deny"
