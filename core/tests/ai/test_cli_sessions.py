"""Удалили чат — у программ-провайдеров (Claude Code, Gemini CLI, Qwen Code) удаляются и файлы его сессии."""

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis.ai.cli.backend import session_files
from jarvis.context import AppContext

SID = "0f8e6a52-3c1d-4b7a-9e2f-1a2b3c4d5e6f"
OTHER = "aa11bb22-0000-4000-8000-000000000000"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(root))
    for name in ("CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME"):
        monkeypatch.delenv(name, raising=False)
    return root


def _write(path: Path, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _backend(ctx: AppContext, provider: str) -> Any:
    backend = ctx.ai.cli.get(provider)  # type: ignore[attr-defined]
    assert backend is not None
    return backend


def test_claude_session_files_removed_with_chat(ctx: AppContext, home: Path) -> None:
    project = home / ".claude" / "projects" / "C--Users-me-AppData-Local-Jarvis-cli-claude"
    mine = _write(project / f"{SID}.jsonl", '{"type":"user"}')
    sub = _write(project / SID / "subagents" / "a.jsonl")
    todo = _write(home / ".claude" / "todos" / f"{SID}-agent-{SID}.json")
    other = _write(project / f"{OTHER}.jsonl")
    ctx.db.kv_set("cli:claude_code:session:chat1", {"id": SID, "turns": 2})
    _backend(ctx, "claude_code").forget_chat("chat1")  # без запущенного цикла — сразу
    assert not mine.exists() and not sub.parent.exists() and not todo.exists() and other.exists()
    assert ctx.db.kv_get("cli:claude_code:session:chat1") is None


def test_gemini_and_qwen_sessions_and_logs(ctx: AppContext, home: Path) -> None:
    chats = home / ".gemini" / "tmp" / "abc123" / "chats"
    mine = _write(chats / f"session-2026-10-18T10-00-{SID[:8]}.json", json.dumps({"sessionId": SID}))
    same_prefix = _write(chats / f"session-2026-10-18T11-00-{SID[:8]}.json", json.dumps({"sessionId": "x"}))
    logs = _write(
        home / ".gemini" / "tmp" / "abc123" / "logs.json",
        json.dumps([{"sessionId": SID, "message": "секрет"}, {"sessionId": OTHER, "message": "чужое"}]),
    )
    assert session_files("gemini_cli", SID) == [mine]
    assert _backend(ctx, "gemini_cli").forget_session(SID) == 2
    assert not mine.exists() and same_prefix.exists()
    assert json.loads(logs.read_text(encoding="utf-8")) == [{"sessionId": OTHER, "message": "чужое"}]

    qwen = _write(home / ".qwen" / "projects" / "jarvis-cli-qwen" / "chats" / f"{SID}.jsonl")
    assert _backend(ctx, "qwen_code").forget_session(SID) == 1 and not qwen.exists()


def test_unsafe_session_ids_are_ignored(home: Path) -> None:
    _write(home / ".claude" / "projects" / "p" / "x.jsonl")
    assert session_files("claude_code", "*") == [] and session_files("claude_code", "../../etc") == []
    assert session_files("unknown", SID) == []
