"""Живой прогон браузера Jarvis: настоящий Chromium (без окна) по CDP и локальная тестовая страница.

Пропускается, если Chromium нет (в CI его нет; в контейнере разработки — Playwright, /opt/pw-browsers).
Путь можно задать: JARVIS_TEST_CHROMIUM=/path/to/chrome.
"""

import asyncio
import contextlib
import glob
import http.server
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis.ai.browser import launch
from jarvis.ai.browser import page as pages
from jarvis.ai.browser.service import Browser
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply
from tests.ai.test_agent_browser import _answer, _results, act
from tests.ai.test_agent_browser import _id as element_id


def _chromium() -> str:
    found = os.environ.get("JARVIS_TEST_CHROMIUM") or next(
        iter(sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))), ""
    )
    return found or shutil.which("chromium") or shutil.which("google-chrome") or ""


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="нет Chromium для живого прогона браузера Jarvis")

SHOP = """<!doctype html><html><head><meta charset="utf-8"><title>Тестовый магазин</title></head><body>
<header><a href="/">Главная</a> <a href="/cart">Корзина</a></header>
<h1>Наушники</h1><p>Отличные наушники за 1 990 ₽. Доставка завтра.</p>
<form action="/search" method="get"><input type="search" name="q" aria-label="Поиск по магазину">
<button>Найти</button></form>
<form action="/login" method="post"><label>Логин <input name="login"></label>
<label>Пароль <input type="password" name="pw" value="secret123"></label>
<button type="submit">Войти</button></form>
<button onclick="if (confirm('Удалить товар из корзины?')) document.title = 'Удалено'">Удалить</button>
<a href="/get?id=7">Каталог</a>
<div style="height:3000px"></div><p>Подвал страницы</p><button>Внизу</button>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/get":  # отдаёт файл, хотя по ссылке этого не видно
            self._send(
                b"PK\x03\x04" + b"0" * 4_000_000, "application/zip", 'attachment; filename="catalog.zip"'
            )
            return
        if url.path == "/search":
            query = parse_qs(url.query).get("q", [""])[0]
            body = f'<!doctype html><meta charset="utf-8"><title>Поиск</title><h1>Найдено: {query}</h1>'
            self._send(body.encode(), "text/html; charset=utf-8")
            return
        self._send(SHOP.encode(), "text/html; charset=utf-8")

    def _send(self, body: bytes, kind: str, disposition: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # скачивание оборвал браузер
            self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def site() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
async def live(ctx: AppContext) -> Any:
    """Браузер Jarvis — настоящий Chromium без окна, со своим профилем во временной папке данных."""
    ctx.settings.patch({"ai": {"browser": True}})
    browser: Browser = ctx.browser
    processes: list[subprocess.Popen[bytes]] = []

    async def launcher(app: launch.BrowserApp, args: list[str]) -> None:
        command = [
            app.path,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--window-size=1280,900",
            *args,
        ]
        processes.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    browser.launcher = launcher
    browser.find = lambda choice: launch.BrowserApp("chrome", "Chromium", CHROMIUM)
    yield browser
    await browser.disconnect()
    for process in processes:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(5)


async def _front(ctx: AppContext, browser: Browser) -> None:
    """Окно браузера Jarvis — впереди (платформа-заглушка: процесс — настоящего Chromium)."""
    tabs = await browser.tabs()
    title = str(tabs[0].get("title") or "") if tabs else ""
    platform: Any = ctx.platform
    platform.windows = [
        WindowInfo(500, f"{title} - Chromium", "chrome.exe", browser.pid, (0, 0, 1280, 900), foreground=True)
    ]


async def test_live_snapshot_of_a_real_page(ctx: AppContext, live: Browser, site: str) -> None:
    await live.connect()
    assert live.pid > 0 and live.version
    tab = (await live.tabs())[0]
    session = await live.session(str(tab["targetId"]))
    await live.send("Page.navigate", {"url": site + "/"}, session=session)
    for _ in range(50):
        if await live.evaluate(session, "document.readyState") == "complete":
            break
        await asyncio.sleep(0.1)
    tab = next(t for t in await live.tabs() if t["targetId"] == tab["targetId"])
    page = await pages.capture(
        live, str(tab["targetId"]), session, pages.Ids(), url=tab["url"], title=tab["title"]
    )
    rendered = pages.render(page, await live.tabs())
    assert "# Наушники" in rendered and "Отличные наушники за 1 990 ₽. Доставка завтра." in rendered
    assert re.search(r"\[b\d+ поле поиска «Поиск по магазину»\]", rendered)
    assert re.search(r"\[b\d+ поле «Пароль» = ••••, пароль\]", rendered)
    assert "secret123" not in rendered and "secret123" not in "\n".join(pages.parts(page))
    below = next(line for line in rendered.splitlines() if line.startswith("Ниже экрана"))
    assert "кнопка «Внизу»" in below
    login = next(e for e in page.elements.values() if e.name == "Войти")
    assert login.login and login.rect is not None


async def test_live_agent_searches_answers_a_dialog_and_blocks_a_download(
    ctx: AppContext, llm: ScriptedLlm, live: Browser, site: str
) -> None:
    agent: Any = ctx.agent
    agent.ui = FakeUi()
    agent.settle_s = agent.launch_settle_s = agent.pointer_delay_s = 0.0
    await live.connect()
    await _front(ctx, live)
    asked = _answer(ctx)
    llm.add(
        reply('{"steps": ["Открыть магазин", "Найти наушники"]}'),
        calls(("browser_open", {"url": site + "/"})),
        act("browser_click", lambda t: {"id": element_id(t, "Удалить")}),
        calls(("browser_press", {"keys": "enter"})),
        act("browser_click", lambda t: {"id": element_id(t, "Каталог")}),
        act("browser_scroll", lambda t: {"direction": "bottom"}),
        act("browser_click", lambda t: {"id": element_id(t, "Поиск по магазину")}),  # выше экрана: прокрутит
        act(
            "browser_type",
            lambda t: {"id": element_id(t, "Поиск по магазину"), "text": "наушники", "submit": True},
        ),
        calls(("finish", {"status": "done", "summary": "Нашёл."})),
        calls(("finish", {"status": "done", "summary": "Нашёл наушники."})),
    )
    run_id = await agent.run("Найди наушники в тестовом магазине")
    result = await asyncio.wait_for(agent.wait(run_id), 90)
    results = _results(llm)
    if os.environ.get("JARVIS_SHOW"):
        print("\n=====\n".join(results))
    assert result.status == "done", results
    assert "«Тестовый магазин»" in results[0] and "Страница в браузере Jarvis" in results[0]
    # окно сайта: confirm — страница ждёт ответа; enter — OK (с разрешения: «Удалить»)
    assert results[1].startswith("«Удалить»: сайт показал окно"), results[1]
    assert "окно сайта (подтверждение): «Удалить товар из корзины?»" in results[1]
    assert results[2].startswith("Окно сайта: OK") and "«Удалено»" in results[2]
    assert [a[0] for a in asked] == [
        "Нажать «Удалить» на 127.0.0.1?",
        "Согласиться: «Удалить товар из корзины?» на 127.0.0.1?",
    ]
    # ссылка, которая неожиданно скачивает файл, — без разрешения браузер не скачивает
    assert "Сайт начал скачивать «catalog.zip» — Jarvis не дал" in results[3] + results[4], results[3]
    assert results[4].startswith("Прокручено в конец") and "кнопка «Внизу»" in results[4]
    # поиск — настоящими событиями клавиатуры: форма отправлена
    # щелчок по полю (вне экрана — браузер прокрутил к нему): не «закрыт другим элементом», фокус в поле
    assert results[5].startswith("«Поиск по магазину»: нажато"), results[5]
    assert re.search(r"\[b\d+ поле поиска «Поиск по магазину», фокус\]", results[5])
    assert "Найдено: наушники" in results[6], results[6]
    tabs = await live.tabs()
    assert any("/search?q=" in str(t["url"]) for t in tabs)
