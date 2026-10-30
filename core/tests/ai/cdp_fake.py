"""Поддельный Chrome для тестов браузера Jarvis: настоящий сервер CDP (WebSocket, 127.0.0.1) и модель страниц.

Страница — дерево узлов N (роль, название, тег, атрибуты, что делает нажатие и Enter); раскладка — сверху
вниз, по 40 точек на элемент. Сервер отвечает на те команды CDP, что зовёт jarvis.ai.browser (дерево
доступности, DOMSnapshot, попадание в точку, мышь, ввод, клавиши, вкладки, история, окна сайта, скачивания),
и шлёт события. Как настоящий Chrome: пока открыто окно сайта (confirm), команды страницы не отвечают, а
ответ на нажатие, которое его открыло, приходит после закрытия окна.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from jarvis.ai.browser import launch, page, tools

ROW = 40
VIEW_W, VIEW_H = 1200.0, 800.0
# что выполняется и при открытом окне сайта (уровень браузера, не страницы)
BROWSER_LEVEL = ("Target.", "Browser.", "SystemInfo.", "Page.handleJavaScriptDialog", "Page.bringToFront")


@dataclass
class N:
    role: str = "generic"
    name: str = ""
    tag: str = "DIV"
    attrs: dict[str, str] = field(default_factory=dict)
    value: str = ""
    children: list["N"] = field(default_factory=list)
    click: Callable[["FakeChrome", "FakePage", "N"], Any] | None = None
    enter: Callable[["FakeChrome", "FakePage", "N"], Any] | None = None
    options: list[str] = field(default_factory=list)
    overlay: bool = False  # лежит поверх остального (баннер cookies)
    rect: tuple[float, float, float, float] | None = None  # задано руками; иначе — по раскладке
    clickable: bool = False
    pointer: bool = False
    checked: bool = False
    backend: int = 0
    focusable: bool = False

    def walk(self) -> list["N"]:
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


def text(value: str) -> N:
    return N("StaticText", value, "#text")


def heading(value: str, level: int = 1) -> N:
    return N("heading", value, f"H{level}", {"level": str(level)}, children=[text(value)])


def link(name: str, href: str, **kw: Any) -> N:
    attrs = {"href": href, **kw.pop("attrs", {})}
    return N("link", name, "A", attrs, children=[text(name)], focusable=True, **kw)


def button(name: str, **kw: Any) -> N:
    return N("button", name, "BUTTON", kw.pop("attrs", {}), children=[text(name)], focusable=True, **kw)


def field_(name: str, *, role: str = "textbox", kind: str = "text", **kw: Any) -> N:
    attrs = {"type": kind, **kw.pop("attrs", {})}
    return N(role, name, "INPUT", attrs, focusable=True, **kw)


def select(name: str, options: list[str]) -> N:
    return N("combobox", name, "SELECT", {}, value=options[0], options=list(options), focusable=True)


def form(*children: N, **kw: Any) -> N:
    return N("form", "", "FORM", {}, children=list(children), **kw)


def chooser() -> Callable[["FakeChrome", "FakePage", N], Any]:
    """Кнопка, которая открывает выбор файла («Добавить фото» — не поле type=file)."""

    async def run(chrome: "FakeChrome", tab: "FakePage", node: N) -> None:
        await chrome.open_chooser(tab, node)

    return run


def go(url: str) -> Callable[["FakeChrome", "FakePage", N], Any]:
    async def run(chrome: "FakeChrome", tab: "FakePage", node: N) -> None:
        await chrome.navigate(tab, url.format(value=node.value))

    return run


def confirm(
    message: str, then: Callable[["FakeChrome", "FakePage", N], Any] | None = None
) -> Callable[..., Any]:
    async def run(chrome: "FakeChrome", tab: "FakePage", node: N) -> None:
        await chrome.open_dialog(tab, "confirm", message, then, node)

    return run


@dataclass
class FakePage:
    target: str
    url: str = "about:blank"
    title: str = ""
    root: N = field(default_factory=lambda: N("RootWebArea"))
    scroll: float = 0.0
    history: list[tuple[str, str]] = field(default_factory=list)
    index: int = -1
    focused: int = 0
    selected_all: bool = False
    dialog: dict[str, Any] | None = None
    after_dialog: Any = None
    blocked: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)  # (ws, сообщение) — ждут окна
    held: list[tuple[Any, int]] = field(default_factory=list)  # ответы, отложенные до закрытия окна
    loads: int = 0  # сколько раз загружалась
    intercept_files: bool = False  # Page.setInterceptFileChooserDialog
    user_agent: str = ""  # Emulation.setUserAgentOverride
    screencast: bool = False  # Page.startScreencast

    def nodes(self) -> list[N]:
        return self.root.walk()

    def by_backend(self, backend: int) -> N | None:
        return next((n for n in self.nodes() if n.backend == backend), None)

    @property
    def height(self) -> float:
        bottoms = [n.rect[1] + n.rect[3] for n in self.nodes() if n.rect is not None and not n.overlay]
        return max([VIEW_H, *[b + ROW for b in bottoms]])


Site = Callable[[str], tuple[str, list[N]] | None]


class FakeChrome:
    """Сервер CDP. sites(url) → (заголовок, узлы) или None (адрес не найден)."""

    def __init__(self, sites: Site, *, pid: int = 5200) -> None:
        self.sites = sites
        self.pid = pid
        self.tabs: list[FakePage] = []
        self.sessions: dict[str, str] = {}  # сессия → вкладка
        self.active = ""
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.download_behavior = "default"
        self.download_path = ""
        self.downloads: list[dict[str, Any]] = []
        self.canceled: set[str] = set()  # Browser.cancelDownload
        self.file_choosers: list[str] = []  # окна выбора файла, которые открылись у человека (не перехвачены)
        self.clicked: list[str] = []
        self.closed = False  # Browser.close
        self.port = 0
        self._server: Any = None
        self._sockets: set[Any] = set()
        self._next_backend = 1

    # --- сервер -------------------------------------------------------------------------------------------
    async def start(self, profile: Path | None = None) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(self._serve, "127.0.0.1", 0, max_size=None)
        self.port = int(next(iter(self._server.sockets)).getsockname()[1])
        if profile is not None:
            profile.mkdir(parents=True, exist_ok=True)
            (profile / launch.PORT_FILE).write_text(
                f"{self.port}\n/devtools/browser/{uuid.uuid4()}\n", "utf-8"
            )
        if not self.tabs:
            self.tabs.append(FakePage(self._id()))
            self.active = self.tabs[0].target

    async def stop(self) -> None:
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _serve(self, ws: Any) -> None:
        self._sockets.add(ws)
        try:
            async for raw in ws:
                message = json.loads(raw)
                await self._handle(ws, message)
        except Exception:
            pass
        finally:
            self._sockets.discard(ws)

    async def emit(self, method: str, params: dict[str, Any], session: str = "") -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session:
            message["sessionId"] = session
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(message, ensure_ascii=False))

    def _session_of(self, target: str) -> str:
        return next((s for s, t in self.sessions.items() if t == target), "")

    async def _handle(self, ws: Any, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        params = message.get("params") or {}
        session = str(message.get("sessionId") or "")
        tab = self.tab(self.sessions.get(session, "")) if session else None
        self.commands.append((method, params))
        if tab is not None and tab.dialog is not None and not method.startswith(BROWSER_LEVEL):
            tab.blocked.append((ws, message))  # страница «висит», пока открыто окно сайта
            return
        held = False
        try:
            result = await self._run(method, params, session, tab)
            if result is HOLD:
                held, result = True, {}
        except CdpFail as e:
            await ws.send(json.dumps({"id": message["id"], "error": {"code": -32000, "message": str(e)}}))
            return
        if held and tab is not None:
            tab.held.append((ws, message["id"]))
            return
        await ws.send(json.dumps({"id": message["id"], "result": result}, ensure_ascii=False))

    def tab(self, target: str) -> FakePage | None:
        return next((t for t in self.tabs if t.target == target), None)

    def _id(self) -> str:
        return uuid.uuid4().hex.upper()

    # --- страницы ------------------------------------------------------------------------------------------
    async def navigate(self, tab: FakePage, url: str, *, history: bool = True) -> str:
        found = self.sites(url) if url != "about:blank" else ("", [])
        if found is None:
            return "net::ERR_NAME_NOT_RESOLVED"
        session = self._session_of(tab.target)
        await self.emit("Page.frameStartedLoading", {"frameId": tab.target}, session)
        title, nodes = found
        tab.url, tab.title, tab.scroll, tab.focused, tab.selected_all = url, title, 0.0, 0, False
        tab.root = N("RootWebArea", title, "#document", children=nodes)
        tab.loads += 1
        self._layout(tab)
        if history:
            tab.history = [*tab.history[: tab.index + 1], (url, title)]
            tab.index = len(tab.history) - 1
        await self.emit("Page.frameNavigated", {"frame": {"id": tab.target, "url": url}}, session)
        await self.emit("Page.frameStoppedLoading", {"frameId": tab.target}, session)
        return ""

    def _layout(self, tab: FakePage) -> None:
        y = 10.0

        def place(node: N, depth: int) -> None:
            nonlocal y
            self._next_backend += 1
            node.backend = self._next_backend
            if node.role == "StaticText":
                return
            leaf = node.role not in ("generic", "form", "RootWebArea") or not node.children
            if node.rect is None and leaf and not node.overlay and node.role != "RootWebArea":
                node.rect = (10.0 + depth * 10, y, 300.0, 30.0)
                y += ROW
            for child in node.children:
                place(child, depth + 1)
            if node.rect is None and node.children and node.role != "RootWebArea":
                rects = [c.rect for c in node.children if c.rect is not None]
                if rects:
                    top = min(r[1] for r in rects)
                    bottom = max(r[1] + r[3] for r in rects)
                    node.rect = (10.0 + depth * 10, top, 320.0, bottom - top)

        place(tab.root, 0)
        for node in tab.nodes():  # текст — там же, где его элемент
            for child in node.children:
                if child.role == "StaticText" and child.rect is None and node.rect is not None:
                    child.rect = node.rect

    async def open_dialog(self, tab: FakePage, kind: str, message: str, then: Any, node: N) -> None:
        tab.dialog = {"type": kind, "message": message, "url": tab.url}
        tab.after_dialog = (then, node)
        await self.emit("Page.javascriptDialogOpening", dict(tab.dialog), self._session_of(tab.target))

    async def close_dialog(self, tab: FakePage, accept: bool) -> None:
        then, node = tab.after_dialog or (None, None)
        tab.dialog, tab.after_dialog = None, None
        await self.emit("Page.javascriptDialogClosed", {"result": accept}, self._session_of(tab.target))
        if accept and then is not None:
            await then(self, tab, node)
        held, tab.held = tab.held, []
        for ws, message_id in held:
            await ws.send(json.dumps({"id": message_id, "result": {}}))
        blocked, tab.blocked = tab.blocked, []
        for ws, message in blocked:
            await self._handle(ws, message)

    def viewport_rect(self, tab: FakePage, node: N) -> tuple[float, float, float, float] | None:
        if node.rect is None:
            return None
        x, y, w, h = node.rect
        return (x, y if node.overlay else y - tab.scroll, w, h)

    def hit(self, tab: FakePage, x: float, y: float) -> N | None:
        def inside(node: N) -> bool:
            rect = self.viewport_rect(tab, node)
            return (
                rect is not None and rect[0] <= x <= rect[0] + rect[2] and rect[1] <= y <= rect[1] + rect[3]
            )

        nodes = [n for n in tab.nodes() if n.role not in ("StaticText", "RootWebArea")]
        overlays = [n for n in nodes if n.overlay or any(n in o.walk() for o in nodes if o.overlay)]
        for group in (overlays, [n for n in nodes if n not in overlays]):
            found = [n for n in group if inside(n)]
            if found:
                return found[-1]  # самый глубокий
        return None

    async def open_chooser(self, tab: FakePage, node: N) -> None:
        """Сайт открывает выбор файла: перехвачен — событие Jarvis, нет — окно открылось."""
        if tab.intercept_files:
            await self.emit(
                "Page.fileChooserOpened",
                {"frameId": tab.target, "mode": "selectSingle", "backendNodeId": node.backend},
                self._session_of(tab.target),
            )
        else:
            self.file_choosers.append(node.name)

    async def click(self, tab: FakePage, node: N) -> Any:
        self.clicked.append(node.name)
        if node.tag == "INPUT" and node.attrs.get("type") == "file":
            await self.open_chooser(tab, node)
            return None
        if node.focusable:
            tab.focused = node.backend
        if node.role == "checkbox":
            node.checked = not node.checked
        if node.click is not None:
            result = node.click(self, tab, node)
            if asyncio.iscoroutine(result):
                await result
            return HOLD if tab.dialog is not None else None
        if node.tag == "A":
            href = node.attrs.get("href", "")
            if "download" in node.attrs or href.endswith(".zip") or "data-file" in node.attrs:
                await self.start_download(tab, href)
                return None
            await self.navigate(tab, urljoin(tab.url, href))
        return None

    async def start_download(self, tab: FakePage, href: str, *, frame: str = "") -> str:
        """Скачивание, начатое страницей (ссылкой или сама): allowAndName — файл <папка>/<guid>, как у
        Chrome."""
        guid = self._id()
        name = href.rsplit("/", 1)[-1]
        state = "canceled" if self.download_behavior == "deny" else "completed"
        self.downloads.append({"name": name, "state": state})
        await self.emit(
            "Browser.downloadWillBegin",
            {"guid": guid, "url": href, "suggestedFilename": name, "frameId": frame or tab.target},
        )
        for _ in range(10):  # Jarvis успевает отменить чужое скачивание до конца
            if guid in self.canceled or self.download_behavior in ("deny", "default"):
                break
            await asyncio.sleep(0.005)
        if guid in self.canceled:
            state = "canceled"
            self.downloads[-1]["state"] = state
        if state == "completed" and self.download_behavior == "allowAndName" and self.download_path:
            Path(self.download_path, guid).write_bytes(b"PK")
        await self.emit("Browser.downloadProgress", {"guid": guid, "state": state})
        return guid

    # --- команды --------------------------------------------------------------------------------------------
    async def _run(self, method: str, p: dict[str, Any], session: str, tab: FakePage | None) -> Any:
        if method == "Browser.getVersion":
            return {
                "product": "Chrome/141.0.7390.37",
                "userAgent": "Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36",
            }
        if method == "SystemInfo.getProcessInfo":
            return {"processInfo": [{"type": "browser", "id": self.pid}, {"type": "renderer", "id": 1}]}
        if method == "Browser.setDownloadBehavior":
            self.download_behavior = p.get("behavior", "default")
            self.download_path = str(p.get("downloadPath") or "")
            return {}
        if method == "Browser.cancelDownload":
            self.canceled.add(str(p.get("guid") or ""))
            return {}
        if method in ("Target.setDiscoverTargets", "Page.enable", "Page.bringToFront"):
            return {}
        if method == "Browser.close":
            self.closed = True
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [self._info(t) for t in self.tabs]}
        if method == "Target.getTargetInfo":
            found = self.tab(p.get("targetId", ""))
            if found is None:
                raise CdpFail("No target with given id found")
            return {"targetInfo": self._info(found)}
        if method == "Target.attachToTarget":
            if self.tab(p.get("targetId", "")) is None:
                raise CdpFail("No target with given id found")
            new = self._id()
            self.sessions[new] = p["targetId"]
            return {"sessionId": new}
        if method == "Target.createTarget":
            created = FakePage(self._id())
            self.tabs.append(created)
            self.active = created.target
            error = await self.navigate(created, p.get("url", "about:blank"))
            if error:
                created.url = p.get("url", "")
            return {"targetId": created.target}
        if method == "Target.activateTarget":
            self.active = p.get("targetId", "")
            return {}
        if method == "Target.closeTarget":
            closing = self.tab(p.get("targetId", ""))
            if closing is not None:
                self.tabs.remove(closing)
                gone = self._session_of(closing.target)
                await self.emit("Target.targetDestroyed", {"targetId": closing.target})
                if gone:
                    await self.emit("Target.detachedFromTarget", {"sessionId": gone})
                self.active = self.tabs[-1].target if self.tabs else ""
            return {"success": True}
        if tab is None:
            raise CdpFail(f"{method}: нет сессии")
        return await self._page_command(method, p, tab)

    def _info(self, tab: FakePage) -> dict[str, Any]:
        return {"targetId": tab.target, "type": "page", "title": tab.title, "url": tab.url, "attached": True}

    async def _page_command(self, method: str, p: dict[str, Any], tab: FakePage) -> Any:
        if method == "Page.navigate":
            error = await self.navigate(tab, p.get("url", ""))
            return {"frameId": tab.target, **({"errorText": error} if error else {})}
        if method == "Page.reload":
            await self.navigate(tab, tab.url, history=False)
            return {}
        if method == "Page.getNavigationHistory":
            entries = [{"id": i + 1, "url": u, "title": t} for i, (u, t) in enumerate(tab.history)]
            return {"currentIndex": tab.index, "entries": entries}
        if method == "Page.navigateToHistoryEntry":
            tab.index = int(p["entryId"]) - 1
            await self.navigate(tab, tab.history[tab.index][0], history=False)
            return {}
        if method == "Page.handleJavaScriptDialog":
            if tab.dialog is None:
                raise CdpFail("No dialog is showing")
            await self.close_dialog(tab, bool(p.get("accept")))
            return {}
        if method == "Page.setInterceptFileChooserDialog":
            tab.intercept_files = bool(p.get("enabled"))
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": tab.target, "url": tab.url}, "childFrames": []}}
        if method == "Page.getLayoutMetrics":
            visual = {"pageX": 0, "pageY": tab.scroll, "clientWidth": VIEW_W, "clientHeight": VIEW_H}
            return {"cssVisualViewport": visual, "cssContentSize": {"width": VIEW_W, "height": tab.height}}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": self._evaluate(tab, p.get("expression", ""))}}
        if method == "Accessibility.getFullAXTree":
            if p.get("frameId"):
                raise CdpFail("Frame with the given frameId is not found")
            return {"nodes": self._ax(tab)}
        if method == "DOMSnapshot.captureSnapshot":
            return self._dom(tab)
        if method == "Emulation.setUserAgentOverride":
            tab.user_agent = p.get("userAgent", "")
            return {}
        if method == "Page.startScreencast":
            tab.screencast = True
            frame = {
                "data": "anBlZw==",
                "metadata": {"deviceWidth": VIEW_W, "deviceHeight": VIEW_H},
                "sessionId": 1,
            }
            asyncio.get_running_loop().call_later(
                0.01,
                lambda: asyncio.ensure_future(
                    self.emit("Page.screencastFrame", frame, self._session_of(tab.target))
                ),
            )
            return {}
        if method in ("Page.stopScreencast", "Page.screencastFrameAck"):
            if method == "Page.stopScreencast":
                tab.screencast = False
            return {}
        if method == "Page.captureScreenshot":
            return {"data": "anBlZw=="}
        node = self._node(tab, p)
        if method == "DOM.scrollIntoViewIfNeeded":
            if node.rect is not None and not node.overlay:
                top, bottom = node.rect[1], node.rect[1] + node.rect[3]
                if top < tab.scroll or bottom > tab.scroll + VIEW_H:
                    tab.scroll = max(0.0, min(top - 100, tab.height - VIEW_H))
            return {}
        if method == "DOM.getContentQuads":
            rect = self.viewport_rect(tab, node)
            if rect is None:
                raise CdpFail("Could not compute content quads.")
            x, y, w, h = rect
            return {"quads": [[x, y, x + w, y, x + w, y + h, x, y + h]]}
        if method == "DOM.getNodeForLocation":
            found = self.hit(tab, float(p["x"]), float(p["y"]))
            return {"backendNodeId": found.backend if found is not None else tab.root.backend}
        if method == "DOM.focus":
            tab.focused, tab.selected_all = node.backend, False
            return {}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{node.backend}"}}
        if method == "Runtime.callFunctionOn":
            return await self._call(tab, node, p)
        if method == "Input.dispatchMouseEvent":
            return await self._mouse(tab, p)
        if method == "Input.insertText":
            focused = tab.by_backend(tab.focused)
            if focused is not None:
                focused.value = p["text"] if tab.selected_all else focused.value + p["text"]
                tab.selected_all = False
            return {}
        if method == "Input.dispatchKeyEvent":
            return await self._key(tab, p)
        raise CdpFail(f"'{method}' wasn't found")

    def _node(self, tab: FakePage, p: dict[str, Any]) -> N:
        backend = p.get("backendNodeId")
        if backend is None and str(p.get("objectId", "")).startswith("obj-"):
            backend = int(str(p["objectId"])[4:])
        if backend is None:
            if "x" in p or "type" in p or "text" in p:
                return tab.root
            raise CdpFail("Either nodeId, backendNodeId or objectId must be specified")
        found = tab.by_backend(int(backend))
        if found is None:
            raise CdpFail("No node with given id found")
        return found

    def _evaluate(self, tab: FakePage, expression: str) -> Any:
        if expression == page.STATE_JS:
            return ["complete", VIEW_W, VIEW_H, 1, "visible" if tab.target == self.active else "hidden"]
        if expression == "document.readyState":
            return "complete"
        if expression == "document.visibilityState":
            return "visible" if tab.target == self.active else "hidden"
        if ".includes(" in expression:
            wanted = json.loads(expression.rsplit(".includes(", 1)[1][:-1])
            body = " ".join(n.name for n in tab.nodes() if n.role == "StaticText").casefold()
            return wanted in body
        return None

    async def _call(self, tab: FakePage, node: N, p: dict[str, Any]) -> Any:
        function = p.get("functionDeclaration", "")
        if function == tools.CLICK_JS:
            result = await self.click(tab, node)
            return HOLD if result is HOLD else {"result": {"type": "undefined"}}
        if function == tools.SELECT_ALL_JS:
            tab.selected_all = True
            return {"result": {"type": "undefined"}}
        if function == tools.SELECT_OPTION_JS:
            want = str((p.get("arguments") or [{}])[0].get("value", "")).casefold()
            found = next((o for o in node.options if want in o.casefold()), None)
            if found is None:
                return {"result": {"type": "object", "value": {"ok": False, "options": node.options}}}
            node.value = found
            return {"result": {"type": "object", "value": {"ok": True, "text": found}}}
        raise CdpFail("чужой код в странице")

    async def _mouse(self, tab: FakePage, p: dict[str, Any]) -> Any:
        kind = p.get("type")
        if kind == "mouseWheel":
            tab.scroll = max(0.0, min(tab.scroll + float(p.get("deltaY") or 0), tab.height - VIEW_H))
            return {}
        if kind == "mouseReleased" and int(p.get("clickCount") or 1) == 1:
            found = self.hit(tab, float(p["x"]), float(p["y"]))
            if found is not None:
                target = found
                while (
                    target is not None and target.click is None and target.tag not in ("A", "BUTTON", "INPUT")
                ):
                    target = next((n for n in tab.nodes() if target in n.children), None)
                result = await self.click(tab, target or found)
                if result is HOLD:
                    return HOLD
        return {}

    async def _key(self, tab: FakePage, p: dict[str, Any]) -> Any:
        if p.get("type") not in ("keyDown", "rawKeyDown"):
            return {}
        focused = tab.by_backend(tab.focused)
        if p.get("key") == "Enter" and focused is not None:
            if focused.enter is not None:
                result = focused.enter(self, tab, focused)
                if asyncio.iscoroutine(result):
                    await result
            else:
                parent = next((n for n in tab.nodes() if n.role == "form" and focused in n.walk()), None)
                submit = next((n for n in (parent.walk() if parent else []) if n.role == "button"), None)
                if submit is not None:
                    await self.click(tab, submit)
        elif p.get("key") == "Delete" and focused is not None and tab.selected_all:
            focused.value, tab.selected_all = "", False
        return {}

    def _ax(self, tab: FakePage) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def visit(node: N, parent: str) -> None:
            entry: dict[str, Any] = {
                "nodeId": str(node.backend),
                "ignored": node.role in ("generic", "form") and not node.name and not node.clickable,
                "role": {"type": "role", "value": node.role if node.role != "form" else "form"},
                "name": {"type": "computedString", "value": node.name},
                "backendDOMNodeId": node.backend,
                "childIds": [str(c.backend) for c in node.children],
                "properties": [],
            }
            if parent:
                entry["parentId"] = parent
            props = entry["properties"]
            if node.focusable:
                props.append({"name": "focusable", "value": {"type": "boolean", "value": True}})
            if node.backend == tab.focused:
                props.append({"name": "focused", "value": {"type": "boolean", "value": True}})
            if node.role == "checkbox":
                props.append(
                    {"name": "checked", "value": {"type": "tristate", "value": str(node.checked).lower()}}
                )
            if node.role == "heading":
                props.append(
                    {"name": "level", "value": {"type": "integer", "value": int(node.attrs["level"])}}
                )
            if node.role == "link":
                props.append(
                    {
                        "name": "url",
                        "value": {"type": "string", "value": urljoin(tab.url, node.attrs["href"])},
                    }
                )
            if node.role in ("textbox", "searchbox", "combobox") and node.value:
                masked = "•" * len(node.value) if node.attrs.get("type") == "password" else node.value
                entry["value"] = {"type": "string", "value": masked}
            out.append(entry)
            for child in node.children:
                visit(child, entry["nodeId"])

        visit(tab.root, "")
        return out

    def _dom(self, tab: FakePage) -> dict[str, Any]:
        strings: list[str] = []

        def s(value: str) -> int:
            if value not in strings:
                strings.append(value)
            return strings.index(value)

        nodes = tab.nodes()
        index = {id(n): i for i, n in enumerate(nodes)}
        parents = [-1] * len(nodes)
        for n in nodes:
            for child in n.children:
                parents[index[id(child)]] = index[id(n)]
        layout_index, bounds, styles = [], [], []
        for i, n in enumerate(nodes):
            if n.rect is not None:
                x, y, w, h = n.rect
                layout_index.append(i)
                bounds.append([x, y + tab.scroll if n.overlay else y, w, h])
                styles.append([s("pointer")] if n.pointer else [s("auto")])
        document = {
            "documentURL": s(tab.url),
            "scrollOffsetX": 0,
            "scrollOffsetY": tab.scroll,
            "nodes": {
                "parentIndex": parents,
                "nodeName": [s(n.tag) for n in nodes],
                "backendNodeId": [n.backend for n in nodes],
                "attributes": [[v for k, val in n.attrs.items() for v in (s(k), s(val))] for n in nodes],
                "isClickable": {
                    "index": [i for i, n in enumerate(nodes) if n.clickable or n.tag in ("A", "BUTTON")]
                },
                # значения полей открытым текстом — как в настоящем DOMSnapshot (Jarvis их брать не должен)
                "inputValue": {
                    "index": [i for i, n in enumerate(nodes) if n.value],
                    "value": [s(n.value) for n in nodes if n.value],
                },
            },
            "layout": {"nodeIndex": layout_index, "bounds": bounds, "styles": styles},
        }
        return {"documents": [document], "strings": strings}


APP = launch.BrowserApp("chrome", "Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def use_fake(ctx: Any, chrome: FakeChrome) -> list[list[str]]:
    """Браузер Jarvis включён, «запуск» поднимает поддельный Chrome и пишет DevToolsActivePort в профиль."""
    ctx.settings.patch({"ai": {"browser": True}})
    browser = ctx.browser
    launched: list[list[str]] = []

    async def launcher(app: launch.BrowserApp, args: list[str]) -> None:
        launched.append(args)
        await chrome.start(browser.profile)

    browser.launcher = launcher
    browser.find = lambda choice: APP
    return launched


class CdpFail(Exception):
    pass


HOLD = object()  # ответ на команду придёт, когда закроют окно сайта


def shop(base: str = "https://shop.test") -> Site:
    """Сайты для тестов: магазин (поиск, покупка, вход, скачивание, удаление с подтверждением, баннер cookies,
    длинная страница), страница поиска, корзина; nowhere.test — не находится."""

    def sites(url: str) -> tuple[str, list[N]] | None:
        parsed = urlparse(url)
        if parsed.hostname == "nowhere.test":
            return None
        path = parsed.path or "/"
        if path.startswith("/search"):
            query = parsed.query.partition("=")[2]
            return (
                f"Поиск: {query}",
                [heading(f"Найдено по запросу «{query}»"), link("Наушники X1", "/item/1")],
            )
        if path == "/checkout":
            return ("Оформление заказа", [heading("Заказ оформлен")])
        if path == "/cart":
            return ("Корзина", [heading("Корзина"), text("Пусто")])
        if path == "/long":
            items = [link(f"Товар {i}", f"/item/{i}") for i in range(1, 61)]
            return ("Каталог", [heading("Каталог"), *items, text("Конец каталога")])
        if path == "/covered":
            buy = button("Купить", click=go(base + "/checkout"))
            banner = N(
                "generic", "", "DIV", overlay=True, rect=(0.0, 0.0, VIEW_W, 200.0),
                children=[text("Мы используем cookies"), button("Принять")],
            )  # fmt: skip
            banner.children[1].rect = (500.0, 20.0, 100.0, 30.0)
            banner.children[1].overlay = True
            return ("Баннер", [heading("Товар"), buy, banner])
        return (
            "Магазин",
            [
                link("Корзина", "/cart"),
                heading("Наушники"),
                text("Отличные наушники за 1 990 ₽"),
                field_(
                    "Поиск по магазину", role="searchbox", kind="search", enter=go(base + "/search?q={value}")
                ),
                button("Купить", click=go(base + "/checkout")),
                button("Удалить", click=confirm("Удалить товар из корзины?")),
                link("Скачать прайс", "/price.zip", attrs={"download": ""}),
                link("Каталог", "/get?id=7", attrs={"data-file": ""}),  # скачивает, хотя по ссылке не видно
                select("Цвет", ["Красные", "Синие"]),
                N("checkbox", "Подарочная упаковка", "INPUT", {"type": "checkbox"}, focusable=True),
                N(
                    "generic",
                    "",
                    "DIV",
                    clickable=True,
                    children=[text("Купить в один клик")],
                    click=go(base + "/checkout"),
                ),
                form(
                    field_("Логин"),
                    field_("Пароль", kind="password"),
                    button("Войти", attrs={"type": "submit"}, click=go(base + "/cart")),
                ),
            ],
        )

    return sites
