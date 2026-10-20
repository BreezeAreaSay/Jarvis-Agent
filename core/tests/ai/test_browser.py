"""Браузер Jarvis без настоящего браузера: клиент CDP, поиск Chrome и Edge, подключение со своим профилем,
снимок страницы (дерево доступности + раскладка) — на поддельном CDP (tests/ai/cdp_fake.py)."""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from jarvis.ai.browser import launch
from jarvis.ai.browser import page as pages
from jarvis.ai.browser.cdp import CdpClient, CdpClosed, CdpError
from jarvis.ai.browser.service import Browser
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from jarvis.services import UserFacingError
from tests.ai.cdp_fake import APP, FakeChrome, N, button, heading, link, shop, text, use_fake
from tests.conftest import EventLog


@pytest.fixture
async def chrome(ctx: AppContext) -> Any:
    fake = FakeChrome(shop())
    yield fake
    await ctx.browser.disconnect()
    await fake.stop()


# --- клиент CDP -------------------------------------------------------------------------------------------


async def test_cdp_client_commands_errors_events_and_close(tmp_path: Path) -> None:
    fake = FakeChrome(shop())
    await fake.start(tmp_path)
    port, path = launch.read_port(tmp_path) or (0, "")
    client = await CdpClient.connect(f"ws://127.0.0.1:{port}{path}")
    seen: list[str] = []
    client.on(lambda method, params, session: seen.append(method))
    version = await client.send("Browser.getVersion")
    assert version["product"].startswith("Chrome/")
    with pytest.raises(CdpError) as error:
        await client.send("Nope.method")
    assert error.value.method == "Nope.method" and "нет сессии" in error.value.message
    target = (await client.send("Target.getTargets"))["targetInfos"][0]["targetId"]
    session = (await client.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
    await client.send("Page.navigate", {"url": "https://shop.test/"}, session=session)
    for _ in range(50):
        if "Page.frameStoppedLoading" in seen:
            break
        await asyncio.sleep(0.01)
    assert {"Page.frameStartedLoading", "Page.frameNavigated", "Page.frameStoppedLoading"} <= set(seen)
    closed: list[bool] = []
    client.on_close = lambda: closed.append(True)
    await fake.stop()  # браузер закрыли
    for _ in range(100):
        if client.closed:
            break
        await asyncio.sleep(0.01)
    assert client.closed and closed == [True]
    with pytest.raises(CdpClosed):
        await client.send("Browser.getVersion")


async def test_cdp_timeout_drops_the_late_reply(tmp_path: Path) -> None:
    fake = FakeChrome(shop())
    await fake.start(tmp_path)
    port, path = launch.read_port(tmp_path) or (0, "")
    client = await CdpClient.connect(f"ws://127.0.0.1:{port}{path}")
    target = (await client.send("Target.getTargets"))["targetInfos"][0]["targetId"]
    session = (await client.send("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
    await client.send("Page.navigate", {"url": "https://shop.test/"}, session=session)
    tab = fake.tabs[0]
    await fake.open_dialog(tab, "alert", "Привет", None, tab.root)
    # окно сайта открыто — страница не отвечает: тайм-аут, а поздний ответ потом просто отбрасывается
    with pytest.raises(TimeoutError):
        await client.send("Page.getLayoutMetrics", session=session, timeout=0.2)
    await client.send("Page.handleJavaScriptDialog", {"accept": True}, session=session)
    assert (await client.send("Page.getLayoutMetrics", session=session))["cssVisualViewport"]["pageY"] == 0
    await client.close()
    await fake.stop()


# --- где браузер и как его открыть -----------------------------------------------------------------------


def test_finds_chrome_then_edge_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch.sys, "platform", "win32")
    monkeypatch.setattr(launch, "_app_paths", lambda exe: [])
    program_files = tmp_path / "pf"
    x86 = tmp_path / "pf86"
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("ProgramFiles(x86)", str(x86))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    def install(base: Path, folder: str, exe: str) -> str:
        path = os.path.join(str(base), folder, exe)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("")
        return path

    assert launch.find("auto") is None
    edge = install(x86, r"Microsoft\Edge\Application", "msedge.exe")
    found = launch.find("auto")
    assert found is not None and (found.id, found.title, found.path) == ("edge", "Microsoft Edge", edge)
    chrome = install(program_files, r"Google\Chrome\Application", "chrome.exe")
    assert launch.find("auto") == launch.BrowserApp("chrome", "Google Chrome", chrome)  # Chrome — первым
    assert launch.find("edge") == launch.BrowserApp("edge", "Microsoft Edge", edge)
    assert [a.id for a in launch.installed()] == ["chrome", "edge"]
    # App Paths из реестра — важнее стандартных папок
    custom = install(tmp_path / "D", "Chrome", "chrome.exe")
    monkeypatch.setattr(launch, "_app_paths", lambda exe: [custom] if exe == "chrome.exe" else [])
    assert launch.find("chrome") == launch.BrowserApp("chrome", "Google Chrome", custom)


def test_arguments_profile_port_and_quoting(tmp_path: Path) -> None:
    profile = tmp_path / "Мои данные" / "browser"
    args = launch.arguments(profile, "about:blank")
    assert args == [
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    line = launch.command_line(args)
    assert f'"--user-data-dir={profile}"' in line  # путь с пробелом — в кавычках, одним аргументом
    (tmp_path / launch.PORT_FILE).write_text("9222\n/devtools/browser/abc\n")
    assert launch.read_port(tmp_path) == (9222, "/devtools/browser/abc")
    (tmp_path / launch.PORT_FILE).write_text("9222\n")  # недописан
    assert launch.read_port(tmp_path) is None
    (tmp_path / launch.PORT_FILE).write_text("junk\n/x\n")
    assert launch.read_port(tmp_path) is None


# --- подключение ---------------------------------------------------------------------------------------


async def test_connect_launches_own_profile_and_reuses_running_browser(
    ctx: AppContext, chrome: FakeChrome, events: EventLog
) -> None:
    launched = use_fake(ctx, chrome)
    browser: Browser = ctx.browser
    (browser.profile).mkdir(parents=True, exist_ok=True)
    (browser.profile / launch.PORT_FILE).write_text("1\n/devtools/browser/old\n")  # от прошлого раза
    await browser.connect()
    assert len(launched) == 1
    args = launched[0]
    assert args[0] == f"--user-data-dir={browser.profile}" and "--remote-debugging-port=0" in args
    assert args[-1].startswith("data:text/html")  # страница «Браузер Jarvis»: что это и зачем входить
    state = browser.state()
    assert (state["status"], state["app"], state["version"]) == (
        "connected",
        "Google Chrome",
        "141.0.7390.37",
    )
    assert browser.pid == 5200
    assert [e["status"] for e in events.of("browser.state")][-2:] == ["starting", "connected"]
    assert ("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": True}) in chrome.commands
    await browser.connect()
    assert len(launched) == 1  # уже подключён
    # Jarvis перезапустили, браузер остался открытым — подключение по DevToolsActivePort, без нового окна
    await browser.disconnect()
    assert browser.state()["status"] == "closed"
    client = await browser.connect(launch=False)
    assert client is not None and len(launched) == 1 and browser.connected


async def test_connect_problems_are_explained(
    ctx: AppContext, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser: Browser = ctx.browser
    with pytest.raises(UserFacingError, match="Браузер Jarvis выключен"):
        await browser.connect()
    use_fake(ctx, chrome)
    assert await browser.connect(launch=False) is None  # не открыт — и не открывать
    assert browser.state()["status"] == "closed"
    browser.find = lambda choice: None
    with pytest.raises(UserFacingError, match="Не нашёл Google Chrome или Microsoft Edge"):
        await browser.connect()
    ctx.settings.patch({"ai": {"browser_app": "edge"}})
    with pytest.raises(UserFacingError, match="Не нашёл Microsoft Edge"):
        await browser.connect()
    assert browser.state()["status"] == "error"

    async def silent(app: launch.BrowserApp, args: list[str]) -> None:
        return None  # открылся, но порт так и не появился (окно было открыто без отладки, политика)

    browser.find = lambda choice: APP
    browser.launcher = silent
    browser.port_wait_s = 0.3
    with pytest.raises(UserFacingError, match="закройте его и попробуйте снова"):
        await browser.connect()
    assert browser.state()["error"].startswith("Google Chrome открылся, но не дал Jarvis подключиться")


async def test_busy_browser_is_not_launched_twice(
    ctx: AppContext, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порт принимает, но браузер не отвечает (занят): второй запуск только передал бы адрес первому —
    не запускать, объяснить."""
    from jarvis.ai.browser import service

    launched = use_fake(ctx, chrome)
    browser: Browser = ctx.browser

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    browser.profile.mkdir(parents=True, exist_ok=True)
    (browser.profile / launch.PORT_FILE).write_text(f"{port}\n/devtools/browser/x\n")
    monkeypatch.setattr(service, "CONNECT_TIMEOUT_S", 0.3)
    with pytest.raises(UserFacingError, match="Браузер Jarvis не отвечает"):
        await browser.connect()
    assert launched == [] and launch.read_port(browser.profile) == (port, "/devtools/browser/x")
    server.close()


async def test_window_to_front_only_when_it_is_clear_which(ctx: AppContext, chrome: FakeChrome) -> None:
    use_fake(ctx, chrome)
    browser: Browser = ctx.browser
    await browser.connect()
    platform: Any = ctx.platform
    platform.windows = [
        WindowInfo(301, "Почта - Google Chrome", "chrome.exe", 5200, (0, 0, 800, 600)),
        WindowInfo(302, "Магазин - Google Chrome", "chrome.exe", 5200, (0, 0, 800, 600)),
    ]
    assert (await browser.window("Магазин")).hwnd == 302  # type: ignore[union-attr]
    assert await browser.window("Другое") is None  # два окна и не понять — не трогать
    platform.windows = platform.windows[:1]
    assert (await browser.window("")).hwnd == 301  # type: ignore[union-attr]


async def test_browser_closed_and_turned_off(running_ctx: AppContext) -> None:
    ctx = running_ctx
    chrome = FakeChrome(shop())
    use_fake(ctx, chrome)
    browser: Browser = ctx.browser
    await browser.connect()
    await chrome.stop()  # человек закрыл браузер
    for _ in range(100):
        if browser.status == "closed":
            break
        await asyncio.sleep(0.01)
    assert browser.status == "closed" and not browser.connected and browser.pid == 0
    chrome = FakeChrome(shop())
    use_fake(ctx, chrome)
    await browser.connect()
    ctx.settings.patch({"ai": {"browser": False}})  # выключили в настройках — отключиться
    for _ in range(100):
        if not browser.connected:
            break
        await asyncio.sleep(0.01)
    assert not browser.connected and browser.state()["status"] == "off"
    await chrome.stop()


async def test_api_open_turns_on_and_shows_the_window(ctx: AppContext, chrome: FakeChrome) -> None:
    import httpx

    from jarvis.server import create_app

    use_fake(ctx, chrome)
    ctx.settings.patch({"ai": {"browser": False}})
    app = create_app(ctx, "t")  # без запуска сервисов: маршруты — в том же цикле, что и поддельный браузер
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://core", headers={"X-Jarvis-Token": "t"}
    ) as api:
        assert (await api.get("/api/browser")).json()["status"] == "off"
        state = (await api.post("/api/browser/open", json={"url": "https://shop.test/"})).json()
        assert state["enabled"] is True and state["status"] == "connected"
        assert any(t.url == "https://shop.test/" for t in chrome.tabs)
        assert (await api.post("/api/browser/disconnect")).json()["status"] == "closed"


# --- снимок страницы ---------------------------------------------------------------------------------------


async def _page(ctx: AppContext, chrome: FakeChrome, url: str, ids: pages.Ids | None = None) -> pages.Page:
    browser: Browser = ctx.browser
    await browser.connect()
    target = chrome.tabs[0].target
    session = await browser.session(target)
    await browser.send("Page.navigate", {"url": url}, session=session)
    tab = next(t for t in await browser.tabs() if t["targetId"] == target)
    return await pages.capture(
        browser, target, session, ids or pages.Ids(), url=tab["url"], title=tab["title"]
    )


async def test_page_snapshot_text_elements_and_secrets(ctx: AppContext, chrome: FakeChrome) -> None:
    use_fake(ctx, chrome)
    page = await _page(ctx, chrome, "https://shop.test/")
    rendered = pages.render(page, await ctx.browser.tabs())
    lines = rendered.splitlines()
    assert lines[0] == "Вкладка 1 из 1: «Магазин» — https://shop.test/"
    assert "# Наушники" in lines and "Отличные наушники за 1 990 ₽" in lines
    by_name = {e.name: e for e in page.elements.values()}
    assert by_name["Корзина"].token() == f"{by_name['Корзина'].id} ссылка «Корзина» → /cart"
    assert by_name["Поиск по магазину"].kind == "поле поиска"
    assert by_name["Скачать прайс"].download and "скачивает файл" in by_name["Скачать прайс"].token()
    assert by_name["Цвет"].token().endswith("список «Цвет» = «Красные»")
    assert "не отмечен" in by_name["Подарочная упаковка"].token()
    assert by_name["Купить в один клик"].kind == "элемент"  # div с обработчиком щелчка
    # вход: поле пароля — пароль, кнопка формы — вход; значение пароля не выходит наружу
    password, login = by_name["Пароль"], by_name["Войти"]
    assert password.password and password.login and login.login and by_name["Логин"].login
    chrome.tabs[0].by_backend(password.backend).value = "secret123"  # type: ignore[union-attr]
    again = await _page(ctx, chrome, "https://shop.test/")
    text_all = pages.render(again, []) + "\n".join(pages.parts(again))
    assert "secret123" not in text_all and "пароль" in text_all
    assert "На странице вход (поле пароля)" in text_all
    # текст ссылки — в её названии, а не ещё раз отдельным куском
    assert rendered.count("Корзина") == 1


async def test_ids_are_stable_on_the_same_document_and_new_on_navigation(
    ctx: AppContext, chrome: FakeChrome
) -> None:
    use_fake(ctx, chrome)
    ids = pages.Ids()
    first = await _page(ctx, chrome, "https://shop.test/", ids)
    browser: Browser = ctx.browser
    session = await browser.session(chrome.tabs[0].target)
    # та же страница после прокрутки — те же id
    await browser.send(
        "Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 5, "y": 5, "deltaY": 100}, session=session
    )
    tab = (await browser.tabs())[0]
    same = await pages.capture(browser, tab["targetId"], session, ids, url=tab["url"], title=tab["title"])
    assert {e.backend: e.id for e in first.elements.values()} == {
        e.backend: e.id for e in same.elements.values()
    }
    other = await _page(ctx, chrome, "https://shop.test/cart", ids)
    assert (
        other.title == "Корзина"
        and min(int(e.id[1:]) for e in other.elements.values() or [pages.Element("b1", 0, "", "", "")]) == 1
    )


async def test_long_page_lists_below_and_reads_in_parts(ctx: AppContext, chrome: FakeChrome) -> None:
    use_fake(ctx, chrome)
    page = await _page(ctx, chrome, "https://shop.test/long")
    rendered = pages.render(page, [])
    assert "Экран 1 из" in rendered and "(верх страницы)" in rendered
    below = next(line for line in rendered.splitlines() if line.startswith("Ниже экрана"))
    assert "элементов: " in below and "Товар 60" in below  # всё влезло списком
    assert len(page.elements) == 60  # все ссылки каталога: список не обрезан
    small = pages.parts(page, size=200)
    assert len(small) > 3 and all(len(p) <= 200 for p in small)
    assert "Конец каталога" in small[-1]
    # мало места: дальние элементы — «ещё N»
    budget = pages.BELOW_BUDGET
    try:
        pages.BELOW_BUDGET = 150
        tight = pages.render(page, [])
    finally:
        pages.BELOW_BUDGET = budget
    assert "не показано — browser_scroll или browser_read" in tight


def test_build_from_raw_cdp_answers() -> None:
    """Без сервера: ответы браузера как есть (узлы AX не по порядку документа, рамка без владельца, капча)."""
    ax = [
        {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Вход"}, "backendDOMNodeId": 1,
         "childIds": ["3", "2"]},
        {"nodeId": "2", "parentId": "1", "role": {"value": "Iframe"}, "backendDOMNodeId": 20, "childIds": []},
        {"nodeId": "3", "parentId": "1", "role": {"value": "StaticText"},
         "name": {"value": "Подтвердите, что вы не робот"},
         "backendDOMNodeId": 30},
    ]  # fmt: skip
    snapshot = {
        "strings": ["#document", "IFRAME", "#text", "src", "https://www.google.com/recaptcha/api2/anchor"],
        "documents": [
            {
                "nodes": {
                    "backendNodeId": [1, 20, 30],
                    "parentIndex": [-1, 0, 0],
                    "nodeName": [0, 1, 2],
                    "attributes": [[], [3, 4], []],
                },
                "layout": {
                    "nodeIndex": [1, 2],
                    "bounds": [[0, 100, 300, 80], [0, 10, 200, 20]],
                    "styles": [[], []],
                },
            }
        ],
    }
    metrics = {"cssVisualViewport": {"pageX": 0, "pageY": 0, "clientWidth": 800, "clientHeight": 600}}
    page = pages.build("t", "s", "https://a.test/", "", {"": ax}, {}, snapshot, metrics, None, pages.Ids())
    assert page.title == "Вход" and page.lines[0].text == "Подтвердите, что вы не робот"
    notes = "\n".join(page.notes)
    assert "капча" in notes and "Встроенных окон других сайтов на странице: 1" in notes


async def test_dialog_snapshot_does_not_touch_the_page(ctx: AppContext, chrome: FakeChrome) -> None:
    use_fake(ctx, chrome)
    await _page(ctx, chrome, "https://shop.test/")
    browser: Browser = ctx.browser
    tab = chrome.tabs[0]
    await chrome.open_dialog(tab, "confirm", "Удалить товар из корзины?", None, tab.root)
    session = await browser.session(tab.target)
    for _ in range(50):
        if session in browser.dialogs:
            break
        await asyncio.sleep(0.01)
    before = len(chrome.commands)
    page = await asyncio.wait_for(pages.capture(browser, tab.target, session, pages.Ids()), 2)
    assert len(chrome.commands) == before  # ни одной команды странице — она бы не ответила
    rendered = pages.render(page, [])
    assert "окно сайта (подтверждение): «Удалить товар из корзины?»" in rendered
    assert "enter — OK (согласиться), escape — Отмена" in rendered


async def test_custom_page_covered_and_frames(ctx: AppContext) -> None:
    def sites(url: str) -> Any:
        return (
            "Мини",
            [
                heading("Мини", 2),
                text("Строка"),
                button("Ок"),
                link("Дальше", "/next"),
                N("generic", "", "DIV"),
            ],
        )

    fake = FakeChrome(sites)
    use_fake(ctx, fake)
    page = await _page(ctx, fake, "https://mini.test/")
    assert [line.text.split(" [")[0] for line in page.lines][:2] == ["## Мини", "Строка"]
    await ctx.browser.disconnect()
    await fake.stop()
