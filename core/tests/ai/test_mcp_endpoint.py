"""Сервер MCP Jarvis (/api/ai/mcp) для программ-провайдеров: доступ только с токеном ядра и токеном хода,
инструменты и картинки — настоящим клиентом SDK mcp."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jarvis.context import AppContext
from jarvis.mcp.server import NAME_LIMIT, McpResult, exposed_name
from tests.ai.cli_helpers import live_core
from tests.conftest import TOKEN

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PNG = "data:image/png;base64,iVBORw0KGgo="


@pytest.fixture
async def core(data_dir: Path) -> AsyncIterator[AppContext]:
    async with live_core() as ctx:
        yield ctx


async def call(url: str, headers: dict[str, str], name: str, args: dict[str, Any]) -> tuple[list[str], Any]:
    async with (
        httpx.AsyncClient(headers=headers, trust_env=False) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool(name, args)
        return [t.name for t in listed.tools], result


async def test_endpoint_needs_core_token_and_live_turn_token(core: AppContext) -> None:
    server: Any = core.ai.mcp_server  # type: ignore[attr-defined]
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(name: str, args: dict[str, Any]) -> McpResult:
        calls.append((name, args))
        if name == "take_screenshot":
            return McpResult("Снимок экрана", images=[PNG])
        return McpResult(f"Громкость: {args['level']}%")

    tools = [
        {
            "name": "set_volume",
            "description": "Громкость",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {"name": "take_screenshot", "description": "Снимок", "inputSchema": {"type": "object"}},
    ]
    binding = server.bind(tools, handler, label="test")
    url = server.url
    assert url == f"{core.api_url}/api/ai/mcp"
    headers = server.headers(binding)
    assert headers == {"X-Jarvis-Token": TOKEN, "X-Jarvis-Turn": binding.token}

    async with httpx.AsyncClient(trust_env=False) as http:
        # без токена ядра — 401 (общая защита /api/*), с ним, но без хода или с чужим ходом — 403
        assert (await http.post(url, json=INIT, headers=ACCEPT)).status_code == 401
        wrong_core = {**ACCEPT, "X-Jarvis-Token": "nope", "X-Jarvis-Turn": binding.token}
        assert (await http.post(url, json=INIT, headers=wrong_core)).status_code == 401
        no_turn = await http.post(url, json=INIT, headers={**ACCEPT, "X-Jarvis-Token": TOKEN})
        assert no_turn.status_code == 403 and "ход Jarvis AI не найден" in no_turn.json()["detail"]
        bad_turn = {**ACCEPT, "X-Jarvis-Token": TOKEN, "X-Jarvis-Turn": "guess"}
        assert (await http.post(url, json=INIT, headers=bad_turn)).status_code == 403
        ok = await http.post(url, json=INIT, headers={**ACCEPT, **headers})
        assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "jarvis"

    names, result = await call(url, headers, "set_volume", {"level": 30})
    assert names == ["set_volume", "take_screenshot"]
    assert result.isError is False and result.content[0].text == "Громкость: 30%"
    _, shot = await call(url, headers, "take_screenshot", {})
    kinds = [block.type for block in shot.content]
    assert kinds == ["text", "image"] and shot.content[1].mimeType == "image/png"
    assert calls == [("set_volume", {"level": 30}), ("take_screenshot", {})]
    assert binding.calls == 2

    # ход закончился — токен больше не открывает ничего
    server.unbind(binding)
    async with httpx.AsyncClient(trust_env=False) as http:
        assert (await http.post(url, json=INIT, headers={**ACCEPT, **headers})).status_code == 403


async def test_unknown_tool_and_handler_errors_come_back_as_tool_errors(core: AppContext) -> None:
    server: Any = core.ai.mcp_server  # type: ignore[attr-defined]

    async def handler(name: str, args: dict[str, Any]) -> McpResult:
        raise RuntimeError("сломалось")

    binding = server.bind(
        [{"name": "get_time", "description": "", "inputSchema": {"type": "object"}}], handler
    )
    try:
        _, failed = await call(server.url, server.headers(binding), "get_time", {})
        assert failed.isError is True and "сломалось" in failed.content[0].text
        _, missing = await call(server.url, server.headers(binding), "rm_rf", {})
        assert missing.isError is True
    finally:
        server.unbind(binding)


def test_tool_names_fit_the_model_limits() -> None:
    assert exposed_name("set_volume") == "set_volume"
    long = "github_" + "x" * 80
    short = exposed_name(long)
    assert len(short) == NAME_LIMIT and short != exposed_name(long + "y")
    assert exposed_name("notes.search") != "notes.search" and "." not in exposed_name("notes.search")
    assert exposed_name("погода").startswith("______")
