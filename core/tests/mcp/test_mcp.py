"""MCP: настройки серверов, имена инструментов, каталог реестра, настоящий stdio-сервер, API."""

import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import types

from jarvis.context import AppContext
from jarvis.mcp.catalog import map_catalog
from jarvis.mcp.client import result_text
from jarvis.mcp.config import (
    KV_KEY,
    MASK,
    McpServerConfig,
    is_secret,
    load,
    normalize,
    save,
    slug,
    split_command,
)
from jarvis.mcp.service import Mcp, tool_name
from jarvis.services import UserFacingError
from tests.conftest import EventLog

ECHO_SERVER = Path(__file__).with_name("echo_server.py")

REGISTRY_PAGE = {
    "servers": [
        {
            "server": {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json",
                "name": "io.github.modelcontextprotocol/server-filesystem",
                "description": "Доступ к файлам",
                "repository": {"url": "https://github.com/modelcontextprotocol/servers", "source": "github"},
                "version": "1.0.2",
                "packages": [
                    {
                        "registryType": "npm",
                        "identifier": "@modelcontextprotocol/server-filesystem",
                        "version": "1.0.2",
                        "transport": {"type": "stdio"},
                        "packageArguments": [
                            {"type": "positional", "valueHint": "path", "isRequired": True},
                            {"type": "named", "name": "--verbose"},
                        ],
                        "environmentVariables": [
                            {"name": "LOG_LEVEL", "description": "Уровень журнала", "default": "info"}
                        ],
                    }
                ],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
        },
        {
            "server": {
                "name": "io.github.someone/weather-mcp",
                "title": "Weather",
                "description": "Погода",
                "version": "0.3.0",
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "weather-mcp",
                        "version": "0.3.0",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [
                            {
                                "name": "WEATHER_API_KEY",
                                "isRequired": True,
                                "isSecret": True,
                                "description": "Ключ",
                            }
                        ],
                    },
                    {"registryType": "oci", "identifier": "ghcr.io/someone/weather", "version": "0.3.0"},
                ],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
        },
        {
            "server": {
                "name": "com.example/remote",
                "description": "Удалённый сервер",
                "version": "2.0.0",
                "remotes": [
                    {"type": "sse", "url": "https://example.com/sse"},
                    {"type": "streamable-http", "url": "https://example.com/mcp"},
                ],
            }
        },
        # та же запись старой версии — не должна вытеснить новую
        {
            "server": {"name": "io.github.someone/weather-mcp", "version": "0.2.0", "description": "старая"},
            "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": False}},
        },
        {"server": {"description": "без имени — пропускаем"}},
        "мусор",
    ],
    "metadata": {"nextCursor": "page-2", "count": 5},
}

OLD_REGISTRY_PAGE = {
    "servers": [
        {
            "name": "io.github.old/docker-tool",
            "description": "Старый формат",
            "version_detail": {"version": "1.1.0"},
            "repository": {"url": "https://github.com/old/docker-tool"},
            "packages": [
                {
                    "registry_name": "docker",
                    "name": "old/docker-tool",
                    "version": "1.1.0",
                    "environment_variables": [{"name": "TOKEN", "is_required": True, "is_secret": True}],
                }
            ],
        }
    ],
    "metadata": {"next_cursor": None, "total": 1},
}


def test_config_normalize_and_secrets() -> None:
    config = normalize(
        {"name": "Файлы", "command": "npx -y @modelcontextprotocol/server-filesystem /tmp"}, server_id="f"
    )
    assert (config.command, config.args) == ("npx", ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    with pytest.raises(UserFacingError):
        normalize({"name": "X", "transport": "http", "url": "ftp://x"}, server_id="x")
    with pytest.raises(UserFacingError):
        normalize({"name": "", "command": "npx"}, server_id="x")
    with pytest.raises(UserFacingError):
        normalize({"name": "X", "command": ""}, server_id="x")
    previous = McpServerConfig(
        id="h",
        name="H",
        transport="http",
        url="https://h.example/mcp",
        headers={"Authorization": "Bearer real"},
    )
    updated = normalize(
        {
            "name": "H",
            "transport": "http",
            "url": "https://h.example/mcp",
            "headers": {"Authorization": MASK},
        },
        server_id="h",
        previous=previous,
    )
    assert updated.headers == {"Authorization": "Bearer real"}
    assert split_command('uvx "my server" --port 1') == ["uvx", "my server", "--port", "1"]


def test_secrets_are_encrypted_in_db_and_old_plaintext_migrates(ctx: AppContext) -> None:
    assert is_secret("TELEGRAM_API_HASH") and is_secret("SESSION_STRING") and not is_secret("TELEGRAM_API_ID")
    # прежние версии хранили значения открытыми — при чтении они шифруются
    old = McpServerConfig(
        id="tg", name="Telegram", command="uvx", env={"TELEGRAM_API_HASH": "abc123", "ID": "42"}
    )
    ctx.db.kv_set(KV_KEY, [old.model_dump(mode="json")])
    loaded = load(ctx.db)
    assert loaded[0].env == {"TELEGRAM_API_HASH": "abc123", "ID": "42"}
    raw = ctx.db.kv_get(KV_KEY)
    assert all(v.startswith("enc:") for v in raw[0]["env"].values()) and "abc123" not in str(raw)
    save(ctx.db, loaded)
    assert load(ctx.db)[0].env == loaded[0].env


def test_tool_names_are_sanitized_and_unique() -> None:
    assert slug("Мои файлы!") == "moi_fayly"
    used: set[str] = set()
    first = tool_name("moi_fayly", "read file/v2", used)
    assert first == "mcp__moi_fayly__read_file_v2"
    long = tool_name("server", "x" * 100, used)
    assert len(long) == 64 and long.startswith("mcp__server__")
    used |= {first, long}
    duplicate = tool_name("server", "x" * 100, used)
    assert duplicate != long and len(duplicate) == 64 and duplicate.endswith("_2")


def test_result_text() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="Первая строка"),
            types.ImageContent(type="image", data="AAAA", mimeType="image/png"),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(uri="file:///a.txt", text="из файла"),
            ),
        ]
    )
    assert result_text(result) == "Первая строка\n[изображение]\nиз файла"
    structured = types.CallToolResult(content=[], structuredContent={"temp": 5})
    assert result_text(structured) == '{"temp": 5}'


def test_catalog_mapping() -> None:
    page = map_catalog(REGISTRY_PAGE)
    assert page["next_cursor"] == "page-2" and page["total"] == 3
    items = {i["name"]: i for i in page["items"]}
    files = items["io.github.modelcontextprotocol/server-filesystem"]
    assert files["title"] == "Filesystem" and files["version"] == "1.0.2"
    assert files["repository"] == "https://github.com/modelcontextprotocol/servers"
    assert files["install"] == {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem@1.0.2", "<path>"],
        "url": "",
        "env_vars": [
            {
                "name": "LOG_LEVEL",
                "description": "Уровень журнала",
                "required": False,
                "secret": False,
                "default": "info",
            }
        ],
    }
    weather = items["io.github.someone/weather-mcp"]
    assert weather["title"] == "Weather" and weather["description"] == "Погода"
    assert (weather["install"]["command"], weather["install"]["args"]) == ("uvx", ["weather-mcp"])
    assert weather["install"]["env_vars"][0] == {
        "name": "WEATHER_API_KEY",
        "description": "Ключ",
        "required": True,
        "secret": True,
    }
    docker = weather["installs"][1]
    assert docker["command"] == "docker" and docker["args"] == [
        "run",
        "-i",
        "--rm",
        "ghcr.io/someone/weather:0.3.0",
    ]
    remote = items["com.example/remote"]["install"]
    assert remote["transport"] == "http" and remote["url"] == "https://example.com/mcp"

    old = map_catalog(OLD_REGISTRY_PAGE)
    assert old["next_cursor"] is None and old["total"] == 1
    item = old["items"][0]
    assert item["version"] == "1.1.0" and item["repository"] == "https://github.com/old/docker-tool"
    assert item["install"]["args"] == ["run", "-i", "--rm", "-e", "TOKEN", "old/docker-tool:1.1.0"]
    assert map_catalog("не JSON") == {"items": [], "next_cursor": None, "total": 0}


async def test_catalog_search_request(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REGISTRY_PAGE)

    mcp: Any = ctx.mcp
    mcp.catalog.transport = httpx.MockTransport(handler)
    page = await mcp.catalog_search("files", "abc")
    assert len(page["items"]) == 3
    params = seen[0].url.params
    assert seen[0].url.host == "registry.modelcontextprotocol.io" and seen[0].url.path == "/v0/servers"
    assert (params["search"], params["cursor"], params["limit"]) == ("files", "abc", "30")
    mcp.catalog.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(UserFacingError, match="Каталог MCP"):
        await mcp.catalog_search()


async def test_stdio_server_round_trip(ctx: AppContext, events: EventLog) -> None:
    mcp: Any = ctx.mcp
    assert isinstance(mcp, Mcp)
    await mcp.start()
    try:
        server = await mcp.add(
            {"name": "Эхо", "transport": "stdio", "command": sys.executable, "args": [str(ECHO_SERVER)]}
        )
        assert await mcp.wait_connected(server["id"], 30), mcp.get(server["id"])
        info = mcp.get(server["id"])
        assert info["status"] == "connected" and {t["name"] for t in info["tools"]} == {"add", "echo", "fail"}
        specs = {s["function"]["name"]: s["function"] for s in mcp.tools()}
        assert set(specs) == {"mcp__eho__add", "mcp__eho__echo", "mcp__eho__fail"}
        assert specs["mcp__eho__add"]["parameters"]["properties"]["a"]["type"] == "integer"
        assert mcp.tool_title("mcp__eho__add") == "Эхо: add"
        # инструкции сервера (initialize) — для подсказок модели
        assert mcp.server_notes() == [
            {
                "name": "Эхо",
                "prefix": "mcp__eho__",
                "instructions": "Эхо: складывает числа и повторяет текст.",
            }
        ]
        assert await mcp.call("mcp__eho__add", {"a": 2, "b": 3}) == "5"
        assert await mcp.call("mcp__eho__echo", {"text": "привет"}) == "привет"
        with pytest.raises(UserFacingError, match="вернул ошибку"):
            await mcp.call("mcp__eho__fail", {"reason": "так задумано"})
        with pytest.raises(UserFacingError):
            await mcp.call("mcp__eho__nope", {})
        statuses = [e["status"] for e in events.of("mcp.status") if e["server_id"] == server["id"]]
        assert statuses[0] == "connecting" and "connected" in statuses
        assert any(e.get("tools_count") == 3 for e in events.of("mcp.status"))

        # выключенный сервер не подключается, инструменты пропадают
        disabled = await mcp.update(server["id"], {**server, "enabled": False})
        assert disabled["status"] == "disabled" and mcp.tools() == []
    finally:
        await mcp.stop()


async def test_broken_server_reports_error(ctx: AppContext) -> None:
    mcp: Any = ctx.mcp
    await mcp.start()
    try:
        server = await mcp.add({"name": "Сломанный", "command": "jarvis-no-such-program-xyz"})
        deadline = time.monotonic() + 10
        while mcp.get(server["id"])["status"] != "error" and time.monotonic() < deadline:
            await __import__("asyncio").sleep(0.05)
        info = mcp.get(server["id"])
        assert info["status"] == "error"
        assert "Не найдена программа «jarvis-no-such-program-xyz»" in info["error"]
        with pytest.raises(UserFacingError):
            await mcp.reconnect("nope")
        assert await mcp.remove(server["id"]) and not await mcp.remove(server["id"])
    finally:
        await mcp.stop()


def test_mcp_api(client: TestClient) -> None:
    created = client.post(
        "/api/mcp/servers",
        json={
            "name": "GitHub",
            "transport": "http",
            "url": "https://api.example.com/mcp",
            "headers": {"Authorization": "Bearer secret-token"},
            "enabled": False,
        },
    ).json()
    assert created["status"] == "disabled" and created["headers"] == {"Authorization": MASK}
    assert created["id"].startswith("github-") and created["tools"] == []
    assert client.get("/api/mcp/servers").json()["items"][0]["id"] == created["id"]
    stored = client.ctx.db.kv_get("mcp:servers")  # type: ignore[attr-defined]
    # в базе секрет зашифрован, наружу — маска, серверу — настоящее значение
    assert stored[0]["headers"]["Authorization"].startswith("enc:") and "secret-token" not in str(stored)
    assert load(client.ctx.db)[0].headers == {"Authorization": "Bearer secret-token"}  # type: ignore[attr-defined]

    payload = {**created, "name": "GitHub 2", "headers": {"Authorization": MASK}}
    updated = client.put(f"/api/mcp/servers/{created['id']}", json=payload).json()
    assert updated["name"] == "GitHub 2"
    assert load(client.ctx.db)[0].headers["Authorization"] == "Bearer secret-token"  # type: ignore[attr-defined]
    assert client.post(f"/api/mcp/servers/{created['id']}/reconnect").json()["status"] == "disabled"

    bad = client.post("/api/mcp/servers", json={"name": "Без команды", "transport": "stdio"})
    assert bad.status_code == 400 and "команду" in bad.json()["detail"]
    assert client.put("/api/mcp/servers/nope", json=payload).status_code == 404
    assert client.post("/api/mcp/servers/nope/reconnect").status_code == 404
    assert client.delete(f"/api/mcp/servers/{created['id']}").json() == {"ok": True}
    assert client.delete(f"/api/mcp/servers/{created['id']}").status_code == 404

    client.ctx.mcp.catalog.transport = httpx.MockTransport(  # type: ignore[attr-defined]
        lambda request: httpx.Response(200, json=REGISTRY_PAGE)
    )
    catalog = client.get("/api/mcp/catalog", params={"q": "files"}).json()
    assert catalog["next_cursor"] == "page-2" and len(catalog["items"]) == 3


async def test_catalog_retries_without_version_param(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "version" in request.url.params:
            return httpx.Response(400, json={"error": "unknown parameter version"})
        return httpx.Response(200, json=REGISTRY_PAGE)

    mcp: Any = ctx.mcp
    mcp.catalog.transport = httpx.MockTransport(handler)
    page = await mcp.catalog_search("weather")
    assert (
        len(seen) == 2 and seen[0].url.params["version"] == "latest" and "version" not in seen[1].url.params
    )
    assert len(page["items"]) == 3  # повторы версий схлопнуты
