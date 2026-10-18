"""Импорт серверов MCP из других программ: Claude Code, Claude Desktop, Cursor, VS Code, Codex, Gemini CLI."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.mcp import clients
from jarvis.mcp.config import MASK

LINUX_ENV: dict[str, str] = {}  # без XDG_CONFIG_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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
