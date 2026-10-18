"""Фундамент ядра: шина, настройки, секреты, база, текст, диспетчер, API и WebSocket."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis import text
from jarvis.bus import EventBus
from jarvis.confirm import parse_yes_no
from jarvis.context import AppContext
from jarvis.db import Database
from jarvis.services import CommandMatch, ExecResult, MatchedPart
from jarvis.settings import Secrets, SettingsStore
from tests.conftest import TOKEN, EventLog


async def test_bus_delivers_to_subscribers_and_handlers() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe("voice.*")
    seen: list[str] = []
    bus.on("voice.state", lambda e: seen.append(e.data["state"]))
    bus.publish("voice.state", {"state": "listening"})
    bus.publish("chat.delta", {"delta": "x"})
    event = await asyncio.wait_for(sub.get(), 1)
    assert event.type == "voice.state"
    assert seen == ["listening"]
    assert sub.queue.empty()


async def test_bus_publish_from_thread() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe()
    await asyncio.to_thread(bus.publish, "toast", {"title": "x"})
    event = await asyncio.wait_for(sub.get(), 1)
    assert event.data == {"title": "x"}


def test_settings_patch_validates_and_persists(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.patch({"interface": {"accent": "#8B5CF6", "blur": 50}})
    assert store.value.interface.blur == 50
    with pytest.raises(ValidationError):
        store.patch({"interface": {"blur": 500}})
    assert store.value.interface.blur == 50
    again = SettingsStore(tmp_path / "settings.json")
    assert again.value.interface.accent == "#8B5CF6"


def test_settings_bad_file_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{oops", encoding="utf-8")
    store = SettingsStore(path)
    assert store.value.voice.wake_words == ["джарвис"]
    assert (tmp_path / "settings.json.bad").exists()


def test_secrets_roundtrip(tmp_path: Path) -> None:
    secrets = Secrets(tmp_path / "secrets.dat")
    secrets.set("ai_api_key", " sk-test ")
    assert Secrets(tmp_path / "secrets.dat").get("ai_api_key") == "sk-test"
    assert secrets.status()["ai_api_key"] is True
    secrets.set("ai_api_key", "")
    assert secrets.status()["ai_api_key"] is False


def test_db_crud(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    db.upsert("notes", {"id": "n1", "title": "a", "body": "", "created": 1, "updated": 1})
    db.upsert("notes", {"id": "n1", "title": "b", "body": "", "created": 1, "updated": 2})
    assert db.query_one("SELECT title FROM notes WHERE id='n1'") == {"title": "b"}
    db.kv_set("k", {"a": [1, 2]})
    assert db.kv_get("k") == {"a": [1, 2]}
    assert db.delete("notes", "n1") == 1
    db.close()
    # повторное открытие не повторяет миграции
    Database(tmp_path / "x.db").close()


@pytest.mark.parametrize(
    ("phrase", "found", "rest"),
    [
        ("Джарвис, включи музыку", True, "включи музыку"),
        ("джарис включи музыку", True, "включи музыку"),
        ("ну жарвис сверни окна", True, "сверни окна"),
        ("джар вис открой браузер", True, "открой браузер"),
        ("включи музыку", False, "включи музыку"),
        ("дарья позвони", False, "дарья позвони"),
    ],
)
def test_wake_word(phrase: str, found: bool, rest: str) -> None:
    assert text.find_wake(phrase, ["джарвис"]) == (found, rest)


@pytest.mark.parametrize(
    ("phrase", "wake", "found", "rest"),
    [
        # своё слово — с окончаниями, а не только «джарвис»-подобные
        ("пятницу включи музыку", "пятница", True, "включи музыку"),
        ("Пятница, который час", "пятница", True, "который час"),
        ("пятно на рубашке", "пятница", False, "пятно на рубашке"),
        ("пятнадцать минут", "пятница", False, "пятнадцать минут"),
        ("алису позови", "алиса", True, "позови"),
        # фраза из нескольких слов: целиком, склеенная распознаванием и по словам
        ("эй джарвис включи свет", "эй, Джарвис", True, "включи свет"),
        ("эйджарвис включи свет", "эй джарвис", True, "включи свет"),
        ("окей компьютер открой браузер", "окей компьютер", True, "открой браузер"),
        ("акей кампьютер открой браузер", "окей компьютер", True, "открой браузер"),
        ("окей гугл", "окей компьютер", False, "окей гугл"),
        ("эй ты куда", "эй джарвис", False, "эй ты куда"),
        # латиница на слух: Whisper пишет «Jarvis», активатор — «джарвис» (и наоборот)
        ("Jarvis, open YouTube", "джарвис", True, "open YouTube"),
        ("джарвис сверни окна", "Jarvis", True, "сверни окна"),
    ],
)
def test_wake_word_custom_and_phrases(phrase: str, wake: str, found: bool, rest: str) -> None:
    assert text.find_wake(phrase, [wake]) == (found, rest)


def test_normalize_and_stop() -> None:
    assert text.normalize("Громкость на 50%!") == "громкость на 50%"
    assert text.normalize("Ещё раз, пожалуйста…") == "еще раз пожалуйста"
    assert text.is_stop("Стоп!")
    assert not text.is_stop("стоп-кран")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("да", True), ("Да, давай", True), ("нет", False), ("не надо", False), ("может быть", None)],
)
def test_yes_no(answer: str, expected: bool | None) -> None:
    assert parse_yes_no(answer) is expected


async def test_dispatcher_runs_command_and_records_history(ctx: AppContext, events: EventLog) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    part = MatchedPart("media.play", "Включить музыку", "включи музыку", 95.0)

    def match(text: str) -> CommandMatch | None:
        return CommandMatch(text, [part]) if "музык" in text else None

    async def execute(m: CommandMatch, source: str) -> ExecResult:
        return ExecResult(True, "Включить музыку", "Выполнено", icon="spotify")

    ctx.commands.match = match  # type: ignore[method-assign]
    ctx.commands.execute = execute  # type: ignore[method-assign]
    result = await ctx.dispatcher.handle("включи музыку", "text")
    await asyncio.sleep(0)
    assert result["kind"] == "command"
    assert result["result"]["ok"] is True
    rows = ctx.db.query("SELECT * FROM history")
    assert rows[0]["status"] == "ok" and rows[0]["title"] == "Включить музыку"
    assert events.of("toast")[-1]["title"] == "Включить музыку"

    unknown = await ctx.dispatcher.handle("спой песню", "text")
    assert unknown["kind"] == "unknown"


async def test_stop_phrase_stops_speech_and_listening(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    done: list[str] = []

    async def stop_listening() -> None:
        done.append("listening")

    monkeypatch.setattr(ctx.voice, "stop_listening", stop_listening)
    monkeypatch.setattr(ctx.tts, "silence", lambda: done.append("speech"))
    assert (await ctx.dispatcher.handle("Стоп!", "voice"))["kind"] == "stop"
    assert done == ["speech", "listening"]


def test_api_requires_token(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    raw = TestClient(client.app)
    assert raw.get("/api/status").status_code == 401
    assert client.get("/api/status").json()["platform"] == "fake"


def test_api_settings_and_secrets(client: TestClient) -> None:
    r = client.patch("/api/settings", json={"voice": {"prefix_mode": False}})
    assert r.status_code == 200 and r.json()["settings"]["voice"]["prefix_mode"] is False
    assert client.patch("/api/settings", json={"interface": {"blur": -1}}).status_code == 422
    r = client.put("/api/secrets", json={"name": "ai_api_key", "value": "sk-1"})
    assert r.json()["secrets"]["ai_api_key"] is True
    assert client.put("/api/secrets", json={"name": "nope", "value": "1"}).status_code == 400
    body = client.get("/api/settings").json()
    assert "sk-1" not in str(body)


def test_ws_hello_and_events(client: TestClient) -> None:
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["data"]["settings"]["voice"]["wake_words"] == ["джарвис"]
        client.post("/api/dispatch", json={"text": "привет"})
        types = set()
        for _ in range(6):
            msg = ws.receive_json()
            types.add(msg["type"])
            if "dispatch.done" in types:
                break
        assert {"dispatch.started", "history.added"} <= types


def test_ws_rejects_bad_token(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=bad") as ws:
        ws.receive_json()


def test_shutdown_route_asks_server_to_exit(client: TestClient) -> None:
    assert client.post("/api/shutdown").status_code == 409  # без сервера (тестовый клиент) выхода нет
    asked: list[bool] = []
    client.ctx.request_exit = lambda: asked.append(True)  # type: ignore[attr-defined]
    assert client.post("/api/shutdown").json() == {"ok": True} and asked == [True]
