"""Браузер агента для фоновых задач: настоящий Chrome или Edge без окна, свой профиль, по вкладке на задачу.

Окна на рабочем столе у него нет (--headless=new): человек видит его в Jarvis — панель «Браузер агента»
получает живую картинку вкладки (Page.startScreencast) по WebSocket /ws/agent-browser, а его щелчки,
прокрутка и ввод идут прямо в страницу (Input.dispatch*). «Взять управление» — вкладка на время отдана
человеку: действия задачи в ней ждут, пока он не вернёт её агенту (войти на сайт, пройти капчу, поправить
форму).

Почему не браузер внутри Electron: Google, банки и часть сайтов не пускают «встроенные» браузеры, а здесь —
обычный Chrome со своим профилем (<данные>/agent-browser): человек один раз входит в сайты в панели, и задачи
работают под его аккаунтами. Подпись браузера — как у обычного Chrome (HeadlessChrome заменяется), флаг
автоматизации снят. Профиль отдельный и от обычного браузера человека, и от «Браузера Jarvis» экранного
агента.

Запускается, когда задаче понадобился сайт или человек открыл панель; закрывается вместе с Jarvis и сам —
через IDLE_S без задач и зрителей. Его процесс — наш (нет окна, человек его не закроет).
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis import paths
from jarvis.ai.browser import launch
from jarvis.ai.browser.cdp import CdpClient, CdpError
from jarvis.ai.browser.service import BLANK_URLS, Browser, Status
from jarvis.platform.base import WindowInfo
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

HEADLESS = (
    "--headless=new",
    "--window-size=1280,860",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,MediaRouter",
    "--mute-audio",
    "--no-default-browser-check",
)
IDLE_S = 10 * 60.0  # без задач и зрителей столько — закрыть браузер (память)
FRAME_QUALITY = 70
FRAME_MAX = (1600, 1100)
HOLD_POLL_S = 0.25

FrameSink = Callable[[dict[str, Any]], None]


def profile_dir() -> Path:
    return paths.data_dir() / "agent-browser"


class AgentBrowser(Browser):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.launcher = self._spawn
        self._proc: subprocess.Popen[bytes] | None = None
        self.user_agent = ""  # подпись обычного Chrome вместо HeadlessChrome
        self._ua_sessions: set[str] = set()
        self.task_tabs: dict[str, str] = {}  # задача → её вкладка
        self.held: set[str] = set()  # вкладки, которыми сейчас управляет человек
        self.viewers = 0
        self._used = time.monotonic()
        self._idle: asyncio.Task[None] | None = None
        self._acks: set[asyncio.Future[None]] = set()  # «кадр получен» браузеру — ссылки, пока идут

    # --- что это за браузер ------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        """Есть Chrome или Edge (или браузер уже подключён)."""
        return self.connected or self.find(self._choice()) is not None

    @property
    def profile(self) -> Path:
        return profile_dir()

    def state(self) -> dict[str, Any]:
        app = self.app or self.find(self._choice())
        return {
            "status": self.status,
            "app": app.title if app is not None else "",
            "found": app is not None,
            "version": self.version,
            "error": self.error,
            "tasks": dict(self.task_tabs),
            "held": sorted(self.held),
            "profile": str(self.profile),
        }

    def _set(self, status: Status, error: str = "") -> None:
        self.status, self.error = status, error
        self.ctx.bus.publish("agent_browser.state", self.state())

    async def start(self) -> None:
        self.status = "closed"
        self._idle = asyncio.ensure_future(self._idle_loop())

    async def stop(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        await self.close()

    async def close(self) -> None:
        """Закрыть браузер совсем (окна у него нет — иначе процесс остался бы висеть)."""
        client = self.client
        if client is not None and not client.closed:
            with contextlib.suppress(Exception):
                await client.send("Browser.close", timeout=3.0)
        await self.disconnect(status="closed")
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                await self.ctx.run_io(proc.wait, 3.0)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        self.task_tabs.clear()
        self.held.clear()
        self._ua_sessions.clear()

    def touch(self) -> None:
        self._used = time.monotonic()

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(30.0)
            if not self.connected or self.viewers or self.task_tabs:
                continue
            if time.monotonic() - self._used > IDLE_S:
                log.info("браузер агента без дела %d мин — закрываю", int(IDLE_S // 60))
                with contextlib.suppress(Exception):
                    await self.close()

    # --- запуск ---------------------------------------------------------------------------------------------
    async def _start_browser(self, profile: Path) -> CdpClient:
        app = self.find(self._choice())
        if app is None:
            self._set("error", "Не нашёл Google Chrome или Microsoft Edge — установите один из них")
            raise UserFacingError(self.error)
        self.app = app
        profile.mkdir(parents=True, exist_ok=True)
        self._set("starting")
        log.info("браузер агента: запускаю %s без окна (профиль %s)", app.title, profile)
        args = [*launch.arguments(profile, "about:blank"), *HEADLESS]
        if sys.platform != "win32" and getattr(os, "geteuid", lambda: 1)() == 0:
            args.append("--no-sandbox")  # Linux под root (разработка, контейнер): иначе Chrome не запустится
        await self.launcher(app, args)
        deadline = time.monotonic() + self.port_wait_s
        while time.monotonic() < deadline:
            client = await self._running(profile)
            if client is not None:
                return client
            if self._proc is not None and self._proc.poll() is not None:
                break  # процесс уже закончился — порта не будет
            await asyncio.sleep(0.2)
        self._set(
            "error", f"{app.title} не запустился без окна — попробуйте ещё раз или выберите другой браузер"
        )
        raise UserFacingError(self.error)

    async def _spawn(self, app: launch.BrowserApp, args: list[str]) -> None:
        """Свой процесс браузера (без окна): его не видно, и закрыть его может только Jarvis."""

        def run() -> subprocess.Popen[bytes]:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            return subprocess.Popen(
                [app.path, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )

        self._proc = await self.ctx.run_io(run)

    async def _setup(self, client: CdpClient) -> None:
        self._ua_sessions.clear()
        await super()._setup(client)
        with contextlib.suppress(CdpError, TimeoutError):
            info = await client.send("Browser.getVersion")
            agent = str(info.get("userAgent") or "")
            self.user_agent = agent.replace("HeadlessChrome", "Chrome") if "HeadlessChrome" in agent else ""
        with contextlib.suppress(CdpError, TimeoutError):  # скачивания — только с разрешения (задача спросит)
            await client.send("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
            self.downloads_allowed = False

    def _closed(self) -> None:
        super()._closed()
        if self.client is None:  # браузер закрылся (упал): вкладок задач больше нет
            self.task_tabs.clear()
            self.held.clear()
            self._ua_sessions.clear()

    async def session(self, target: str) -> str:
        session = await super().session(target)
        if self.user_agent and session not in self._ua_sessions:
            self._ua_sessions.add(session)
            with contextlib.suppress(CdpError, TimeoutError):
                await self._client().send(
                    "Emulation.setUserAgentOverride",
                    {"userAgent": self.user_agent},
                    session=session,
                    timeout=3.0,
                )
        return session

    # --- окна у браузера агента нет ----------------------------------------------------------------------
    async def activate(self, target: str, title: str = "") -> None:
        with contextlib.suppress(CdpError, TimeoutError):
            await self._client().send("Target.activateTarget", {"targetId": target})

    async def window(self, title: str = "") -> WindowInfo | None:
        return None

    def owns(self, window: WindowInfo | None) -> bool:
        return False

    # --- вкладки задач --------------------------------------------------------------------------------------
    async def task_tab(self, task_id: str) -> str:
        """Вкладка задачи (нет — новая пустая)."""
        await self.connect(launch=True)
        self.touch()
        tabs = {str(t["targetId"]): t for t in await self.tabs()}
        target = self.task_tabs.get(task_id, "")
        if target in tabs:
            return target
        taken = set(self.task_tabs.values())
        blank = next(
            (
                t
                for t, info in tabs.items()
                if t not in taken and str(info.get("url") or "").startswith(BLANK_URLS)
            ),
            "",
        )
        target = blank or await self.open_tab("about:blank")
        self.task_tabs[task_id] = target
        self.ctx.bus.publish("agent_browser.state", self.state())
        return target

    def drop_task(self, task_id: str) -> str:
        """Забыть вкладку задачи (сразу, без ожидания браузера); вернёт её — закрыть её можно close_target."""
        target = self.task_tabs.pop(task_id, "")
        self.held.discard(target)
        if target:
            self.ctx.bus.publish("agent_browser.state", self.state())
        return target

    async def close_task(self, task_id: str) -> None:
        """Задача закончилась — её вкладка больше не нужна."""
        await self.close_target(self.drop_task(task_id))

    async def close_target(self, target: str) -> None:
        """Закрыть вкладку (последнюю — увести на пустую страницу: иначе браузер закрылся бы)."""
        if not target or not self.connected:
            return
        with contextlib.suppress(Exception):
            if len(await self.tabs()) > 1:
                await self.close_tab(target)
            else:
                session = await self.session(target)
                await self.send("Page.navigate", {"url": "about:blank"}, session=session, timeout=5.0)

    # --- человек в панели
    # --------------------------------------------------------------------------------------
    def hold(self, target: str, on: bool) -> None:
        """«Взять управление» / «Вернуть агенту»."""
        if on:
            self.held.add(target)
        else:
            self.held.discard(target)
        self.ctx.bus.publish("agent_browser.state", self.state())

    async def wait_free(self, target: str, stopping: Callable[[], bool] = lambda: False) -> bool:
        """Вкладкой управляет человек — подождать, пока вернёт. True — ждали (страница могла измениться)."""
        waited = False
        while target in self.held and not stopping():
            waited = True
            await asyncio.sleep(HOLD_POLL_S)
        return waited

    async def watch(self, target: str, sink: FrameSink) -> Callable[[], Any]:
        """Живая картинка вкладки: кадры JPEG (base64) с размером страницы — в sink; вернёт «остановить»."""
        client = await self.connect(launch=True)
        assert client is not None
        session = await self.session(target)
        self.viewers += 1
        self.touch()

        def on_event(method: str, params: dict[str, Any], from_session: str) -> None:
            if method != "Page.screencastFrame" or from_session != session:
                return
            sink({"data": params.get("data") or "", "metadata": params.get("metadata") or {}})
            ack = asyncio.ensure_future(self._ack(session, params.get("sessionId")))
            self._acks.add(ack)
            ack.add_done_callback(self._acks.discard)

        off = client.on(on_event)
        width, height = FRAME_MAX
        with contextlib.suppress(CdpError, TimeoutError):
            await client.send(
                "Page.startScreencast",
                {"format": "jpeg", "quality": FRAME_QUALITY, "maxWidth": width, "maxHeight": height},
                session=session,
            )

        async def stop() -> None:
            off()
            self.viewers = max(0, self.viewers - 1)
            self.touch()
            if not client.closed:
                with contextlib.suppress(Exception):
                    await client.send("Page.stopScreencast", session=session, timeout=3.0)

        return stop

    async def _ack(self, session: str, frame_id: Any) -> None:
        with contextlib.suppress(Exception):
            await self._client().send("Page.screencastFrameAck", {"sessionId": frame_id}, session=session)

    async def human_input(self, target: str, event: dict[str, Any]) -> None:
        """Ввод человека из панели: мышь, колесо, клавиши, текст — в страницу вкладки (координаты — CSS)."""
        session = await self.session(target)
        self.touch()
        kind = str(event.get("type") or "")
        client = self._client()
        if kind == "mouse":
            params = {
                "type": str(event.get("event") or "mouseMoved"),
                "x": float(event.get("x") or 0),
                "y": float(event.get("y") or 0),
                "button": str(event.get("button") or "none"),
                "clickCount": int(event.get("clickCount") or 0),
                "modifiers": int(event.get("modifiers") or 0),
            }
            if params["type"] not in ("mousePressed", "mouseReleased", "mouseMoved"):
                return
            await client.send("Input.dispatchMouseEvent", params, session=session, timeout=5.0)
        elif kind == "wheel":
            await client.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": float(event.get("x") or 0),
                    "y": float(event.get("y") or 0),
                    "deltaX": float(event.get("dx") or 0),
                    "deltaY": float(event.get("dy") or 0),
                },
                session=session,
                timeout=5.0,
            )
        elif kind == "key":
            params = {
                "type": str(event.get("event") or "keyDown"),
                "key": str(event.get("key") or ""),
                "code": str(event.get("code") or ""),
                "windowsVirtualKeyCode": int(event.get("keyCode") or 0),
                "modifiers": int(event.get("modifiers") or 0),
            }
            if params["type"] not in ("keyDown", "keyUp", "rawKeyDown"):
                return
            text = str(event.get("text") or "")
            if text and params["type"] == "keyDown":
                params["text"] = params["unmodifiedText"] = text
            await client.send("Input.dispatchKeyEvent", params, session=session, timeout=5.0)
        elif kind == "text":
            text = str(event.get("text") or "")
            if text:
                await client.send("Input.insertText", {"text": text[:5000]}, session=session, timeout=5.0)
        elif kind == "navigate":
            url = str(event.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                await client.send("Page.navigate", {"url": url}, session=session, timeout=30.0)
        elif kind == "history":
            history = await client.send("Page.getNavigationHistory", session=session)
            entries = history.get("entries") or []
            index = int(history.get("currentIndex") or 0) + (-1 if event.get("back") else 1)
            if 0 <= index < len(entries):
                await client.send(
                    "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}, session=session
                )
        elif kind == "reload":
            await client.send("Page.reload", {}, session=session)
