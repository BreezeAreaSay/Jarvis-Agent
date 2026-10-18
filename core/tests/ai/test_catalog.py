"""Каталог инструментов Jarvis AI: встроенные, MCP и действия агента — риск, режимы и разрешения."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.catalog import AGENT_ACTIONS, BUILTIN_INFO, describe
from jarvis.ai.tools import builtin_tools
from jarvis.context import AppContext

SPECS = [
    {
        "type": "function",
        "function": {"name": "mcp__telegram__send_message", "description": "Send a message"},
    },
    {"type": "function", "function": {"name": "mcp__telegram__get_chats", "description": "List chats"}},
]
TITLES = {
    "mcp__telegram__send_message": "Telegram: send_message",
    "mcp__telegram__get_chats": "Telegram: get_chats",
}


@pytest.fixture
def with_mcp(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.setattr(ctx.mcp, "tools", lambda: SPECS)
    monkeypatch.setattr(ctx.mcp, "tool_title", lambda name: TITLES[name])
    return ctx


def _by_key(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["policy_key"]: i for i in data["items"]}


def test_every_builtin_tool_has_plain_description() -> None:
    assert {t.name for t in builtin_tools()} <= set(BUILTIN_INFO)


def test_catalog_lists_tools_with_risk_and_policy(with_mcp: AppContext) -> None:
    data = describe(with_mcp)
    items = _by_key(data)
    search = items["web_search"]
    assert (
        search["title"] == "Поиск в интернете" and search["source"] == "builtin" and search["group"] == "web"
    )
    assert search["risk"] == "read" and search["decision"] == "allow" and search["override"] is None
    assert search["modes"] == ["chat", "all", "agent"] and search["available"] is True
    assert items["press_keys"]["modes"] == ["all", "agent"]
    send = items["mcp__telegram__send_message"]
    assert send["source"] == "mcp:Telegram" and send["title"] == "send_message"
    assert send["risk"] == "risky" and send["decision"] == "ask" and send["default"] == "ask"
    assert items["mcp__telegram__get_chats"]["decision"] == "allow"
    click = items["agent.ui_click"]
    assert click["source"] == "agent" and click["name"] == "ui_click" and click["modes"] == ["agent"]
    assert len([i for i in data["items"] if i["source"] == "agent"]) == len(AGENT_ACTIONS)
    groups = [g["id"] for g in data["groups"]]
    assert groups[:4] == ["web", "memory", "computer", "agent"] and "mcp:Telegram" in groups
    assert data["agent_confirm"] == "risky"


def test_catalog_respects_user_choices(with_mcp: AppContext) -> None:
    with_mcp.settings.patch(
        {
            "ai": {
                "web_search": False,
                "tool_policy": {
                    "mcp__telegram__send_message": "allow",
                    "press_keys": "deny",
                    "agent.ui_click": "ask",
                    "launch_app": "auto",
                },
            }
        }
    )
    items = _by_key(describe(with_mcp))
    assert items["mcp__telegram__send_message"]["decision"] == "allow"
    assert items["mcp__telegram__send_message"]["override"] == "allow"
    assert items["press_keys"]["decision"] == "deny" and items["agent.ui_click"]["decision"] == "ask"
    assert items["launch_app"]["override"] is None and items["launch_app"]["decision"] == "allow"
    assert items["web_search"]["available"] is False


def test_tools_api(client: TestClient) -> None:
    data = client.get("/api/ai/tools").json()
    assert any(i["policy_key"] == "remember" for i in data["items"])
    res = client.patch("/api/settings", json={"ai": {"tool_policy": {"type_text": "ask"}}})
    assert res.status_code == 200
    items = {i["policy_key"]: i for i in client.get("/api/ai/tools").json()["items"]}
    assert items["type_text"]["decision"] == "ask"
    assert (
        client.patch("/api/settings", json={"ai": {"tool_policy": {"type_text": "maybe"}}}).status_code == 422
    )
