"""Браузер Jarvis: Chrome или Edge со своим профилем, подключённый по CDP, — для агента и настроек.

Состояние (state(), событие browser.state): off — выключен в настройках (ai.browser); closed — включён, окно
не открыто (откроется само, когда агенту понадобится сайт, или кнопкой «Открыть браузер Jarvis»); starting —
запускается; connected — подключён; error — не вышло (error — что сделать, по-русски). Браузер, открытый
раньше (Jarvis перезапустили), подхватывается по DevToolsActivePort без нового окна. Выключили в настройках
— подключение закрывается, окна браузера человек закрывает сам.

Сессии вкладок — flatten (одно WebSocket-подключение). Окно сайта (alert, confirm, prompt, «Покинуть
страницу?») запоминается по событиям: пока оно открыто, страница не отвечает на команды — снимок и
действия это учитывают. Скачивания: пока работает агент, браузер их не начинает (deny), кроме нажатия,
которое человек разрешил; закончил агент — снова как обычно. Cookies, пароли и хранилище сайтов Jarvis не
читает: таких команд здесь нет.
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from jarvis.ai.browser import launch
from jarvis.ai.browser.cdp import CdpClient, CdpError
from jarvis.ai.format import one_line
from jarvis.platform.base import PlatformError, WindowInfo
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

Status = Literal["off", "closed", "starting", "connected", "error"]
PORT_WAIT_S = 20.0  # браузер запускается и открывает порт отладки
CONNECT_TIMEOUT_S = 3.0
MAX_TABS = 30
FINISHED = ("done", "stopped", "error")
START_PAGE = "data:text/html;charset=utf-8," + quote(
    "<!doctype html><title>Браузер Jarvis</title><body style='font:16px system-ui;max-width:640px;"
    "margin:60px auto;color:#222'><h1>Браузер Jarvis</h1><p>В этом окне агент Jarvis работает с сайтами: "
    "видит страницу целиком и нажимает точнее. Профиль — отдельный, ваш обычный браузер Jarvis не трогает."
    "</p><p>Войдите здесь в сайты, где агент должен работать за вас (почта, магазины, соцсети), — один раз. "
    "Пароли Jarvis не видит и не сохраняет: они остаются в браузере.</p></body>"
)
BLANK_URLS = ("about:blank", "chrome://newtab/", "edge://newtab/", "chrome://new-tab-page/", "data:text/html")

Launcher = Callable[[launch.BrowserApp, list[str]], Awaitable[None]]


class Browser(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.client: CdpClient | None = None
        self.status: Status = "off"
        self.error = ""
        self.app: launch.BrowserApp | None = None
        self.version = ""
        self.pid = 0  # процесс браузера: его окна — окна браузера Jarvis
        self.launcher: Launcher = self._launch  # тесты подставляют свой запуск
        self.find: Callable[[str], launch.BrowserApp | None] = launch.find
        self.port_wait_s = PORT_WAIT_S
        self._lock = asyncio.Lock()
        self._sessions: dict[str, str] = {}  # вкладка (targetId) → сессия
        self._targets: dict[str, str] = {}  # сессия → вкладка
        self._order: dict[str, int] = {}  # вкладка → в каком порядке Jarvis её увидел
        self._seen = itertools.count(1)
        self.dialogs: dict[str, dict[str, Any]] = {}  # сессия → открытое окно сайта (type, message, url)
        self.loading: dict[str, float] = {}  # сессия → когда начала грузиться (ещё грузится)
        self.navigated: dict[str, float] = {}  # сессия → когда сменился документ
        self.notes: list[str] = []  # о скачиваниях — агенту со следующим снимком
        self.downloads_allowed = True
        self._guarding = False  # агент работает в браузере: скачивания — только с разрешения
        self._stuck = False  # порт отладки есть, но браузер не ответил вовремя (занят) — не запускать второй
        self._unsubscribe: list[Callable[[], None]] = []

    # --- состояние -----------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.ctx.settings.value.ai, "browser", False))

    @property
    def connected(self) -> bool:
        return self.client is not None and not self.client.closed

    @property
    def profile(self) -> Path:
        return launch.profile_dir()

    def state(self) -> dict[str, Any]:
        app = self.app or self.find(self._choice())
        return {
            "enabled": self.enabled,
            "status": self.status if self.enabled else "off",
            "app": app.title if app is not None else "",
            "found": app is not None,
            "version": self.version,
            "error": self.error,
            "tabs": len(self._order) if self.connected else 0,
            "profile": str(self.profile),
        }

    def _choice(self) -> str:
        return str(getattr(self.ctx.settings.value.ai, "browser_app", "auto") or "auto")

    def _set(self, status: Status, error: str = "") -> None:
        self.status, self.error = status, error
        self.ctx.bus.publish("browser.state", self.state())

    # --- жизнь сервиса --------------------------------------------------------------------------------
    async def start(self) -> None:
        self._unsubscribe.append(self.ctx.settings.on_change(self._settings_changed))
        self._unsubscribe.append(self.ctx.bus.on("agent.state", self._agent_state))
        if self.enabled:
            self.status = "closed"
            # браузер Jarvis остался открытым с прошлого раза — подхватить, без нового окна
            self.ctx.spawn(self._reconnect(), "browser-reconnect")

    async def stop(self) -> None:
        for off in self._unsubscribe:
            with contextlib.suppress(Exception):
                off()
        self._unsubscribe.clear()
        await self.disconnect(status="closed")

    async def _reconnect(self) -> None:
        with contextlib.suppress(Exception):
            await self.connect(launch=False)

    def _settings_changed(self, old: Any, new: Any) -> None:
        was, now = bool(getattr(old.ai, "browser", False)), bool(getattr(new.ai, "browser", False))
        if was and not now:
            self.ctx.call_soon(lambda: self.disconnect(status="off"))
        elif now and not was:
            self._set("closed")
        elif getattr(old.ai, "browser_app", "") != getattr(new.ai, "browser_app", ""):
            self.app = None
            self.ctx.bus.publish("browser.state", self.state())

    async def _agent_state(self, event: Any) -> None:
        data = getattr(event, "data", None) or {}
        if data.get("status") in FINISHED and self._guarding:
            self._guarding = False
            with contextlib.suppress(Exception):
                await self.allow_downloads(True)

    # --- подключение ---------------------------------------------------------------------------------
    async def connect(self, launch: bool = True) -> CdpClient | None:
        """Подключиться к браузеру Jarvis; нет его — открыть (launch). Не вышло — UserFacingError с тем, что
        делать; launch=False и браузер не открыт — None."""
        async with self._lock:
            if self.client is not None and not self.client.closed:
                return self.client
            if not self.enabled:
                raise UserFacingError(
                    "Браузер Jarvis выключен — включите его: Настройки → Jarvis AI → Агент → «Браузер Jarvis»"
                )
            profile = self.profile
            client = await self._running(profile)
            if client is None and self._stuck:
                self._set("error", "Браузер Jarvis не отвечает — подождите немного или закройте его окно")
                raise UserFacingError(self.error)
            if client is None:
                if not launch:
                    self._set("closed")
                    return None
                client = await self._start_browser(profile)
            try:
                await self._setup(client)
            except Exception as e:
                await client.close()
                self._set("error", "Браузер открылся, но не отвечает — закройте его окно и попробуйте снова")
                raise UserFacingError(self.error) from e
            return client

    async def _running(self, profile: Path) -> CdpClient | None:
        """Подключиться к уже открытому браузеру Jarvis по DevToolsActivePort. Порт не принимает (браузер
        закрыт, файл от прошлого раза) — None; принимает, но браузер не ответил вовремя — None и _stuck:
        открывать второй нельзя (он только передаст адрес первому)."""
        self._stuck = False
        found = launch.read_port(profile)
        if found is None:
            return None
        port, path = found
        try:
            return await CdpClient.connect(f"ws://127.0.0.1:{port}{path}", CONNECT_TIMEOUT_S)
        except TimeoutError:
            log.info("браузер Jarvis на порту %d не ответил вовремя", port)
            self._stuck = True
            return None
        except Exception as e:
            log.info("браузер Jarvis по старому порту не отвечает: %s", e)
            return None

    async def _start_browser(self, profile: Path) -> CdpClient:
        app = self.find(self._choice())
        if app is None:
            wanted = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}.get(self._choice())
            message = (
                f"Не нашёл {wanted} — установите его или выберите другой браузер в настройках"
                if wanted
                else "Не нашёл Google Chrome или Microsoft Edge — установите один из них"
            )
            self._set("error", message)
            raise UserFacingError(message)
        self.app = app
        with contextlib.suppress(OSError):
            profile.mkdir(parents=True, exist_ok=True)
        self._set("starting")
        log.info("браузер Jarvis: запускаю %s (профиль %s)", app.title, profile)
        try:
            await self.launcher(app, launch.arguments(profile, START_PAGE))
        except (PlatformError, OSError) as e:
            self._set("error", f"{app.title} не запустился: {e}")
            raise UserFacingError(self.error) from e
        deadline = time.monotonic() + self.port_wait_s
        while time.monotonic() < deadline:
            client = await self._running(profile)  # старый файл порта браузер перепишет при запуске
            if client is not None:
                return client
            await asyncio.sleep(0.2)
        self._set(
            "error",
            f"{app.title} открылся, но не дал Jarvis подключиться. Если окно браузера Jarvis уже было "
            "открыто — "
            "закройте его и попробуйте снова; на рабочем компьютере подключение может запрещать политика "
            "организации",
        )
        raise UserFacingError(self.error)

    async def _launch(self, app: launch.BrowserApp, args: list[str]) -> None:
        await self.ctx.run_platform(self.ctx.platform.launch, app.path, launch.command_line(args))

    async def _setup(self, client: CdpClient) -> None:
        self._forget_tabs()
        client.on(self._event)
        client.on_close = self._closed
        info = await client.send("Browser.getVersion")
        product = str(info.get("product") or "")
        agent = str(info.get("userAgent") or "")
        if self.app is None:
            app_id = "edge" if "Edg/" in agent else "chrome"
            self.app = launch.BrowserApp(app_id, launch.APPS[app_id][0], "")
        self.version = product.partition("/")[2] or product
        processes = await client.send("SystemInfo.getProcessInfo")
        self.pid = next(
            (int(p.get("id") or 0) for p in processes.get("processInfo", []) if p.get("type") == "browser"), 0
        )
        await client.send("Target.setDiscoverTargets", {"discover": True})
        await client.send("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": True})
        self.downloads_allowed = True
        self.client = client
        log.info("браузер Jarvis подключён: %s %s, процесс %d", self.app.title, self.version, self.pid)
        self._set("connected")

    def _forget_tabs(self) -> None:
        self._sessions.clear()
        self._targets.clear()
        self._order.clear()
        self.dialogs.clear()
        self.loading.clear()
        self.navigated.clear()

    def _closed(self) -> None:
        if self.client is not None and self.client.closed:
            self.client = None
            self.pid = 0
            self._forget_tabs()
            log.info("браузер Jarvis закрыт")
            self._set("closed" if self.enabled else "off")

    async def disconnect(self, status: Status = "closed") -> None:
        client, self.client = self.client, None
        if client is not None:
            client.on_close = None
            if not self.downloads_allowed:  # браузер остаётся открытым — скачивания снова как обычно
                with contextlib.suppress(Exception):
                    await client.send(
                        "Browser.setDownloadBehavior",
                        {"behavior": "default", "eventsEnabled": False},
                        timeout=2.0,
                    )
            await client.close()
        self.downloads_allowed, self._guarding = True, False
        self.pid = 0
        self._forget_tabs()
        self._set(status)

    def _client(self) -> CdpClient:
        client = self.client
        if client is None or client.closed:
            raise UserFacingError("Браузер Jarvis закрыт — открой сайт заново (browser_open)")
        return client

    # --- события ---------------------------------------------------------------------------------------
    def _event(self, method: str, params: dict[str, Any], session: str) -> None:
        if method == "Target.detachedFromTarget":
            gone = str(params.get("sessionId") or "")
            target = self._targets.pop(gone, "")
            if target:
                self._sessions.pop(target, None)
            self.dialogs.pop(gone, None)
            self.loading.pop(gone, None)
        elif method == "Target.targetDestroyed":
            target = str(params.get("targetId") or "")
            self._order.pop(target, None)
            gone = self._sessions.pop(target, "")
            self._targets.pop(gone, None)
            self.dialogs.pop(gone, None)
        elif method == "Page.javascriptDialogOpening":
            self.dialogs[session] = dict(params)
        elif method == "Page.javascriptDialogClosed":
            self.dialogs.pop(session, None)
        elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading", "Page.frameNavigated"):
            frame = params.get("frame") if method == "Page.frameNavigated" else None
            frame_id = str((frame or {}).get("id") or params.get("frameId") or "")
            if not frame_id or frame_id != self._targets.get(session):
                return  # не главный документ вкладки (рамка внутри страницы)
            if method == "Page.frameStartedLoading":
                self.loading[session] = time.monotonic()
            elif method == "Page.frameStoppedLoading":
                self.loading.pop(session, None)
            else:
                self.navigated[session] = time.monotonic()
        elif method == "Browser.downloadWillBegin":
            name = one_line(str(params.get("suggestedFilename") or "файл"), 80)
            if self.downloads_allowed:
                self.notes.append(f"Скачивается «{name}» — в папку загрузок браузера")
            else:
                self.notes.append(
                    f"Сайт начал скачивать «{name}» — Jarvis не дал: скачивание только с разрешения человека"
                )

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    async def allow_downloads(self, allow: bool) -> None:
        """Скачивания: deny — браузер их не начинает (агент работает), default — как обычно."""
        if allow == self.downloads_allowed or not self.connected:
            return
        if not allow:
            self._guarding = True
        await self._client().send(
            "Browser.setDownloadBehavior", {"behavior": "default" if allow else "deny", "eventsEnabled": True}
        )
        self.downloads_allowed = allow

    # --- вкладки -----------------------------------------------------------------------------------------
    async def tabs(self) -> list[dict[str, Any]]:
        """Вкладки (страницы) браузера в порядке, в каком Jarvis их увидел: {targetId, title, url}."""
        found = await self._client().send("Target.getTargets")
        pages = [
            t
            for t in found.get("targetInfos", [])
            if t.get("type") == "page"
            and not str(t.get("url", "")).startswith(("devtools://", "chrome-extension:"))
        ]
        for tab in pages:
            self._order.setdefault(str(tab["targetId"]), next(self._seen))
        alive = {str(t["targetId"]) for t in pages}
        for target in [t for t in self._order if t not in alive]:
            self._order.pop(target, None)
        pages.sort(key=lambda t: self._order.get(str(t["targetId"]), 0))
        return pages[:MAX_TABS]

    async def session(self, target: str) -> str:
        """Сессия вкладки (подключиться при первом обращении)."""
        found = self._sessions.get(target)
        if found:
            return found
        client = self._client()
        reply = await client.send("Target.attachToTarget", {"targetId": target, "flatten": True})
        session = str(reply.get("sessionId") or "")
        if not session:
            raise UserFacingError("Вкладка браузера недоступна")
        self._sessions[target], self._targets[session] = session, target
        with contextlib.suppress(CdpError, TimeoutError):
            await client.send("Page.enable", session=session, timeout=3.0)
        return session

    def target_of(self, session: str) -> str:
        return self._targets.get(session, "")

    async def open_tab(self, url: str) -> str:
        reply = await self._client().send("Target.createTarget", {"url": url})
        target = str(reply.get("targetId") or "")
        if not target:
            raise UserFacingError("Новая вкладка не открылась")
        self._order.setdefault(target, next(self._seen))
        return target

    async def close_tab(self, target: str) -> None:
        await self._client().send("Target.closeTarget", {"targetId": target})

    async def activate(self, target: str, title: str = "") -> None:
        """Вкладку — вперёд, окно браузера — на передний план (Windows не всегда даёт это самому браузеру)."""
        client = self._client()
        await client.send("Target.activateTarget", {"targetId": target})
        session = await self.session(target)
        with contextlib.suppress(CdpError, TimeoutError):
            await client.send("Page.bringToFront", session=session, timeout=3.0)
        window = await self.window(title)
        if window is not None and not window.foreground:
            with contextlib.suppress(PlatformError):
                await self.ctx.run_platform(self.ctx.platform.window_action, "focus", window.hwnd)

    async def window(self, title: str = "") -> WindowInfo | None:
        """Окно браузера Jarvis (по процессу), где эта вкладка впереди (заголовок окна начинается с её
        заголовка); окно одно — оно. Окон несколько и не понять — None: не трогать, чем вывести не то."""
        if not self.pid:
            return None
        try:
            windows = await self.ctx.run_platform(self.ctx.platform.list_windows)
        except PlatformError:
            return None
        mine = [w for w in windows if w.pid == self.pid and w.title]
        want = title.casefold().strip()
        found = next((w for w in mine if want and w.title.casefold().startswith(want)), None)
        return found if found is not None else (mine[0] if len(mine) == 1 else None)

    def owns(self, window: WindowInfo | None) -> bool:
        """Это окно браузера Jarvis (подключённого)."""
        return window is not None and self.connected and bool(self.pid) and window.pid == self.pid

    async def tab_of(self, window: WindowInfo, preferred: str = "") -> dict[str, Any] | None:
        """Вкладка, которая сейчас видна в окне браузера: по заголовку окна («Заголовок — Google Chrome»), а
        если не понять — какая страница видима. preferred — вкладка агента: она, если подходит."""
        tabs = await self.tabs()
        if not tabs:
            return None
        title = window.title.casefold()
        matching = [
            t for t in tabs if str(t.get("title") or "") and title.startswith(str(t["title"]).casefold())
        ]
        chosen = next((t for t in matching if t["targetId"] == preferred), None)
        if chosen is None and len(matching) == 1:
            chosen = matching[0]
        if chosen is None:
            for tab in matching or tabs[:12]:
                with contextlib.suppress(CdpError, TimeoutError, UserFacingError):
                    session = await self.session(str(tab["targetId"]))
                    if (
                        session in self.dialogs
                        or await self.evaluate(session, "document.visibilityState") == "visible"
                    ):
                        chosen = tab
                        break
        if chosen is None:
            chosen = next(
                (t for t in tabs if t["targetId"] == preferred), tabs[0] if len(tabs) == 1 else None
            )
        return chosen

    async def evaluate(self, session: str, expression: str, timeout: float = 5.0) -> Any:
        """Выражение в странице (только свои, заранее написанные, — не от модели)."""
        reply = await self._client().send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            session=session,
            timeout=timeout,
        )
        if reply.get("exceptionDetails"):
            raise CdpError("Runtime.evaluate", "ошибка в странице")
        return (reply.get("result") or {}).get("value")

    async def send(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._client().send(method, params, **kwargs)
