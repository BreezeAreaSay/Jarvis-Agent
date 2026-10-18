"""Telegram-бот «PC Control» на поддельном Bot API (httpx.MockTransport): привязка владельца, чужие, меню,
кнопки → платформа, текст и голосовые → Jarvis, опрос getUpdates."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jarvis.context import AppContext
from jarvis.remote import telegram
from jarvis.remote.service import Remote
from jarvis.remote.telegram import TelegramBot, chunks, format_reply
from jarvis.services import ExecResult

TOKEN = "123456:TEST-token"
OWNER = 777
STRANGER = 999


class FakeTelegram:
    """Bot API в памяти: отвечает на методы и записывает вызовы; getUpdates отдаёт очередь."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {"voice/file_1.oga": b"OggS-fake-opus"}
        self.errors: dict[str, tuple[int, str]] = {}
        self.message_id = 100
        self.uploads: list[bytes] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith(("/bot", "/file/bot")), path
        if path.startswith("/file/bot"):
            name = path.split("/", 3)[3]
            if name in self.files:
                return httpx.Response(200, content=self.files[name])
            return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})
        method = path.rsplit("/", 1)[1]
        if request.headers.get("content-type", "").startswith("multipart/"):
            self.uploads.append(request.content)
            payload: dict[str, Any] = {}
        else:
            payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method in self.errors:
            code, description = self.errors[method]
            return httpx.Response(code, json={"ok": False, "error_code": code, "description": description})
        return httpx.Response(200, json={"ok": True, "result": await self._result(method, payload)})

    async def _result(self, method: str, payload: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": 123456, "is_bot": True, "first_name": "PC", "username": "jarvis_pc_bot"}
        if method == "getUpdates":
            if self.updates:
                batch, self.updates = self.updates, []
                return batch
            if payload.get("timeout"):
                await asyncio.sleep(0.05)  # долгий опрос
            return []
        if method in ("sendMessage", "sendPhoto"):
            self.message_id += 1
            return {"message_id": self.message_id, "chat": {"id": payload.get("chat_id")}}
        if method == "getFile":
            return {"file_id": payload["file_id"], "file_path": "voice/file_1.oga"}
        return True

    def of(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def texts(self) -> list[str]:
        return [p["text"] for name, p in self.calls if name in ("sendMessage", "editMessageText")]


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def bot(ctx: AppContext, fake: FakeTelegram) -> TelegramBot:
    remote = ctx.remote
    assert isinstance(remote, Remote)
    remote.telegram.transport = httpx.MockTransport(fake.handler)
    remote.telegram.connect(TOKEN)
    return remote.telegram


@pytest.fixture
def owned(ctx: AppContext) -> None:
    ctx.settings.patch({"remote": {"telegram_owner_id": OWNER, "telegram_enabled": True}})


def message(text: str = "", user: int = OWNER, **extra: Any) -> dict[str, Any]:
    return {
        "update_id": int(time.time() * 1000) % 10**9,
        "message": {
            "message_id": 5,
            "date": int(time.time()),
            "chat": {"id": user, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Тони"},
            "text": text,
            **extra,
        },
    }


def callback(data: str, user: int = OWNER) -> dict[str, Any]:
    return {
        "update_id": 1,
        "callback_query": {
            "id": f"cq-{data}",
            "from": {"id": user, "is_bot": False, "first_name": "Тони"},
            "data": data,
            "message": {"message_id": 42, "chat": {"id": user, "type": "private"}},
        },
    }


def buttons(payload: dict[str, Any]) -> list[str]:
    return [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]


class FakeDispatcher:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def handle(self, text: str, source: str, *, speak: bool | None = None) -> dict[str, Any]:
        self.calls.append((text, source))
        return self.result


async def wait_for(check: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.02)


# --- привязка и чужие ---------------------------------------------------------------------------------


async def test_pairing_binds_owner(ctx: AppContext, bot: TelegramBot, fake: FakeTelegram) -> None:
    remote = ctx.remote
    assert isinstance(remote, Remote)
    code = remote.pair()["pairing_code"]
    await bot.handle_update(message(f"/start {code}", user=OWNER))
    assert ctx.settings.value.remote.telegram_owner_id == OWNER
    sent = fake.of("sendMessage")
    assert "Пульт привязан" in sent[0]["text"]
    menu = sent[1]
    assert "Главное" in menu["text"] and menu["parse_mode"] == "HTML"
    assert buttons(menu) == ["nav:media", "nav:system", "nav:commands", "do:main:screenshot", "nav:ai"]
    assert remote.info()["telegram"]["pairing_code"] == ""


async def test_wrong_code_and_unpaired_bot(ctx: AppContext, bot: TelegramBot, fake: FakeTelegram) -> None:
    await bot.handle_update(message("/start 000000", user=STRANGER))
    assert fake.texts()[-1] == telegram.BAD_CODE
    await bot.handle_update(message("привет", user=STRANGER))
    assert fake.texts()[-1] == telegram.NOT_PAIRED
    assert ctx.settings.value.remote.telegram_owner_id == 0


async def test_foreign_user_rejected(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    dispatcher = FakeDispatcher({"kind": "unknown"})
    ctx.dispatcher = dispatcher  # type: ignore[assignment]
    await bot.handle_update(message("выключи компьютер", user=STRANGER))
    assert fake.texts() == ["Это личный пульт. Доступ закрыт."]
    await bot.handle_update(message("ну пожалуйста", user=STRANGER))
    assert len(fake.of("sendMessage")) == 1  # чужим отвечаем не чаще раза в 10 минут

    await bot.handle_update(callback("do:system:shutdown_yes:" + str(int(time.time())), user=STRANGER))
    answer = fake.of("answerCallbackQuery")[-1]
    assert answer["text"] == "Это личный пульт. Доступ закрыт." and answer["show_alert"] is True
    platform: Any = ctx.platform
    assert platform.calls == [] and dispatcher.calls == []

    # код привязки от чужого — только если он действительно выдан в приложении
    await bot.handle_update(message("/start 123456", user=STRANGER))
    assert ctx.settings.value.remote.telegram_owner_id == OWNER


async def test_group_chats_ignored(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    update = message("/menu")
    update["message"]["chat"] = {"id": -100, "type": "group"}
    await bot.handle_update(update)
    assert fake.calls == []


# --- меню и кнопки ------------------------------------------------------------------------------------


async def test_menu_navigation_edits_message(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    await bot.handle_update(message("/menu"))
    assert buttons(fake.of("sendMessage")[-1])[0] == "nav:media"

    await bot.handle_update(callback("nav:media"))
    edit = fake.of("editMessageText")[-1]
    assert edit["message_id"] == 42 and edit["chat_id"] == OWNER
    assert "Главное › Медиа" in edit["text"]
    assert "Night Drive — Neon Coast (Spotify)" in edit["text"] and "Громкость: 40%" in edit["text"]
    assert buttons(edit) == [
        "do:media:prev",
        "do:media:play_pause",
        "do:media:next",
        "do:media:volume_down",
        "do:media:mute",
        "do:media:volume_up",
        "nav:main",
    ]
    labels = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["⏮ Назад", "⏯ Пауза", "⏭ Дальше", "🔉 Тише", "🔇 Звук", "🔊 Громче", "← Назад"]
    assert fake.of("answerCallbackQuery")[-1] == {"callback_query_id": "cq-nav:media"}

    await bot.handle_update(callback("nav:system"))
    system = fake.of("editMessageText")[-1]
    assert "Главное › Система" in system["text"] and "Процессор" in system["text"]
    assert "do:system:shutdown" in buttons(system) and "do:system:screenshot" in buttons(system)

    await bot.handle_update(callback("nav:ai"))
    assert "Jarvis AI" in fake.of("editMessageText")[-1]["text"]
    await bot.handle_update(callback("nav:main"))
    assert "Главное</b>" in fake.of("editMessageText")[-1]["text"]


async def test_media_buttons_call_platform(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    platform: Any = ctx.platform
    await bot.handle_update(callback("do:media:play_pause"))
    assert ("media_key", ("play_pause",)) in platform.calls
    assert "Выполнено: Пауза" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(callback("do:media:next"))
    assert ("media_key", ("next",)) in platform.calls
    assert "Выполнено: Следующий трек" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(callback("do:media:volume_up"))
    assert platform.volume == 50
    assert "Выполнено: Громче · Громкость 50%" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(callback("do:media:mute"))
    assert platform.muted is True
    assert "Звук выключен" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(callback("do:media:explode"))
    assert "Ошибка:" in fake.of("editMessageText")[-1]["text"]


async def test_shutdown_needs_fresh_confirmation(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    platform: Any = ctx.platform
    await bot.handle_update(callback("do:system:shutdown"))
    confirm = fake.of("editMessageText")[-1]
    assert "Выключить компьютер?" in confirm["text"]
    yes = buttons(confirm)[0]
    assert yes.startswith("do:system:shutdown_yes:") and buttons(confirm)[1] == "nav:system"
    assert not any(c[0] == "power" for c in platform.calls)

    await bot.handle_update(callback("do:system:shutdown_yes:" + str(int(time.time()) - 600)))
    assert not any(c[0] == "power" for c in platform.calls)
    assert "устарело" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(callback(yes))
    assert ("power", ("shutdown", 0)) in platform.calls
    assert "Выполнено: Выключение" in fake.of("editMessageText")[-1]["text"]


async def test_system_buttons(ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram) -> None:
    platform: Any = ctx.platform
    await bot.handle_update(callback("do:system:minimize_all"))
    assert ("window_action", ("minimize_all", None)) in platform.calls
    assert "Выполнено: Свернуть всё" in fake.of("editMessageText")[-1]["text"]
    await bot.handle_update(callback("do:system:lock"))
    await bot.handle_update(callback("do:system:sleep"))
    assert ("power", ("lock", 0)) in platform.calls and ("power", ("sleep", 0)) in platform.calls


async def test_screenshot_is_sent_as_photo(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    platform: Any = ctx.platform
    await bot.handle_update(callback("do:main:screenshot"))
    assert ("screenshot", (None,)) in platform.calls
    assert fake.of("sendPhoto") == [{}] and len(fake.uploads) == 1
    upload = fake.uploads[0]
    assert b'name="photo"' in upload and b"image/jpeg" in upload and b'name="chat_id"' in upload
    assert "Выполнено: Скриншот" in fake.of("editMessageText")[-1]["text"]

    await bot.handle_update(message("/screen"))
    assert len(fake.uploads) == 2


async def test_commands_menu_runs_favorites(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    dispatcher = FakeDispatcher(
        {"kind": "command", "result": ExecResult(True, "Время", "Сейчас 14:32").to_json()}
    )
    ctx.dispatcher = dispatcher  # type: ignore[assignment]
    await bot.handle_update(callback("nav:commands"))
    menu = fake.of("editMessageText")[-1]
    assert "Главное › Команды" in menu["text"]
    first = buttons(menu)[0]
    assert first.startswith("do:commands:") and len(first.encode()) <= 64
    await bot.handle_update(callback(first))
    assert dispatcher.calls == [("который час", "telegram")]
    assert "Выполнено: Который час · Сейчас 14:32" in fake.of("editMessageText")[-1]["text"]


async def test_edit_falls_back_to_new_message(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    fake.errors["editMessageText"] = (400, "Bad Request: message to edit not found")
    await bot.handle_update(callback("nav:media"))
    assert "Главное › Медиа" in fake.of("sendMessage")[-1]["text"]
    fake.errors["editMessageText"] = (400, "Bad Request: message is not modified")
    await bot.handle_update(callback("nav:media"))  # не ошибка


# --- текст и голос ------------------------------------------------------------------------------------


async def test_text_goes_to_jarvis(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram
) -> None:
    dispatcher = FakeDispatcher(
        {"kind": "ai", "chat_id": "c1", "text": "Завтра в Москве +12° и дождь.", "error": ""}
    )
    ctx.dispatcher = dispatcher  # type: ignore[assignment]
    await bot.handle_update(message("какая погода завтра"))
    assert dispatcher.calls == [("какая погода завтра", "telegram")]
    assert fake.texts()[-1] == "Завтра в Москве +12° и дождь."
    assert fake.of("sendChatAction")[0] == {"chat_id": OWNER, "action": "typing"}

    dispatcher.result = {
        "kind": "command",
        "result": ExecResult(False, "Открыть Spotify", "Spotify не найден").to_json(),
    }
    await bot.handle_update(message("открой спотифай"))
    assert fake.texts()[-1] == "❌ Открыть Spotify\nSpotify не найден"


async def test_voice_without_ffmpeg(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telegram.audio, "find_ffmpeg", lambda: None)
    voice = {"file_id": "v1", "duration": 3, "mime_type": "audio/ogg"}
    await bot.handle_update(message("", voice=voice))
    assert fake.texts()[-1] == "Голосовые пока не поддерживаются — пришлите текстом"
    assert fake.of("getFile") == []


async def test_voice_is_transcribed_and_dispatched(
    ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np

    decoded: list[bytes] = []

    def decoder(data: bytes) -> Any:
        decoded.append(data)
        return np.zeros(16000, dtype=np.float32)

    heard: list[tuple[int, int]] = []

    async def transcribe(samples: Any, rate: int, *, language: str = "ru") -> str:
        heard.append((len(samples), rate))
        return "сверни все окна"

    bot.decoder = decoder
    monkeypatch.setattr(ctx.voice, "transcribe", transcribe)
    dispatcher = FakeDispatcher(
        {"kind": "command", "result": ExecResult(True, "Свернуть все окна").to_json()}
    )
    ctx.dispatcher = dispatcher  # type: ignore[assignment]

    await bot.handle_update(message("", voice={"file_id": "v1", "duration": 2}))
    assert fake.of("getFile") == [{"file_id": "v1"}]
    assert decoded == [b"OggS-fake-opus"] and heard == [(16000, 16000)]
    assert dispatcher.calls == [("сверни все окна", "telegram")]
    assert fake.texts()[-2:] == ["🎙 «сверни все окна»", "✅ Свернуть все окна"]

    await bot.handle_update(message("", voice={"file_id": "v2", "duration": 600}))
    assert "слишком длинное" in fake.texts()[-1]


# --- накопившиеся обновления и опрос ------------------------------------------------------------------


async def test_backlog_is_safe(ctx: AppContext, owned: None, bot: TelegramBot, fake: FakeTelegram) -> None:
    platform: Any = ctx.platform
    dispatcher = FakeDispatcher({"kind": "unknown"})
    ctx.dispatcher = dispatcher  # type: ignore[assignment]
    await bot.handle_update(callback("do:system:sleep"), backlog=True)
    assert platform.calls == []
    assert "устарела" in fake.of("answerCallbackQuery")[-1]["text"]

    old = message("выключи музыку")
    old["message"]["date"] = int(time.time()) - 3600
    await bot.handle_update(old, backlog=True)
    assert dispatcher.calls == []
    fresh = message("выключи музыку")
    await bot.handle_update(fresh, backlog=True)
    assert dispatcher.calls == [("выключи музыку", "telegram")]


async def test_polling_loop(ctx: AppContext, owned: None, fake: FakeTelegram) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    remote = ctx.remote
    assert isinstance(remote, Remote)
    bot = remote.telegram
    bot.transport = httpx.MockTransport(fake.handler)
    statuses: list[str] = []
    ctx.bus.on("remote.changed", lambda e: statuses.append(e.data["telegram"]["status"]))
    platform: Any = ctx.platform

    stale_press = callback("do:media:next")
    stale_press["update_id"] = 10
    fake.updates = [stale_press]
    await bot.apply(True, TOKEN, "")
    try:
        await wait_for(lambda: bot.status == "online" and fake.of("answerCallbackQuery") != [])
        assert bot.username == "jarvis_pc_bot"
        assert (
            "media_key",
            ("next",),
        ) not in platform.calls  # нажатие, накопившееся до запуска, не выполняется
        assert fake.of("setMyCommands")[0]["commands"][0]["command"] == "menu"

        await wait_for(lambda: any(p.get("timeout") == telegram.POLL_TIMEOUT for p in fake.of("getUpdates")))
        press = callback("do:media:next")
        press["update_id"] = 11
        fake.updates = [press]
        await wait_for(lambda: ("media_key", ("next",)) in platform.calls)
        await wait_for(lambda: ctx.db.kv_get("remote:telegram_offset:123456") == 12)
        assert fake.of("getUpdates")[-1]["offset"] == 12 or fake.of("getUpdates")[-1]["offset"] == 11
        assert remote.info()["telegram"]["bot"] == "jarvis_pc_bot"
        assert "connecting" in statuses and "online" in statuses
    finally:
        await bot.apply(False, TOKEN, "")
    assert bot.status == "off" and not bot.running


async def test_invalid_token_stops_with_error(ctx: AppContext, fake: FakeTelegram) -> None:
    ctx.loop = asyncio.get_running_loop()
    remote = ctx.remote
    assert isinstance(remote, Remote)
    bot = remote.telegram
    bot.transport = httpx.MockTransport(fake.handler)
    fake.errors["getMe"] = (401, "Unauthorized")
    await bot.apply(True, "1:bad", "")
    await wait_for(lambda: not bot.running)
    assert bot.status == "error" and "Неверный токен" in bot.error
    await bot.stop()


async def test_network_errors_back_off(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    remote = ctx.remote
    assert isinstance(remote, Remote)
    bot = remote.telegram
    attempts: list[float] = []
    delays: list[float] = []

    def offline(request: httpx.Request) -> httpx.Response:
        attempts.append(time.monotonic())
        raise httpx.ConnectError("нет сети", request=request)

    async def fast_sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    bot.transport = httpx.MockTransport(offline)
    bot.sleep = fast_sleep
    await bot.apply(True, TOKEN, "")
    await wait_for(lambda: len(delays) >= 8)
    await bot.stop()
    assert bot.status == "error" and "Нет связи с Telegram" in bot.error
    assert delays[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


async def test_enabled_without_token(ctx: AppContext) -> None:
    remote = ctx.remote
    assert isinstance(remote, Remote)
    await remote.telegram.apply(True, "", "")
    assert remote.telegram.status == "error" and "токен" in remote.telegram.error
    await remote.telegram.apply(False, "", "")
    assert remote.telegram.status == "off" and remote.telegram.error == ""


async def test_secret_change_restarts_bot(running_ctx: AppContext, fake: FakeTelegram) -> None:
    remote = running_ctx.remote
    assert isinstance(remote, Remote)
    remote.telegram.transport = httpx.MockTransport(fake.handler)
    running_ctx.settings.patch({"remote": {"telegram_enabled": True}})
    await wait_for(lambda: remote.telegram.status == "error")  # включён, но токена нет
    running_ctx.secrets.set("telegram_token", TOKEN)
    await wait_for(lambda: remote.telegram.status == "online")
    running_ctx.settings.patch({"remote": {"telegram_owner_id": OWNER}})  # привязка — без перезапуска
    await asyncio.sleep(0.1)
    assert remote.telegram.status == "online" and len(fake.of("getMe")) == 1
    running_ctx.settings.patch({"remote": {"telegram_enabled": False}})
    await wait_for(lambda: remote.telegram.status == "off")


# --- мелочи -------------------------------------------------------------------------------------------


def test_chunks_and_format_reply() -> None:
    text = ("строка " * 50 + "\n") * 40
    parts = chunks(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and " ".join(parts).split() == text.split()
    assert (
        format_reply({"kind": "command", "ok": True, "title": "Пауза", "message": "Выполнено"}) == "✅ Пауза"
    )
    assert format_reply({"kind": "stop", "ok": True}) == "⏹ Остановлено"
    assert format_reply({"kind": "empty"}) == ""
    assert format_reply({"kind": "ai", "ok": False, "message": "Нет ключа"}) == "❌ Нет ключа"
