"""Пульт в браузере телефона по Wi-Fi: отдельный HTTP-сервер на 0.0.0.0:<lan_port> внутри цикла ядра.

Страница (web/index.html, app.js, style.css) открыта всем в сети, но всё под /remote/api/* — только с токеном
(секрет remote_token): ?t=… один раз из ссылки или QR-кода, дальше страница хранит его у себя и шлёт
заголовок X-Remote-Token. Главный API ядра (127.0.0.1) отсюда недоступен.
"""

import asyncio
import contextlib
import logging
import socket
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from jarvis.platform.base import PlatformError
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.remote.service import Remote

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
MEDIA_ACTIONS = ("play_pause", "next", "prev", "stop", "volume_up", "volume_down", "mute")
POWER_ACTIONS = ("lock", "sleep", "shutdown", "restart", "cancel")


class _Limiter:
    """Неверный токен 20 раз за 10 минут с одного адреса — дальше 429 до конца окна."""

    WINDOW = 600.0
    LIMIT = 20

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}

    def _recent(self, ip: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self._fails.get(ip, []) if now - t < self.WINDOW]
        if recent:
            self._fails[ip] = recent
        else:
            self._fails.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.LIMIT

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.monotonic())
        if len(self._fails) > 1000:  # не копим адреса бесконечно
            self._fails.pop(next(iter(self._fails)))


class DispatchIn(BaseModel):
    text: str


class VolumeIn(BaseModel):
    level: float | None = None
    delta: float | None = None


def create_remote_app(remote: "Remote") -> FastAPI:
    app = FastAPI(title="Jarvis Remote", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = _Limiter()
    actions = remote.actions
    files: dict[str, bytes] = {}

    def read_web(name: str) -> bytes:
        if name not in files:
            files[name] = (WEB_DIR / name).read_bytes()
        return files[name]

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/remote/api/"):
            ip = request.client.host if request.client else "?"
            if limiter.blocked(ip):
                return JSONResponse({"detail": "Слишком много попыток — подождите несколько минут"}, 429)
            given = request.headers.get("x-remote-token") or request.query_params.get("t", "")
            if not remote.check_token(given):
                limiter.fail(ip)
                return JSONResponse(
                    {"detail": "Нет доступа — откройте ссылку или QR-код из приложения Jarvis заново"}, 401
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(UserFacingError)
    async def user_error(_: Request, exc: UserFacingError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PlatformError)
    async def platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # --- страница ---------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return Response(
            read_web("index.html"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(read_web("icon.svg"), media_type="image/svg+xml")

    @app.get("/{name}")
    async def static(name: str) -> Response:
        media_type = STATIC_FILES.get(name)
        if media_type is None:
            raise HTTPException(404, detail="Нет такой страницы")
        return Response(read_web(name), media_type=media_type)

    # --- API --------------------------------------------------------------------------------------
    @app.get("/remote/api/state")
    async def state() -> dict[str, Any]:
        return await actions.state()

    @app.post("/remote/api/dispatch")
    async def dispatch(body: DispatchIn) -> dict[str, Any]:
        text = body.text.strip()
        if not text:
            raise UserFacingError("Напишите, что сделать")
        if len(text) > 2000:
            raise UserFacingError("Слишком длинный текст")
        return await actions.dispatch(text, "remote")

    @app.post("/remote/api/media/{action}")
    async def media(action: str) -> dict[str, Any]:
        if action not in MEDIA_ACTIONS:
            raise HTTPException(404, detail="Нет такой кнопки")
        return (await actions.media(action)).to_json()

    @app.get("/remote/api/media/thumbnail")
    async def thumbnail() -> Response:
        found = await actions.thumbnail()
        if found is None:
            raise HTTPException(404, detail="Обложки нет")
        data, mime = found
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=600"})

    @app.post("/remote/api/volume")
    async def volume(body: VolumeIn) -> dict[str, Any]:
        return (await actions.volume(body.level, body.delta)).to_json()

    @app.get("/remote/api/screenshot")
    async def screenshot() -> Response:
        data, mime = await actions.screenshot("png")
        return Response(data, media_type=mime)

    @app.get("/remote/api/commands")
    async def commands() -> dict[str, Any]:
        return {"items": actions.favorites()}

    @app.post("/remote/api/run/{command_id:path}")
    async def run(command_id: str) -> dict[str, Any]:
        return (await actions.run_favorite(command_id, "remote")).to_json()

    @app.post("/remote/api/power/{action}")
    async def power(action: str) -> dict[str, Any]:
        if action not in POWER_ACTIONS:
            raise HTTPException(404, detail="Нет такого действия")
        return (await actions.power(action)).to_json()

    @app.post("/remote/api/window/minimize_all")
    async def minimize_all() -> dict[str, Any]:
        return (await actions.minimize_all()).to_json()

    return app


class _EmbeddedServer(uvicorn.Server):
    """uvicorn внутри чужого цикла: сигналы (Ctrl+C) остаются за главным сервером ядра."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def bind_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            # на Windows SO_REUSEADDR позволил бы чужой программе занять тот же порт
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class LanServer:
    """Запуск, остановка и перезапуск сервера пульта (при смене порта или выключении в настройках)."""

    def __init__(self, remote: "Remote", host: str = "0.0.0.0") -> None:  # пульт — для всей домашней сети
        self.remote = remote
        self.ctx = remote.ctx
        self.host = host
        self.app = create_remote_app(remote)
        self.status = "off"  # off | starting | running | error
        self.error = ""
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def apply(self, enabled: bool, port: int) -> None:
        if enabled and self.running and self.port == port:
            return
        await self.stop()
        if enabled:
            await self.start(port)

    async def start(self, port: int) -> None:
        try:
            sock = bind_socket(self.host, port)
        except OSError as e:
            log.warning("пульт: порт %s не открылся: %s", port, e)
            self._set(
                "error", f"Порт {port} занят или закрыт системой — выберите другой порт в настройках пульта"
            )
            return
        config = uvicorn.Config(
            self.app,
            lifespan="off",
            log_config=None,
            access_log=False,
            ws="none",
            server_header=False,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=2,
        )
        server = _EmbeddedServer(config)
        self._server = server
        self.port = port
        self._set("starting", "")
        self._task = self.ctx.spawn(self._serve(server, sock), "remote-lan")
        for _ in range(100):  # до 5 с ждём, пока сервер начнёт принимать соединения
            if server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if server.started:
            log.info("пульт в браузере: http://%s:%s/", self.host, port)
            self._set("running", "")

    async def _serve(self, server: uvicorn.Server, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit:  # uvicorn выходит через sys.exit — ядро от этого падать не должно
            log.error("пульт: сервер не запустился")
            self._set("error", "Сервер пульта не запустился")
        except Exception as e:
            log.exception("пульт: сервер упал")
            self._set("error", f"Сервер пульта упал: {e}")
        finally:
            with contextlib.suppress(OSError):
                sock.close()
            if self.status in ("running", "starting") and self._server is server:
                self._set("off", "")

    async def stop(self) -> None:
        server, task = self._server, self._task
        self._server, self._task = None, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), 5.0)
            except asyncio.CancelledError:
                task.cancel()  # нас самих отменили (выключение ядра) — сервер тоже, и отмену не глотаем
                raise
            except Exception:  # не остановился за 5 с (TimeoutError) или упал
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._set("off", "")
        self.port = None

    def _set(self, status: str, error: str) -> None:
        if (status, error) == (self.status, self.error):
            return
        self.status, self.error = status, error
        self.remote.publish()
