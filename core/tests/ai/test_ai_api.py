"""HTTP API Jarvis AI: провайдеры, расходы, проверка связи, чаты и сообщения, агент."""

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.ai.fakes import ScriptedLlm, no_sleep, reply


def _wait_messages(client: TestClient, chat_id: str, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
        if len(messages) >= count:
            return messages
        time.sleep(0.02)
    raise AssertionError("ответ не пришёл")


def test_providers_usage_and_test_without_key(client: TestClient) -> None:
    items = {p["id"]: p for p in client.get("/api/ai/providers").json()["items"]}
    assert set(items) == {
        "openai",
        "chatgpt",
        "claude_code",
        "gemini_cli",
        "qwen_code",
        "openrouter",
        "deepseek",
        "anthropic",
        "gemini",
        "xai",
        "mistral",
        "groq",
        "qwen",
        "moonshot",
        "zhipu",
        "minimax",
        "doubao",
        "siliconflow",
        "yandex",
        "ollama",
        "lmstudio",
        "custom",
    }
    groups = {pid: p["group"] for pid, p in items.items()}
    assert [pid for pid, g in groups.items() if g == "account"] == [
        "chatgpt",
        "claude_code",
        "gemini_cli",
        "qwen_code",
    ]
    assert {pid for pid, g in groups.items() if g == "local"} == {"ollama", "lmstudio"} and groups[
        "custom"
    ] == "custom"
    claude = items["claude_code"]
    assert claude["cli"] is True and claude["needs_key"] is False and claude["login"] is True
    assert claude["default_model"] == "sonnet" and claude["regions"] == [] and claude["folder"] is False
    qwen = items["qwen"]
    assert qwen["country"] == "cn" and [r["id"] for r in qwen["regions"]] == ["intl", "cn", "us"]
    assert qwen["base_url"] == qwen["regions"][0]["base_url"] and qwen["regions"][1]["key_url"]
    assert items["yandex"]["folder"] is True and items["yandex"]["country"] == "ru"
    assert items["openai"]["default_model"] == "gpt-5.4-mini" and items["openai"]["needs_key"] is True
    assert items["openai"]["cli"] is False and items["openai"]["group"] == "key"
    gpt = items["chatgpt"]
    assert gpt["needs_key"] is False and gpt["login"] is True and gpt["component"] is True
    assert gpt["local"] is False and gpt["key_url"] == "" and gpt["default_model"] in gpt["models"]
    assert items["openai"]["login"] is False and items["openai"]["component"] is False
    assert (
        items["lmstudio"]["base_url"] == "http://127.0.0.1:1234/v1"
        and items["lmstudio"]["needs_key"] is False
    )
    usage = client.get("/api/ai/usage").json()
    assert usage == {"month": usage["month"], "budget": 0.0}
    assert usage["month"]["prompt_tokens"] == 0 and usage["month"]["cost"] == 0
    test = client.post("/api/ai/test").json()
    assert test["ok"] is False and test["error"] == "Укажите ключ API в настройках Jarvis AI"
    assert client.get("/api/ai/models").json()["items"] == ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"]
    assert client.get("/api/status").json()["ai"]["configured"] is False


def test_chat_crud_and_messages(client: TestClient) -> None:
    script = ScriptedLlm(reply("Привет, сэр. Чем могу помочь?"))
    ctx: Any = client.ctx  # type: ignore[attr-defined]
    ctx.ai.set_transport(script.transport)
    ctx.ai.llm.sleep = no_sleep
    client.put("/api/secrets", json={"name": "ai_api_key", "value": "sk-test"})
    assert client.get("/api/status").json()["ai"]["configured"] is True

    chat = client.post("/api/chats", json={"mode": "chat"}).json()
    assert chat["mode"] == "chat" and chat["kind"] == "chat" and chat["title"] == "Новый чат"
    posted = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "Привет! Как дела?"}).json()
    assert posted["chat_id"] == chat["id"] and posted["message_id"]
    messages = _wait_messages(client, chat["id"], 2)
    assert messages[0]["id"] == posted["message_id"] and messages[0]["content"] == "Привет! Как дела?"
    assert messages[1]["role"] == "assistant" and messages[1]["content"] == "Привет, сэр. Чем могу помочь?"
    assert messages[1]["meta"]["usage"]["prompt_tokens"] == 100

    listed = client.get("/api/chats").json()["items"]
    assert listed[0]["id"] == chat["id"] and listed[0]["title"] == "Привет! Как дела"
    patched = client.patch(
        f"/api/chats/{chat['id']}", json={"title": "Болтовня", "pinned": True, "model": "gpt-5.4"}
    )
    assert patched.json()["pinned"] is True and patched.json()["model"] == "gpt-5.4"
    assert client.patch(f"/api/chats/{chat['id']}", json={"mode": "wrong"}).status_code == 422
    assert client.post(f"/api/chats/{chat['id']}/stop").json() == {"ok": True}
    assert client.get("/api/ai/usage").json()["month"]["prompt_tokens"] == 100

    empty = client.post(f"/api/chats/{chat['id']}/messages", json={"text": "  "})
    assert empty.status_code == 400 and empty.json()["detail"] == "Пустое сообщение"
    assert client.delete(f"/api/chats/{chat['id']}").json() == {"ok": True}
    for method, path in [
        ("get", f"/api/chats/{chat['id']}"),
        ("delete", f"/api/chats/{chat['id']}"),
        ("post", f"/api/chats/{chat['id']}/stop"),
    ]:
        assert getattr(client, method)(path).status_code == 404
    assert client.post("/api/chats/nope/messages", json={"text": "x"}).status_code == 404


def test_agent_routes(client: TestClient) -> None:
    assert client.get("/api/agent").json()["status"] == "idle"
    response = client.post("/api/agent/run", json={"task": "Открой почту"})
    assert (
        response.status_code == 400 and response.json()["detail"] == "Укажите ключ API в настройках Jarvis AI"
    )
    assert client.post("/api/agent/stop").json() == {"ok": True}
