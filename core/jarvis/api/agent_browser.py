"""Браузер агента (фоновых задач) в окне Jarvis: состояние, закрыть и WebSocket панели «Браузер агента»
(docs/CONTRACTS.md, раздел 8).

WebSocket /ws/agent-browser?token=…&task=… — панель смотрит вкладку задачи (task) или свою вкладку «для входа
на сайты» (без task). От ядра: {"type": "state", data: {target, task, url, title, held, status, error}} и
{"type": "frame", data: {data: JPEG base64, metadata: {deviceWidth, deviceHeight, …}}} — только последние
кадры (медленный клиент пропускает лишние). От панели: {"type": "watch", task?}, {"type": "take"} /
{"type": "release"} («Взять управление» / «Вернуть агенту»), ввод — {"type": "mouse" | "wheel" | "key" |
"text" | "navigate" | "history" | "reload", …} (AgentBrowser.human_input; координаты — CSS-точки страницы).
Панель закрылась — управление само возвращается агенту, своя вкладка для входа закрывается.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from jarvis.ai.browser.agent_browser import AgentBrowser
from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.services import UserFacingError

router = APIRouter()
log = logging.getLogger(__name__)

MANUAL = "manual"  # своя вкладка панели — войти на сайты, когда задач нет
INPUT = frozenset({"mouse", "wheel", "key", "text", "navigate", "history", "reload"})


def _browser(ctx: AppContext) -> AgentBrowser:
    browser = getattr(ctx, "agent_browser", None)
    if not isinstance(browser, AgentBrowser):
        raise HTTPException(503, detail="Браузер агента недоступен")
    return browser


@router.get("/api/agent-browser")
async def agent_browser_state(ctx: Ctx) -> dict[str, Any]:
    return _browser(ctx).state()


@router.post("/api/agent-browser/close")
async def agent_browser_close(ctx: Ctx) -> dict[str, Any]:
    browser = _browser(ctx)
    await browser.close()
    return browser.state()


class Viewer:
    """Одна открытая панель."""

    def __init__(self, ws: WebSocket, browser: AgentBrowser) -> None:
        self.ws = ws
        self.browser = browser
        self.target = ""
        self.task = ""
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)
        self.stop: Callable[[], Awaitable[Any]] | None = None
        self.taken = False
        self.own = f"{MANUAL}:{id(self):x}"  # своя вкладка этой панели (панелей может быть открыто две)
        self._states: set[asyncio.Future[None]] = set()  # «страница сменилась» — состояние панели заново

    async def send(self, kind: str, data: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps({"type": kind, "data": data}, ensure_ascii=False))

    def sink(self, frame: dict[str, Any]) -> None:
        if self.frames.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.frames.get_nowait()  # старый кадр — не нужен
        self.frames.put_nowait(frame)

    async def pump(self) -> None:
        while True:
            frame = await self.frames.get()
            await self.send("frame", frame)

    async def state(self) -> None:
        url = title = ""
        if self.target and self.browser.connected:
            with contextlib.suppress(Exception):
                tab = next((t for t in await self.browser.tabs() if t["targetId"] == self.target), None)
                if tab is not None:
                    url, title = str(tab.get("url") or ""), str(tab.get("title") or "")
        base = self.browser.state()
        await self.send(
            "state",
            {
                "target": self.target,
                "task": self.task,
                "url": url,
                "title": title,
                "held": self.target in self.browser.held,
                "status": base["status"],
                "error": base["error"],
            },
        )

    async def watch(self, task: str) -> None:
        await self.unwatch()
        browser = self.browser
        self.task = task
        if task and task != MANUAL:
            target = browser.task_tabs.get(task, "")
            if not target:  # задача ещё не открывала сайты — смотреть нечего, браузер не запускаем
                self.target = ""
                await self.state()
                return
        else:
            self.task = MANUAL
            target = await browser.task_tab(self.own, fresh=True)
            await browser.start_page(target)
        self.target = target
        self.stop = await browser.watch(target, self.sink, self.changed)
        await self.state()

    def changed(self) -> None:
        """Страница сменилась сама (ссылка, действие агента) — адрес и заголовок в панели заново."""
        work = asyncio.ensure_future(self._state_quietly())
        self._states.add(work)
        work.add_done_callback(self._states.discard)

    async def _state_quietly(self) -> None:
        await asyncio.sleep(0.2)
        with contextlib.suppress(Exception):
            await self.state()

    async def unwatch(self) -> None:
        if self.taken and self.target:
            self.browser.hold(self.target, False)
        self.taken = False
        stop, self.stop = self.stop, None
        if stop is not None:
            with contextlib.suppress(Exception):
                await stop()

    async def handle(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "watch":
            await self.watch(str(message.get("task") or ""))
        elif kind in ("take", "release") and self.target:
            self.taken = kind == "take"
            self.browser.hold(self.target, self.taken)
            await self.state()
        elif kind in INPUT and self.target:
            await self.browser.human_input(self.target, message)


@router.websocket("/ws/agent-browser")
async def agent_browser_ws(ws: WebSocket) -> None:
    if ws.query_params.get("token", "") != ws.app.state.token:
        await ws.close(code=4401)
        return
    ctx = ws.app.state.ctx
    browser = getattr(ctx, "agent_browser", None)
    if not isinstance(browser, AgentBrowser):
        await ws.close(code=4404)
        return
    await ws.accept()
    viewer = Viewer(ws, browser)
    sender = asyncio.ensure_future(viewer.pump())
    try:
        try:
            await viewer.watch(ws.query_params.get("task", ""))
        except UserFacingError as e:
            await viewer.send("error", {"text": str(e)})
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            try:
                await viewer.handle(message)
            except UserFacingError as e:
                await viewer.send("error", {"text": str(e)})
            except Exception as e:
                log.info("панель браузера агента: %s не выполнено: %s", message.get("type"), e)
    except WebSocketDisconnect:
        pass
    finally:
        # сначала — то, что не ждёт браузера (обработчик могут оборвать на любом ожидании)
        sender.cancel()
        own = browser.drop_task(viewer.own)
        await viewer.unwatch()
        with contextlib.suppress(Exception):
            await browser.close_target(own)
