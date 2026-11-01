"""Фоновые задачи у провайдеров по подписке: программам включаются только их веб-поиск и чтение страниц,
Codex — свой веб-поиск; задача идёт через их run_agent с Host вместо агента."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from jarvis.ai.cli.specs import KINDS, CliRun
from jarvis.background import runner
from jarvis.background.model import Task
from tests.background.conftest import service


def _run(tmp_path: Path, web: bool) -> CliRun:
    return CliRun(
        work_dir=tmp_path,
        prompt="задача",
        system="system",
        mcp_url="http://127.0.0.1:1/api/ai/mcp",
        mcp_headers={"X-Jarvis-Token": "t", "X-Jarvis-Turn": "u"},
        web=web,
    )


def test_claude_code_gets_only_web_tools_for_tasks(tmp_path: Path) -> None:
    claude = KINDS["claude_code"]()
    plain = claude.args(_run(tmp_path, False))
    assert plain[plain.index("--tools") + 1] == ""
    args = claude.args(_run(tmp_path, True))
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert allowed == ["mcp__jarvis__*", "WebSearch", "WebFetch"]
    assert "Bash" not in ",".join(args)


def test_gemini_cli_web_tools_only_in_task_folder(tmp_path: Path) -> None:
    gemini = KINDS["gemini_cli"]()
    gemini.prepare_dir(tmp_path / "chat", tools=True)
    gemini.prepare_dir(tmp_path / "task", tools=True, web=True)
    chat = json.loads((tmp_path / "chat" / ".gemini" / "settings.json").read_text("utf-8"))["tools"]
    task = json.loads((tmp_path / "task" / ".gemini" / "settings.json").read_text("utf-8"))["tools"]
    assert "google_web_search" in chat["exclude"] and "google_web_search" not in chat["core"]
    assert {"google_web_search", "web_fetch"} <= set(task["core"])
    assert not {"google_web_search", "web_fetch"} & set(task["exclude"])
    assert {"run_shell_command", "write_file"} <= set(task["exclude"])


def test_qwen_code_gets_web_fetch_for_tasks(tmp_path: Path) -> None:
    qwen = KINDS["qwen_code"]()
    args = qwen.args(_run(tmp_path, True))
    assert "web_fetch" in args[args.index("--core-tools") + 1].split(",")
    assert "web_fetch" not in args[args.index("--exclude-tools") + 1].split(",")
    assert "web_fetch" in args[args.index("--allowed-tools") + 1]
    plain = qwen.args(_run(tmp_path, False))
    assert "web_fetch" in plain[plain.index("--exclude-tools") + 1].split(",")


class FakeBackend:
    """Бэкенд подписки: зовёт действия так, как их звала бы программа, — через host._act и finish."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.kwargs: dict[str, Any] = {}

    def problem(self) -> str | None:
        return None

    async def run_agent(self, host: Any, run: Any, **kwargs: Any) -> str:
        from jarvis.ai.llm import ToolCall

        self.kwargs = kwargs
        await host._act(run, ToolCall("c1", "report", json.dumps({"text": "Нашёл"})))
        args = {"status": "done", "summary": "Готово"}
        assert await host.check_finish(run, args) is None
        return host.finish_summary(run, args)


async def test_subscription_paths_use_backend_with_task_rules(bg: Any, monkeypatch: Any) -> None:
    ctx, _, _ = bg
    for provider in ("chatgpt", "claude_code"):
        backend = FakeBackend(provider)
        monkeypatch.setattr(ctx.ai, "account_backend", lambda b=backend: b)
        ctx.settings.patch({"ai": {"provider": provider}})
        task = Task("s" + provider.replace("_", ""), "Собери новости")
        session = await runner.run_session(service(ctx), task)
        assert session.outcome == "done" and session.summary == "Готово"
        names = {s["function"]["name"] for s in backend.kwargs["specs"]}
        assert "web_search" not in names  # поиск — свой у Codex и Claude Code
        if provider == "chatgpt":
            assert backend.kwargs["web_search"] is True and backend.kwargs["rules"] == runner.CODEX_TASK_RULES
            assert "web_read" in names
        else:
            assert backend.kwargs["web"] is True and backend.kwargs["wrap_system"] is False
            assert backend.kwargs["system"].startswith(runner.CLI_TASK_RULES)
            assert "web_read" not in names


def test_api_routes(client: TestClient, tmp_path: Path) -> None:
    ctx = client.ctx  # type: ignore[attr-defined]
    vault = tmp_path / "vault"
    vault.mkdir()
    client.patch("/api/settings", json={"background": {"vault": str(vault)}})
    state = client.get("/api/background/vault").json()
    assert state["path"] == str(vault) and state["obsidian"] is False and state["folder"] == "Jarvis"
    assert client.post("/api/background/vault/setup").json()["exists"] is True
    assert (vault / "Jarvis" / "Инструкции.md").exists()

    # ИИ не настроен — задачу не поставить, понятной ошибкой
    response = client.post("/api/background", json={"brief": "Собери новости"})
    assert response.status_code == 400 and "ключ" in response.json()["detail"].lower()
    assert client.get("/api/background").json() == {"items": []}
    assert client.get("/api/background/nope").status_code == 404

    task = Task("t1", "Собери новости", title="Новости", status="partial", summary="Сделано не всё")
    service(ctx)._tasks[task.id] = task
    assert client.get("/api/background").json()["items"][0]["title"] == "Новости"
    full = client.get("/api/background/t1").json()["task"]
    assert full["brief"] == "Собери новости" and full["card"]["status"] == "partial"
    assert client.post("/api/background/t1/answer", json={"text": "да"}).status_code == 400  # вопросов нет
    assert client.delete("/api/background/t1").json() == {"ok": True}
    assert client.get("/api/background").json() == {"items": []}
