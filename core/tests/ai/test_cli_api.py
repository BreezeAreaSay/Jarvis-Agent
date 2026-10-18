"""API программ-провайдеров (/api/ai/cli/…) и установка через npm — на фальшивых программах, без сети."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.cli import backend as backend_module
from jarvis.ai.cli import locate
from jarvis.ai.cli.locate import Launch
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from tests.ai.cli_helpers import FAKE, collect, until

FAKE_NPM = """import os, sys, time
from pathlib import Path
print("npm warn deprecated something", file=sys.stderr, flush=True)
print("added 1 package in 2s", flush=True)
if os.environ.get("FAKE_NPM_FAIL"):
    print("npm error code E403", file=sys.stderr)
    print("npm error 403 Forbidden - GET https://registry.npmjs.org/@google%2fgemini-cli", file=sys.stderr)
    sys.exit(1)
Path(os.environ["FAKE_NPM_MARK"]).write_text(" ".join(sys.argv[1:]), encoding="utf-8")
"""


@pytest.fixture
def no_programs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Никаких настоящих программ: Node.js не найден (кнопка «Установить» не нужна для этих проверок)."""
    monkeypatch.setattr(locate, "find_node", lambda *a, **k: None)


def backends(ctx: Any) -> dict[str, Any]:
    return ctx.ai.cli.backends  # type: ignore[no-any-return]


def test_cli_status_install_and_login_routes(client: TestClient, no_programs: None) -> None:
    ctx: Any = client.ctx  # type: ignore[attr-defined]
    for item in backends(ctx).values():
        item.locator = lambda: None
        item.launch(refresh=True)
    state = client.get("/api/ai/cli/gemini_cli").json()
    assert state["installed"] is False and state["can_install"] is False and state["node"] == ""
    assert state["npm"] == "@google/gemini-cli" and state["install_command"].startswith("npm install -g")
    assert state["login_command"] == "gemini" and state["logged_in"] is None
    claude = client.get("/api/ai/cli/claude_code?refresh=true").json()
    assert claude["npm"] == "" and "install.ps1" in claude["install_command"]
    assert client.get("/api/ai/cli/codex").status_code == 404
    assert client.get("/api/ai/cli/openai").status_code == 404

    denied = client.post("/api/ai/cli/claude_code/install")
    assert denied.status_code == 400 and "официальным установщиком" in denied.json()["detail"]
    no_node = client.post("/api/ai/cli/gemini_cli/install")
    assert no_node.status_code == 400 and "Node.js 20 или новее" in no_node.json()["detail"]
    login = client.post("/api/ai/cli/qwen_code/login")
    assert login.status_code == 400 and login.json()["detail"].startswith("Qwen Code не установлен")
    # без токена ядра — никак
    assert client.get("/api/ai/cli/gemini_cli", headers={"X-Jarvis-Token": "nope"}).status_code == 401


def test_cli_provider_status_after_login_check(
    client: TestClient, no_programs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx: Any = client.ctx  # type: ignore[attr-defined]
    claude = backends(ctx)["claude_code"]
    claude.locator = lambda: Launch((sys.executable, FAKE), FAKE)
    claude.launch(refresh=True)
    monkeypatch.setenv("FAKE_CLI_KIND", "claude")
    state = client.get("/api/ai/cli/claude_code?refresh=true").json()
    assert state["installed"] is True and state["path"] == FAKE and state["version"] == "2.1.98"
    # вход проверен командой самой программы (claude auth status), без чтения её файлов
    assert state["logged_in"] is True and state["account"] == "tony@stark.example (подписка Claude Max)"
    monkeypatch.setenv("FAKE_CLI_AUTH", "out")
    state = client.get("/api/ai/cli/claude_code?refresh=true").json()
    assert state["logged_in"] is False and state["account"] == ""


async def test_npm_install_runs_in_background_with_progress(
    running_ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemini: Any = running_ctx.ai.cli.get("gemini_cli")  # type: ignore[attr-defined]
    mark = tmp_path / "installed.txt"
    npm_script = tmp_path / "npm-cli.py"
    npm_script.write_text(FAKE_NPM, encoding="utf-8")
    monkeypatch.setenv("FAKE_NPM_MARK", str(mark))
    monkeypatch.setenv("FAKE_CLI_KIND", "gemini")
    monkeypatch.setattr(locate, "find_node", lambda *a, **k: "/fake/node")

    async def node_version(node: str) -> str:
        return "v22.11.0"

    monkeypatch.setattr(gemini, "_node_version", node_version)
    monkeypatch.setattr(
        backend_module, "_npm_launch", lambda node: Launch((sys.executable, str(npm_script)), "npm")
    )
    gemini.locator = lambda: Launch((sys.executable, FAKE), FAKE) if mark.exists() else None
    gemini.launch(refresh=True)
    events = collect(running_ctx)

    assert (await gemini.status())["can_install"] is True
    assert await gemini.install() == {"ok": True, "installing": True}
    assert (await gemini.status())["installing"] is True and gemini.problem().endswith("подождите немного")
    await until(lambda: any(e["state"] in ("installed", "error") for e in events.of("ai.cli")), 20)
    states = [e["state"] for e in events.of("ai.cli")]
    assert states[0] == "installing" and states[-1] == "installed", events.of("ai.cli")
    assert events.of("ai.cli")[-1]["version"] == "0.66.0"
    assert mark.read_text(encoding="utf-8") == "install -g @google/gemini-cli"
    assert gemini.installed and gemini.problem() is None
    assert await gemini.install() == {"ok": True, "installed": True}

    # ошибка npm — понятный текст и событие error
    monkeypatch.setenv("FAKE_NPM_FAIL", "1")
    mark.unlink()
    gemini.launch(refresh=True)
    await gemini.install()
    await until(lambda: events.of("ai.cli")[-1]["state"] == "error", 20)
    error = events.of("ai.cli")[-1]["error"]
    assert error.startswith("Не удалось установить:") and "403 Forbidden" in error
    assert (await gemini.status())["install_error"] == error


async def test_install_refuses_old_node(running_ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    qwen: Any = running_ctx.ai.cli.get("qwen_code")  # type: ignore[attr-defined]
    qwen.locator = lambda: None
    qwen.launch(refresh=True)
    monkeypatch.setattr(locate, "find_node", lambda *a, **k: "/fake/node")

    async def node_version(node: str) -> str:
        return "v20.18.0"

    monkeypatch.setattr(qwen, "_node_version", node_version)
    with pytest.raises(UserFacingError, match=r"Node\.js 22 или новее"):
        await qwen.install()
    await asyncio.sleep(0)
