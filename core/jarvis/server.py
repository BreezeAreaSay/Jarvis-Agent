"""HTTP/WebSocket-сервер ядра и точка входа `python -m jarvis`.

Интерфейс (Electron) запускает ядро с --port 0 --parent-pid <pid>; ядро слушает 127.0.0.1, печатает в stdout
строку `JARVIS_CORE_READY {"port": …, "token": …}` и завершается вместе с родителем. Все /api/* (кроме
/api/health) требуют заголовок X-Jarvis-Token, /ws — параметр ?token=.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jarvis import __version__, paths
from jarvis import log as jlog
from jarvis.api import include_all
from jarvis.app import build_context, start_services, stop_services
from jarvis.context import AppContext
from jarvis.platform.base import PlatformError
from jarvis.services import UserFacingError

log = logging.getLogger("jarvis")
READY_PREFIX = "JARVIS_CORE_READY "
OPEN_PATHS = ("/api/health",)


def create_app(ctx: AppContext, token: str) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        ctx.loop = loop
        ctx.bus.bind_loop(loop)
        await start_services(ctx)
        log.info("ядро Jarvis %s запущено (платформа %s)", __version__, ctx.platform.name)
        try:
            yield
        finally:
            await stop_services(ctx)
            log.info("ядро остановлено")

    app = FastAPI(title="Jarvis Core", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.state.token = token
    ctx.api_token = token  # сервер MCP Jarvis отдаёт его программам-провайдерам (jarvis.mcp.server)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def auth(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_PATHS and request.method != "OPTIONS":
            given = request.headers.get("x-jarvis-token") or request.query_params.get("token", "")
            if not secrets.compare_digest(given, token):
                return JSONResponse({"detail": "Нет доступа"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(UserFacingError)
    async def user_error(_: Request, exc: UserFacingError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PlatformError)
    async def platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    include_all(app)
    return app


# --- один экземпляр ядра на каталог данных ------------------------------------------------------------


class InstanceLock:
    def __init__(self) -> None:
        self.path = paths.data_dir() / "core.lock"
        self._fh: Any = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+")  # noqa: SIM115 — держим открытым всю жизнь процесса
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def _watch_parent(parent_pid: int, server: uvicorn.Server) -> None:
    import psutil

    while not server.should_exit:
        if not psutil.pid_exists(parent_pid):
            log.info("родительский процесс %s завершился — останавливаю ядро", parent_pid)
            server.should_exit = True
            return
        time.sleep(2.0)


def main(argv: list[str] | None = None) -> None:
    args_in = sys.argv[1:] if argv is None else argv
    if "--selftest" in args_in:
        # самопроверка сборки (jarvis/selftest.py): без сервера, сети и настоящих данных; код выхода 0 / 1
        from jarvis import selftest

        sys.exit(selftest.main(args_in))
    parser = argparse.ArgumentParser(prog="jarvis-core", description="Ядро Jarvis")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_CORE_PORT", "0")))
    parser.add_argument("--data-dir", default=os.environ.get("JARVIS_DATA_DIR", ""))
    parser.add_argument("--platform", default=os.environ.get("JARVIS_PLATFORM", "auto"))
    parser.add_argument("--token", default=os.environ.get("JARVIS_CORE_TOKEN", ""))
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        # вывод в pipe Electron — всегда UTF-8
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    if args.data_dir:
        paths.set_data_dir(args.data_dir)
    jlog.setup(args.debug)

    lock = InstanceLock()
    if not lock.acquire():
        print(json.dumps({"error": "already_running"}), flush=True)
        log.error("ядро уже запущено для каталога %s", paths.data_dir())
        sys.exit(3)

    if args.parent_pid:
        # корень дерева процессов Jarvis: платформа не трогает свои окна и процессы (Electron и потомки)
        os.environ.setdefault("JARVIS_ROOT_PID", str(args.parent_pid))
    token = args.token or secrets.token_urlsafe(24)
    ctx = build_context(args.platform)
    app = create_app(ctx, token)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    port = sock.getsockname()[1]
    host = args.host if args.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    ctx.api_url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"

    config = uvicorn.Config(
        app, log_level="warning", access_log=False, ws_ping_interval=20, ws_ping_timeout=20
    )
    server = uvicorn.Server(config)

    def request_exit() -> None:
        server.should_exit = True

    ctx.request_exit = request_exit

    info = {"port": port, "token": token, "pid": os.getpid(), "version": __version__}
    (paths.data_dir() / "core.json").write_text(json.dumps(info), encoding="utf-8")

    async def serve() -> None:
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if server.started:
            print(READY_PREFIX + json.dumps(info), flush=True)
        await task

    if args.parent_pid:
        threading.Thread(target=_watch_parent, args=(args.parent_pid, server), daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
