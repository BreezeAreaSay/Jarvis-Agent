"""AI Dev System и импорт серверов MCP из других программ (Claude, Cursor, VS Code, Codex, Gemini)."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.mcp import aidev, clients
from jarvis.mcp.config import MASK
from jarvis.mcp.service import Mcp
from jarvis.services import UserFacingError

ECHO_SERVER = Path(__file__).with_name("echo_server.py")
LINUX_ENV: dict[str, str] = {}  # без XDG_CONFIG_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fake_clone(root: Path, version: str = "2.0.0") -> Path:
    script = write(root / "ai-dev-mcp-server" / "bin" / "ai-dev.mjs", "// ai-dev\n")
    write(
        root / "ai-dev-mcp-server" / "package.json",
        json.dumps({"name": "ai-dev-mcp-server", "version": version}),
    )
    return script


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for name in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    return h


def test_discover_reads_every_client_format(home: Path) -> None:
    write(
        home / ".cursor" / "mcp.json",
        json.dumps(
            {"mcpServers": {"github": {"command": "npx", "args": ["-y", "gh"], "env": {"GITHUB_TOKEN": "t"}}}}
        ),
    )
    write(
        home / ".config" / "Code" / "User" / "mcp.json",
        """{
          // серверы VS Code
          "servers": {
            "remote": {"type": "http", "url": "https://mcp.example.com/mcp",
                       "headers": {"Authorization": "Bearer x"}},
            "old": {"type": "sse", "url": "https://sse.example.com/sse"},
            "asks": {"command": "uvx", "args": ["tool", "--key", "${input:api_key}"]},
          },
        }""",
    )
    write(
        home / ".codex" / "config.toml",
        '[mcp_servers.files]\ncommand = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]\n',
    )
    write(
        home / ".gemini" / "settings.json",
        json.dumps({"mcpServers": {"sse": {"url": "https://g.example.com/sse"}}}),
    )
    write(home / ".claude.json", json.dumps({"numStartups": 3, "mcpServers": {"bad": "не сервер"}}))
    write(home / ".config" / "Claude" / "claude_desktop_config.json", "{ битый json")

    found = {(f.client, f.name): f for f in clients.discover(home, LINUX_ENV, "linux")}
    assert set(found) == {
        ("cursor", "github"),
        ("vscode", "remote"),
        ("vscode", "old"),
        ("vscode", "asks"),
        ("codex", "files"),
        ("gemini", "sse"),
    }
    gh = found["cursor", "github"]
    assert (gh.transport, gh.command, gh.args, gh.env, gh.problem) == (
        "stdio",
        "npx",
        ["-y", "gh"],
        {"GITHUB_TOKEN": "t"},
        "",
    )
    remote = found["vscode", "remote"]
    assert (
        remote.transport == "http" and remote.headers == {"Authorization": "Bearer x"} and not remote.problem
    )
    assert "SSE" in found["vscode", "old"].problem and "SSE" in found["gemini", "sse"].problem
    assert "${input" in found["vscode", "asks"].problem
    assert found["codex", "files"].args[-1] == "/tmp" and found["codex", "files"].client_label == "Codex"


def test_client_locations_on_windows(tmp_path: Path) -> None:
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    (local / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    env = {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)}
    files = {(f.client, str(f.path)) for f in clients.client_files(tmp_path, env, "win32")}
    assert ("claude-desktop", str(roaming / "Claude" / "claude_desktop_config.json")) in files
    store = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    assert ("claude-desktop", str(store / "claude_desktop_config.json")) in files
    assert ("vscode", str(roaming / "Code" / "User" / "mcp.json")) in files


def test_jsonc_keeps_strings_intact() -> None:
    text = '{"a": "http://x//y", "b": "/* нет */", /* да */ "c": [1, 2,], // хвост\n}'
    assert clients.parse_jsonc(text) == {"a": "http://x//y", "b": "/* нет */", "c": [1, 2]}


def test_find_prefers_client_entry_then_command_then_clone(home: Path) -> None:
    clone = home / "Projects" / "ai-dev-system"
    script = fake_clone(clone, "2.3.1")
    node = write(home / "bin" / "node", "")
    entry = {
        "command": str(node),
        "args": [str(script), "mcp"],
        "env": {"AI_DEV_HOME": "/x", "AI_DEV_PROFILES": "all"},
    }
    write(home / ".cursor" / "mcp.json", json.dumps({"mcpServers": {"ai-dev": entry}}))

    def no_which(_name: str) -> str | None:
        return None

    install = aidev.find(home, LINUX_ENV, "linux", which=no_which)
    assert install is not None and install.source == "client" and "Cursor" in install.label
    assert install.args == [str(script), "mcp"] and install.env == {
        "AI_DEV_HOME": "/x"
    }  # профили задаёт Jarvis
    assert install.root == str(clone) and install.version == "2.3.1"

    # клон переехал — запись устарела, находится команда ai-dev
    script.unlink()
    install = aidev.find(
        home, LINUX_ENV, "linux", which=lambda n: "/usr/local/bin/ai-dev" if n == "ai-dev" else None
    )
    assert install is not None and (install.source, install.command, install.args) == (
        "command",
        "/usr/local/bin/ai-dev",
        ["mcp"],
    )

    # ни записи, ни команды — клон в привычной папке, Node.js нужной версии
    fake_clone(clone)
    (home / ".cursor" / "mcp.json").unlink()
    install = aidev.find(
        home,
        LINUX_ENV,
        "linux",
        which=lambda n: "/usr/bin/node" if n == "node" else None,
        version_of=lambda _node: (22, 16, 0),
    )
    assert install is not None and install.source == "folder" and install.command == "/usr/bin/node"
    assert install.args == [str(clone / "ai-dev-mcp-server" / "bin" / "ai-dev.mjs"), "mcp"]

    # старый Node — клон не подходит
    old_node = aidev.find(
        home,
        LINUX_ENV,
        "linux",
        which=lambda n: "/usr/bin/node" if n == "node" else None,
        version_of=lambda _n: (20, 1, 0),
    )
    assert old_node is None


def test_from_folder_errors_in_russian(tmp_path: Path) -> None:
    with pytest.raises(UserFacingError, match="нет AI Dev System"):
        aidev.from_folder(tmp_path)
    fake_clone(tmp_path)
    with pytest.raises(UserFacingError, match=r"нужен Node\.js"):
        aidev.from_folder(tmp_path, which=lambda _n: None)
    with pytest.raises(UserFacingError, match=r"а здесь 23\.1\.0"):
        aidev.from_folder(tmp_path, which=lambda _n: "/usr/bin/node", version_of=lambda _n: (23, 1, 0))
    # можно указать и саму папку ai-dev-mcp-server
    inner = aidev.from_folder(
        tmp_path / "ai-dev-mcp-server", which=lambda _n: "/usr/bin/node", version_of=lambda _n: (24, 0, 0)
    )
    assert inner.root == str(tmp_path) and inner.version == "2.0.0"


def test_profiles_and_server_data() -> None:
    assert aidev.normalize_profiles(["git", "memory", "nope"]) == ["core", "memory", "git"]
    assert aidev.normalize_profiles("coding, core") == ["core", "coding"]
    assert aidev.tools_count(["core", "memory"]) == 42
    install = aidev.AiDevInstall("command", "команда ai-dev", "ai-dev", ["mcp"], {"AI_DEV_HOME": "/h"})
    data = aidev.server_data(install, ["memory"])
    assert data["name"] == "AI Dev System" and data["command"] == "ai-dev"
    assert data["env"] == {"AI_DEV_HOME": "/h", "AI_DEV_PROFILES": "core,memory"}


async def test_aidev_connects_found_install_and_changes_profiles(ctx: AppContext, home: Path) -> None:
    # запись ai-dev запускает настоящий маленький сервер MCP (эхо) — проверяем подключение целиком
    entry = {"command": sys.executable, "args": [str(ECHO_SERVER)]}
    write(home / ".gemini" / "settings.json", json.dumps({"mcpServers": {"ai-dev": entry}}))
    mcp: Any = ctx.mcp
    assert isinstance(mcp, Mcp)
    await mcp.start()
    try:
        state = await mcp.aidev_state()
        assert state["found"] and state["install"]["source"] == "client" and state["server"] is None
        assert [p["id"] for p in state["profiles"] if p["enabled"]] == ["core"] and state["tools"] == 25

        state = await mcp.aidev_connect(["memory"])
        server = state["server"]
        assert server["id"] == "ai-dev" and server["name"] == "AI Dev System"
        assert server["env"] == {"AI_DEV_PROFILES": "core,memory"}
        assert await mcp.wait_connected("ai-dev", 30), mcp.get("ai-dev")
        assert any(s["function"]["name"] == "mcp__ai_dev_system__add" for s in mcp.tools())

        # смена профилей не трогает команду запуска
        state = await mcp.aidev_connect(["git"])
        assert state["server"]["env"]["AI_DEV_PROFILES"] == "core,git"
        assert state["server"]["command"] == sys.executable and state["tools"] == 34
        with pytest.raises(UserFacingError, match="уже подключён"):
            await mcp.add({"name": "x", "command": "y"}, server_id="ai-dev")
    finally:
        await mcp.stop()


async def test_aidev_not_found(ctx: AppContext, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aidev.shutil, "which", lambda _name: None)
    mcp: Any = ctx.mcp
    state = await mcp.aidev_state()
    assert (
        state["found"] is False
        and state["install"] is None
        and state["github"].startswith("https://github.com/")
    )
    with pytest.raises(UserFacingError, match="не найден"):
        await mcp.aidev_connect(["core"])


def test_import_api(client: TestClient, home: Path) -> None:
    servers = {
        "github": {"command": "npx", "args": ["-y", "gh"], "env": {"GITHUB_TOKEN": "secret"}},
        "old": {"type": "sse", "url": "https://sse.example.com/sse"},
    }
    write(home / ".config" / "Claude" / "claude_desktop_config.json", json.dumps({"mcpServers": servers}))
    items = client.get("/api/mcp/import").json()["items"]
    by_name = {i["name"]: i for i in items}
    assert (
        by_name["github"]["env"] == {"GITHUB_TOKEN": MASK}
        and by_name["github"]["client_label"] == "Claude Desktop"
    )
    assert by_name["github"]["added"] is False and "SSE" in by_name["old"]["problem"]

    keys = [by_name["github"]["key"], by_name["old"]["key"]]
    result = client.post("/api/mcp/import", json={"keys": keys}).json()
    assert [s["name"] for s in result["items"]] == ["github"]
    assert result["skipped"] == [{"key": by_name["old"]["key"], "reason": by_name["old"]["problem"]}]
    stored = client.ctx.db.kv_get("mcp:servers")  # type: ignore[attr-defined]
    assert stored[0]["env"] == {"GITHUB_TOKEN": "secret"}  # ключ перенесён как есть, наружу — маска

    again = client.post("/api/mcp/import", json={"keys": keys[:1]}).json()
    assert again["items"] == [] and again["skipped"][0]["reason"] == "уже подключён"
    assert client.get("/api/mcp/import").json()["items"][0]["added"] is True

    state = client.get("/api/mcp/aidev").json()
    assert {p["id"] for p in state["profiles"]} >= {"core", "memory", "git"}
    bad = client.post("/api/mcp/aidev", json={"folder": str(home / "nowhere")})
    assert bad.status_code == 400 and "нет AI Dev System" in bad.json()["detail"]
