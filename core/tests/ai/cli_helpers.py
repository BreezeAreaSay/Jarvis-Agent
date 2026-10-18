"""Помощники тестов программ-провайдеров: ядро с настоящим HTTP-сервером (порт на 127.0.0.1 — к нему ходит
фальшивая программа за инструментами по MCP) и сама фальшивая программа (tests/ai/fake_cli.py)."""

import asyncio
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from jarvis.ai.cli.locate import Launch
from jarvis.app import build_context
from jarvis.context import AppContext
from jarvis.server import create_app
from tests.conftest import TOKEN, EventLog

FAKE = str(Path(__file__).with_name("fake_cli.py"))
KINDS = {"claude_code": "claude", "gemini_cli": "gemini", "qwen_code": "qwen"}


@contextlib.asynccontextmanager
async def live_core() -> AsyncIterator[AppContext]:
    """Ядро целиком (сервисы запускает lifespan) на настоящем порту 127.0.0.1."""
    ctx = build_context("fake")
    app = create_app(ctx, TOKEN)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.02)
        ctx.api_url = f"http://127.0.0.1:{port}"
        yield ctx
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 15)
        ctx.db.close()


class FakeCli:
    """Фальшивая программа для провайдера: сценарии запусков и журнал того, с чем её запустили."""

    def __init__(self, ctx: AppContext, provider: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ctx = ctx
        self.provider = provider
        self.scenario = tmp / f"scenario-{provider}.json"
        self.log = tmp / f"log-{provider}.jsonl"
        self.scenario.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("FAKE_CLI_KIND", KINDS[provider])
        monkeypatch.setenv("FAKE_CLI_SCENARIO", str(self.scenario))
        monkeypatch.setenv("FAKE_CLI_LOG", str(self.log))
        # своя домашняя папка: признаки входа (файлы Gemini CLI и Qwen Code) не зависят от машины
        self.home = tmp / "home"
        (self.home / ".gemini").mkdir(parents=True, exist_ok=True)
        (self.home / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        for name in ("HOME", "USERPROFILE"):
            monkeypatch.setenv(name, str(self.home))
        for name in ("GEMINI_CLI_HOME", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        self.backend: Any = ctx.ai.cli.get(provider)  # type: ignore[attr-defined]
        self.backend.locator = lambda: Launch((sys.executable, FAKE), FAKE)
        self.backend.launch(refresh=True)
        self.terminals: list[Path] = []
        self.backend.terminal = self.terminals.append

    def add(self, *runs: list[dict[str, Any]]) -> None:
        """Сценарии следующих запусков: каждый — список шагов."""
        queue = json.loads(self.scenario.read_text(encoding="utf-8"))
        queue += [{"steps": steps} for steps in runs]
        self.scenario.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")

    @property
    def runs(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [
            json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    @staticmethod
    def arg(run: dict[str, Any], name: str) -> str | None:
        argv = run["argv"]
        return argv[argv.index(name) + 1] if name in argv else None


async def until(check: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("не дождался")
        await asyncio.sleep(0.02)


def collect(ctx: AppContext) -> EventLog:
    log = EventLog()
    ctx.bus.on("*", log)
    return log
