"""Приватность: маскировка секретов (журналы), очистка по срокам хранения, «Удалить данные», журналы, API."""

import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis import paths
from jarvis.ai import lessons
from jarvis.context import AppContext
from jarvis.log import RedactFilter
from jarvis.redact import has_secret, luhn_ok, redact
from jarvis.retention import DAY_S, TOOL_PLACEHOLDER, Retention
from jarvis.services import UserFacingError
from tests.conftest import EventLog

NOW = time.time()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Моя карта 4111 1111 1111 1111, CVV 123", "Моя карта •••• 1111, CVV •••"),
        ("оплачу 5555555555554444", "оплачу •••• 4444"),
        ("время 1760000000123", "время 1760000000123"),  # метка времени — не карта
        ("счёт 40817810099910004312", "счёт 40817810099910004312"),  # 20 цифр — не карта
        ("пароль: qwerty123 и ещё", "пароль: ••• и ещё"),
        ("Пароль-то я забыл", "Пароль-то я забыл"),
        ("мой password = hunter2!", "мой password = •••"),
        ("пин-код 1234", "пин-код •••"),
        ('{"password": "s3cr3tpass", "user": "anna"}', '{"password": "•••", "user": "anna"}'),
        ("ключ sk-proj-abcdefghijklmnopqrstuvwx123", "ключ sk-proj-•••"),
        ("ElevenLabs sk_0123456789abcdef0123456789abcdef0123456789abcdef", "ElevenLabs sk_•••"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "Authorization: Bearer •••"),
        ("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "token=ghp_•••"),
        ("TELEGRAM_SESSION_STRING=1BVtsOKkBu3v8Xyz-abcdefg", "TELEGRAM_SESSION_STRING=•••"),
        ("бот 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1", "бот 123456789:•••"),
        ("max_tokens: 12345678", "max_tokens: 12345678"),
        ("обычный текст 2026 года", "обычный текст 2026 года"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert has_secret(text) is (text != expected)


def test_luhn() -> None:
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


def test_log_filter_masks_message_and_traceback() -> None:
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactFilter())
    logger = logging.getLogger("jarvis.test.redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("ключ %s не подошёл", "sk-abcdefghijklmnopqrstuvwxyz")
        try:
            raise RuntimeError("пароль: hunter2 неверный")
        except RuntimeError:
            logger.exception("вход не удался")
    finally:
        logger.removeHandler(handler)
    out = buffer.getvalue()
    assert "sk-•••" in out and "abcdefghijkl" not in out
    assert "пароль: •••" in out and "hunter2" not in out and "Traceback" in out


# --- очистка по срокам -------------------------------------------------------------------------------------


@pytest.fixture
def keeper(ctx: AppContext) -> Retention:
    service = ctx.retention
    service.clock = lambda: NOW
    return service


def _history(ctx: AppContext, ts: float, text: str) -> None:
    ctx.db.insert(
        "history",
        {"ts": ts, "source": "voice", "text": text, "kind": "command", "title": text, "status": "ok"},
    )


def _chat(ctx: AppContext, kind: str = "chat", *, age_days: float = 0, pinned: bool = False) -> str:
    chat = ctx.ai.chats.create(title="Тест", kind=kind)  # type: ignore[attr-defined]
    ctx.db.update("chats", chat["id"], {"updated": NOW - age_days * DAY_S, "pinned": int(pinned)})
    return str(chat["id"])


def _message(ctx: AppContext, chat_id: str, role: str, content: str, *, age_days: float, **kw: Any) -> str:
    row = ctx.ai.chats.add_message(chat_id, role, content, **kw)  # type: ignore[attr-defined]
    ctx.db.update("messages", row["id"], {"created": NOW - age_days * DAY_S})
    return str(row["id"])


def _old_file(path: Path, age_days: float, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = NOW - age_days * DAY_S
    os.utime(path, (ts, ts))
    return path


async def test_purge_by_retention_settings(ctx: AppContext, keeper: Retention, events: EventLog) -> None:
    ctx.settings.patch({"privacy": {"history_days": 30, "chat_days": 60, "voice_chat_days": 7}})
    _history(ctx, NOW - 40 * DAY_S, "старая")
    _history(ctx, NOW - DAY_S, "свежая")
    old_chat = _chat(ctx, age_days=90)
    pinned = _chat(ctx, age_days=90, pinned=True)
    fresh = _chat(ctx, age_days=1)
    voice = _chat(ctx, "voice")
    _message(ctx, voice, "user", "давно", age_days=10)
    kept_voice = _message(ctx, voice, "user", "вчера", age_days=1)
    tool = _message(ctx, fresh, "tool", "x" * 500, age_days=40, meta={"hidden": True}, tool_call_id="c1")
    recent_tool = _message(
        ctx, fresh, "tool", "y" * 500, age_days=1, meta={"hidden": True}, tool_call_id="c2"
    )
    picture = _message(
        ctx, fresh, "user", "что на фото?", age_days=40, meta={"images": ["data:image/png;base64,AAA"]}
    )
    summaries = paths.data_dir() / "summaries"
    _old_file(summaries / "old.wav", 3)
    _old_file(summaries / "new.wav", 0)
    logs = paths.logs_dir()
    _old_file(logs / "mcp-gone.log", 30, b"old log")
    _old_file(logs / "core.log.2", 30)
    _old_file(logs / "crash.log", 30, b"crash")
    _old_file(logs / "fresh.log", 0, b"fresh")
    ctx.notes.memory_add("Сегодня работаю из дома", kind="temp")  # type: ignore[call-arg]
    ctx.db.execute("UPDATE memory SET expires=?", (NOW - 10,))

    removed = await keeper.purge()

    assert removed == {
        "memory": 1,
        "history": 1,
        "chats": 1,
        "voice_chats": 1,
        "tool_results": 2,
        "summaries": 1,
        "logs": 3,
    }
    assert [h["text"] for h in ctx.db.query("SELECT text FROM history")] == ["свежая"]
    chats = {c["id"] for c in ctx.db.query("SELECT id FROM chats")}
    assert old_chat not in chats and {pinned, fresh, voice} <= chats
    assert [m["id"] for m in ctx.db.query("SELECT id FROM messages WHERE chat_id=?", (voice,))] == [
        kept_voice
    ]
    rows = {r["id"]: r for r in ctx.db.query("SELECT id, content, meta FROM messages")}
    assert rows[tool]["content"] == TOOL_PLACEHOLDER and rows[recent_tool]["content"] == "y" * 500
    meta = json.loads(rows[picture]["meta"])
    assert "images" not in meta and meta["images_removed"] == 1 and rows[picture]["content"] == "что на фото?"
    assert not (summaries / "old.wav").exists() and (summaries / "new.wav").exists()
    assert not (logs / "mcp-gone.log").exists() and not (logs / "core.log.2").exists()
    assert (logs / "crash.log").read_bytes() == b"" and (logs / "fresh.log").read_bytes() == b"fresh"
    assert events.of("data.purged")[-1]["removed"] == removed
    assert {"id": old_chat} in events.of("chat.deleted")
    # сутки не прошли — не пора; повторная очистка ничего не находит
    assert not keeper.due()
    assert await keeper.purge() == {}


async def test_purge_respects_zero_and_keep_audio(ctx: AppContext, keeper: Retention) -> None:
    ctx.settings.patch(
        {
            "privacy": {"history_days": 0, "audio_days": 1},
            "plugins": {"summary": {"config": {"keep_audio": True}}},
        }
    )
    _history(ctx, NOW - 3000 * DAY_S, "очень старая")
    wav = _old_file(paths.data_dir() / "summaries" / "kept.wav", 30)
    await keeper.purge()
    assert len(ctx.db.query("SELECT * FROM history")) == 1 and wav.exists()


async def test_big_log_keeps_tail(ctx: AppContext, keeper: Retention) -> None:
    lines = b"".join(f"строка {i}\n".encode() for i in range(400_000))
    log = _old_file(paths.logs_dir() / "mcp-big.log", 0, lines)
    await keeper.purge()
    data = log.read_bytes()
    assert (
        0 < len(data) <= 1_000_000
        and data.endswith("строка 399999\n".encode())
        and data.startswith("строка".encode())
    )


# --- «Удалить данные» ------------------------------------------------------------------------------------


async def test_clear_all_data(ctx: AppContext, keeper: Retention, events: EventLog) -> None:
    _history(ctx, NOW, "свежая")
    chat = _chat(ctx)
    _message(ctx, chat, "user", "привет", age_days=0)
    voice = _chat(ctx, "voice")
    ctx.db.kv_set("voice_chat:voice", voice)
    ctx.notes.memory_add("Живёт в Казани")
    ctx.notes.note_add("Заметка")
    ctx.notes.task_add("Задача")
    ctx.db.kv_set("clipboard:items", [{"id": "1", "text": "скопировано"}])
    ctx.db.insert("usage", {"ts": NOW, "provider": "openai", "model": "x", "prompt_tokens": 1})
    ctx.secrets.set("ai_api_key", "sk-test")
    lessons.add(ctx.db, "telegram.exe", "Поиск чатов — Ctrl+K: кнопка поиска не нажимается")
    models = paths.models_dir() / "piper"
    models.mkdir(parents=True)
    (models / "voice.onnx").write_bytes(b"model")
    _old_file(paths.data_dir() / "screenshots" / "shot.png", 0)
    _old_file(paths.logs_dir() / "core.log.1", 0)
    _old_file(paths.cache_dir() / "tts" / "ok.pcm", 0)

    with pytest.raises(UserFacingError):
        await keeper.clear(["models"])
    cleared = await keeper.clear(["all"])

    assert cleared["history"] == 1 and cleared["chats"] == 1 and cleared["voice_chats"] == 1
    assert cleared["memory"] == 1 and cleared["notes"] == 1 and cleared["tasks"] == 1
    assert cleared["clipboard"] == 1 and cleared["usage"] == 1 and cleared["secrets"] == 1
    assert cleared["screenshots"] == 1 and cleared["voice_cache"] == 1 and cleared["agent_notes"] == 1
    assert lessons.for_app(ctx.db, "telegram.exe") == []
    for table in ("history", "chats", "messages", "memory", "notes", "tasks", "usage"):
        assert ctx.db.query(f"SELECT * FROM {table}") == [], table
    assert ctx.db.kv_get("voice_chat:voice") is None and ctx.db.kv_get("clipboard:items") is None
    assert ctx.secrets.get("ai_api_key") == "" and (models / "voice.onnx").exists()
    assert not (paths.logs_dir() / "core.log.1").exists()
    assert events.of("history.cleared") and events.of("memory.cleared") and events.of("data.cleared")
    assert {"op": "clear"} in events.of("notes.changed")


def test_inventory_counts(ctx: AppContext, keeper: Retention) -> None:
    _history(ctx, NOW, "раз")
    _history(ctx, NOW, "два")
    ctx.notes.memory_add("Живёт в Казани")
    lessons.add(ctx.db, "telegram.exe", "Поиск чатов — Ctrl+K")
    _old_file(paths.data_dir() / "summaries" / "a.wav", 0, b"1234")
    _old_file(paths.cache_dir() / "tts" / "ok.pcm", 0, b"123456")  # звук «Готово, сэр.»
    data = keeper.inventory()
    items = {i["id"]: i for i in data["items"]}
    assert items["voice_cache"] == {**items["voice_cache"], "count": 1, "bytes": 6, "clearable": True}
    assert items["history"]["count"] == 2 and items["history"]["bytes"] > 0
    assert items["memory"]["count"] == 1
    assert items["agent_notes"]["count"] == 1 and items["agent_notes"]["bytes"] > 0
    assert items["summaries"] == {**items["summaries"], "count": 1, "bytes": 4}
    assert items["models"]["clearable"] is False and items["secrets"]["clearable"] is True
    assert data["db_bytes"] > 0 and data["dir"] == str(paths.data_dir())


def test_data_api(client: TestClient) -> None:
    inventory = client.get("/api/data/inventory").json()
    assert {i["id"] for i in inventory["items"]} >= {"history", "chats", "memory", "logs", "models"}
    client.post("/api/memory", json={"text": "Любит джаз"})
    res = client.post("/api/data/clear", json={"scopes": ["memory"]}).json()
    assert res == {"ok": True, "cleared": {"memory": 1}}
    assert client.post("/api/data/clear", json={"scopes": ["models"]}).status_code == 400
    assert client.post("/api/data/clear", json={"scopes": []}).status_code == 422
    assert client.post("/api/data/purge").json()["ok"] is True
    (paths.logs_dir() / "mcp-x.log").write_text("журнал", encoding="utf-8")
    logs = client.get("/api/logs").json()
    assert logs["dir"] == str(paths.logs_dir()) and any(i["name"] == "mcp-x.log" for i in logs["items"])
    assert client.delete("/api/logs").json()["ok"] is True
    assert (paths.logs_dir() / "mcp-x.log").read_text(encoding="utf-8") == ""
