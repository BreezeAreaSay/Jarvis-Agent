"""«Умный буфер»: история буфера (платформа-заглушка), выделенный текст, действия ИИ, палитра, голосовые
команды и API; пометки «не запоминать» и снимок буфера Windows — на подставном pywin32."""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.platform.base import PlatformError, WindowInfo
from jarvis.platform.fake import FakePlatform
from jarvis.platform.windows import WindowsPlatform
from jarvis.platform.windows import system as winsys
from jarvis.platform.windows import winplatform as wp
from jarvis.plugins.clipboard import (
    KV_KEY,
    MAX_ITEM_CHARS,
    History,
    SmartClipboard,
    clean_answer,
    parse_position,
    translation_lang,
)
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import FakeAi, FakeCommands, FakeTts, enable, manager, wait_until


def fake(ctx: AppContext) -> FakePlatform:
    platform = ctx.platform
    assert isinstance(platform, FakePlatform)
    return platform


def plugin_of(ctx: AppContext) -> SmartClipboard:
    plugin = manager(ctx).get("clipboard")
    assert isinstance(plugin, SmartClipboard)
    return plugin


async def start(ctx: AppContext, config: dict[str, Any] | None = None) -> SmartClipboard:
    """Включить плагин; фоновая проверка буфера — раз в час, тесты проверяют буфер сами (poll_once)."""
    plugin = plugin_of(ctx)
    plugin.poll_s = 3600
    plugin.selection_wait_s = 0.05
    await enable(ctx, "clipboard", config)
    await plugin.poll_once()
    return plugin


async def copy(ctx: AppContext, text: str, *, secret: bool = False) -> None:
    platform = fake(ctx)
    platform.clipboard_set(text)
    platform.clipboard_secret = secret
    await plugin_of(ctx).poll_once()


def texts(plugin: SmartClipboard) -> list[str]:
    return [item.text for item in plugin.history.items]


def select(monkeypatch: pytest.MonkeyPatch, platform: FakePlatform, text: str) -> None:
    """В «активном окне» выделен text: Ctrl+C кладёт его в буфер ("" — ничего не выделено)."""

    def press(combo: str) -> None:
        FakePlatform.press_keys(platform, combo)
        if combo == "ctrl+c" and text:
            platform.clipboard_set(text)

    monkeypatch.setattr(platform, "press_keys", press)


def calls(platform: FakePlatform, name: str) -> list[tuple[Any, ...]]:
    return [args for call, args in platform.calls if call == name]


def system_prompt(ai: Any, index: int = -1) -> str:
    return str(ai.calls[index]["messages"][0]["content"])


# --- история -----------------------------------------------------------------------------------------------


def test_history_dedupe_limit_and_pins() -> None:
    history = History(limit=3)
    for i, text in enumerate(["a", "b", "c", "d"]):
        history.add(text, float(i))
    assert [i.text for i in history.items] == ["d", "c", "b"]
    b = history.items[2]
    assert history.add("b", 10.0) is b and b.ts == 10.0
    assert [i.text for i in history.items] == ["b", "d", "c"]
    assert history.add("b", 11.0) is None  # и так сверху — ничего не меняется
    assert history.add("   \n", 12.0) is None and history.add("x" * (MAX_ITEM_CHARS + 1), 12.0) is None
    history.items[2].pinned = True  # «c»
    history.add("e", 13.0)
    history.add("f", 14.0)
    assert [i.text for i in history.items] == ["f", "e", "b", "c"]  # «d» вытеснен, закреплённый «c» — нет
    assert history.nth(1).text == "f" and history.nth(4).text == "c" and history.nth(5) is None  # type: ignore[union-attr]
    assert history.clear() == 3 and [i.text for i in history.items] == ["c"]
    assert history.clear(everything=True) == 1 and history.items == []


def test_history_load_skips_garbage() -> None:
    history = History(limit=2)
    history.load(
        [
            {"id": "1", "text": "раз", "ts": 3, "pinned": True},
            {"id": "2", "text": "два", "ts": "плохое время"},
            "мусор",
            {"id": "3", "text": "раз"},  # повтор
            {"id": "4", "text": ""},
            {"id": "5", "text": "три", "ts": 1},
            {"id": "6", "text": "четыре", "ts": 0},
        ]
    )
    assert [(i.id, i.text, i.pinned) for i in history.items] == [
        ("1", "раз", True),
        ("2", "два", False),
        ("5", "три", False),
    ]
    history.load(None)
    assert history.items == []


async def test_watcher_captures_copies(pctx: AppContext, pevents: EventLog) -> None:
    fake(pctx).clipboard_set("уже было в буфере")
    plugin = plugin_of(pctx)
    plugin.poll_s = 0.01
    await enable(pctx, "clipboard")
    await wait_until(lambda: texts(plugin) == ["уже было в буфере"])
    fake(pctx).clipboard_set("Привет, мир")
    await wait_until(lambda: texts(plugin) == ["Привет, мир", "уже было в буфере"])
    await wait_until(lambda: bool(pevents.of("plugin.clipboard.items")))
    last = pevents.of("plugin.clipboard.items")[-1]
    assert (
        last["total"] == 2 and last["items"][0]["text"] == "Привет, мир" and last["items"][0]["chars"] == 11
    )
    # Electron берёт горячую клавишу из сохранённых настроек — значение по умолчанию записано
    assert pctx.settings.value.plugins["clipboard"].config["hotkey"] == "Ctrl+Alt+V"


async def test_dedupe_and_limit(pctx: AppContext) -> None:
    plugin = await start(pctx, {"limit": 10})
    for i in range(12):
        await copy(pctx, f"запись {i}")
    assert (
        len(plugin.history.items) == 10
        and texts(plugin)[0] == "запись 11"
        and texts(plugin)[-1] == "запись 2"
    )
    old = plugin.history.items[5]
    await copy(pctx, old.text)
    assert plugin.history.items[0] is old and len(plugin.history.items) == 10
    long_text = "x" * 600
    await copy(pctx, long_text)
    info = (await manager(pctx).action("clipboard", "list", {}))["items"][0]
    assert info["chars"] == 600 and len(info["text"]) == 400  # интерфейсу — начало, вставляется целиком
    await manager(pctx).action("clipboard", "paste", {"id": info["id"]})
    assert ("paste_text", (long_text,)) in fake(pctx).calls


async def test_limit_shrinks_history(pctx: AppContext) -> None:
    plugin = await start(pctx, {"limit": 30})
    for i in range(15):
        await copy(pctx, f"запись {i}")
    await manager(pctx).update("clipboard", config={"limit": 10})
    assert len(plugin.history.items) == 10 and texts(plugin)[-1] == "запись 5"


async def test_watcher_reports_broken_clipboard(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.plugins.clipboard.FAIL_BACKOFF_S", 0.01)
    platform = fake(pctx)
    plugin = plugin_of(pctx)
    plugin.poll_s = 0.01

    def broken() -> int:
        raise PlatformError("Буфер обмена занят другой программой — попробуйте ещё раз")

    monkeypatch.setattr(platform, "clipboard_seq", broken)
    await enable(pctx, "clipboard")
    await wait_until(lambda: plugin.error.startswith("Буфер обмена недоступен"))
    assert manager(pctx).info("clipboard")["status"]["error"] == plugin.error
    monkeypatch.undo()
    platform.clipboard_set("снова работает")
    await wait_until(lambda: texts(plugin) == ["снова работает"] and not plugin.error)


async def test_sensitive_copies_are_skipped(pctx: AppContext) -> None:
    plugin = await start(pctx)
    await copy(pctx, "обычный текст")
    await copy(pctx, "пароль-123", secret=True)
    assert texts(plugin) == ["обычный текст"]
    assert plugin.status()["skipped"] == 1
    assert (await manager(pctx).action("clipboard", "list", {}))["skipped"] == 1


def test_sensitive_marks() -> None:
    assert winsys.is_sensitive({winsys.MONITOR_EXCLUDE: b"\x00"})
    assert winsys.is_sensitive({winsys.VIEWER_IGNORE: None})
    assert winsys.is_sensitive({winsys.HISTORY_FLAG: b"\x00\x00\x00\x00"})
    assert winsys.is_sensitive({winsys.HISTORY_FLAG: None})  # не прочиталось — лучше не запоминать
    assert not winsys.is_sensitive({winsys.HISTORY_FLAG: b"\x01\x00\x00\x00"})
    assert not winsys.is_sensitive({})


class FakeWin32Clipboard:
    """То, что Clipboard.get_item берёт у win32clipboard: форматы буфера по номерам."""

    def __init__(self, text: str, marks: dict[str, bytes]) -> None:
        self.names: dict[str, int] = {}
        self.data: dict[int, Any] = {winsys.CF_UNICODETEXT: text}
        for name, value in marks.items():
            self.data[self.RegisterClipboardFormat(name)] = value
        self.open = 0

    def RegisterClipboardFormat(self, name: str) -> int:
        return self.names.setdefault(name, 0xC000 + len(self.names))

    def OpenClipboard(self) -> None:
        self.open += 1

    def CloseClipboard(self) -> None:
        self.open -= 1

    def IsClipboardFormatAvailable(self, fmt: int) -> bool:
        return fmt in self.data

    def GetClipboardData(self, fmt: int) -> Any:
        return self.data[fmt]

    def GetClipboardDataHandle(self, fmt: int) -> int:
        return fmt

    def GetGlobalMemory(self, handle: int) -> Any:
        return self.data[handle]


@pytest.mark.parametrize(
    ("marks", "expected"),
    [
        ({}, ("строка\nвторая", False)),
        ({winsys.MONITOR_EXCLUDE: b"\x00"}, ("", True)),
        ({winsys.HISTORY_FLAG: b"\x00\x00\x00\x00"}, ("", True)),
        ({winsys.HISTORY_FLAG: b"\x01\x00\x00\x00"}, ("строка\nвторая", False)),
        ({winsys.VIEWER_IGNORE: b""}, ("", True)),
    ],
)
def test_windows_clipboard_item(marks: dict[str, bytes], expected: tuple[str, bool]) -> None:
    clipboard = winsys.Clipboard.__new__(winsys.Clipboard)
    fake_cb = FakeWin32Clipboard("строка\r\nвторая\x00мусор", marks)
    clipboard.cb = fake_cb
    assert clipboard.get_item() == expected
    assert fake_cb.open == 0  # буфер закрыт


def wait_until_sync(condition: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class FakeSender:
    def __init__(self) -> None:
        self.log: list[Any] = []

    def release_modifiers(self, wait_s: float = 0.0, keep: Any = ()) -> None:
        self.log.append(("release", wait_s))

    def combo(self, vks: list[int]) -> None:
        self.log.append(("combo", list(vks)))


class FakeSnapshotClipboard:
    def __init__(self, content: list[tuple[int, bytes]]) -> None:
        self.content = list(content)
        self.seq = 1
        self.restores: list[list[tuple[int, bytes]]] = []

    def set_text(self, text: str, transient: bool = False) -> int:
        self.content = [(13, text.encode("utf-16-le") + b"\x00\x00")]
        self.seq += 1
        return self.seq

    def sequence(self) -> int:
        return self.seq

    def snapshot(self) -> list[tuple[int, bytes]]:
        return list(self.content)

    def restore(self, snapshot: list[tuple[int, bytes]]) -> None:
        self.restores.append(list(snapshot))
        self.content = list(snapshot)
        self.seq += 1


def test_windows_snapshot_during_paste_keeps_user_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "PASTE_RESTORE_S", 0.3)
    platform = WindowsPlatform()
    try:
        platform._sender = FakeSender()  # type: ignore[assignment]
        original = [(13, b"o\x00l\x00d\x00\x00\x00"), (0xC123, b"<b>old</b>")]
        cb = FakeSnapshotClipboard(original)
        platform._clipboard = cb  # type: ignore[assignment]
        assert platform.clipboard_seq() == 1
        platform.paste_text("временный текст")  # буфер вернётся через PASTE_RESTORE_S
        snapshot = platform.clipboard_snapshot()
        assert snapshot == original  # настоящий буфер человека, а не временный текст вставки
        cb.set_text("выделенное")  # Ctrl+C
        platform.clipboard_restore(snapshot)
        assert cb.content == original
        assert wait_until_sync(lambda: platform._paste_pending is None)
        time.sleep(0.05)
        # таймер вставки увидел новую версию и не тронул буфер: возвращался только буфер человека
        assert cb.content == original and cb.restores and all(r == original for r in cb.restores)
    finally:
        platform._media._worker.stop()


async def test_pin_delete_clear_and_persistence(pctx: AppContext) -> None:
    plugin = await start(pctx)
    for text in ("раз", "два", "три"):
        await copy(pctx, text)
    two = plugin.history.items[1]
    act = manager(pctx).action
    result = await act("clipboard", "pin", {"id": two.id})
    assert result["item"]["pinned"] is True and two.pinned
    await act("clipboard", "delete", {"id": plugin.history.items[0].id})
    assert texts(plugin) == ["два", "раз"]
    with pytest.raises(UserFacingError, match="уже нет"):
        await act("clipboard", "delete", {"id": "нет-такого"})
    assert await act("clipboard", "clear", {}) == {"ok": True, "removed": 1, "kept": 1}
    assert texts(plugin) == ["два"]
    await act("clipboard", "pin", {"id": two.id, "pinned": False})
    await act("clipboard", "pin", {"id": two.id})  # без pinned — переключить
    await manager(pctx).update("clipboard", enabled=False)  # остановка сохраняет историю
    assert pctx.db.kv_get(KV_KEY) == [{"id": two.id, "text": "два", "ts": two.ts, "pinned": True}]
    plugin.history.items = []
    await manager(pctx).update("clipboard", enabled=True)
    assert [(i.id, i.text, i.pinned) for i in plugin.history.items] == [(two.id, "два", True)]
    assert await act("clipboard", "clear", {"all": True}) == {"ok": True, "removed": 1, "kept": 0}


async def test_history_is_saved_soon(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.plugins.clipboard.SAVE_DELAY_S", 0.01)
    await start(pctx)
    await copy(pctx, "сохрани меня")
    await wait_until(lambda: [i["text"] for i in pctx.db.kv_get(KV_KEY, [])] == ["сохрани меня"])


# --- выделенный текст --------------------------------------------------------------------------------------


async def test_selection_restores_clipboard(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = await start(pctx)
    platform = fake(pctx)
    await copy(pctx, "старый буфер")
    select(monkeypatch, platform, "Выделенный текст")
    assert await plugin.selected_text() == "Выделенный текст"
    assert platform.clipboard == "старый буфер"  # буфер вернулся
    assert calls(platform, "press_keys") == [("ctrl+c",)] and calls(platform, "clipboard_restore")
    await plugin.poll_once()
    assert texts(plugin) == ["старый буфер"]  # своё копирование — не в историю

    platform.clipboard_set("свежая копия")  # человек скопировал, цикл ещё не видел
    assert await plugin.selected_text() == "Выделенный текст"
    assert texts(plugin) == ["свежая копия", "старый буфер"]

    select(monkeypatch, platform, "")  # ничего не выделено: Ctrl+C буфер не меняет
    restores = len(calls(platform, "clipboard_restore"))
    with pytest.raises(UserFacingError, match=r"^Сначала выделите текст, сэр$"):
        await plugin.selected_text()
    assert platform.clipboard == "свежая копия" and len(calls(platform, "clipboard_restore")) == restores


# --- ИИ ----------------------------------------------------------------------------------------------------


def test_translation_direction_and_cleanup() -> None:
    assert translation_lang("Привет, как дела?", "en") == "en"
    assert translation_lang("Hello there", "de") == "ru"
    assert translation_lang("Привет", "xx") == "en"
    assert clean_answer('"Hello!"') == "Hello!"
    assert clean_answer("«Привет»") == "Привет"
    assert clean_answer("«Привет»", "«Hi»") == "«Привет»"  # кавычки были в исходном
    assert clean_answer("```\nprint(1)\n```") == "print(1)"
    assert clean_answer('He said "hi" and "bye"') == 'He said "hi" and "bye"'


async def test_ai_actions_use_completion(pctx: AppContext) -> None:
    ai = FakeAi(configured=True, reply=lambda messages: "  «Hello, world!»  ")
    pctx.ai = ai  # type: ignore[assignment]
    plugin = await start(pctx, {"target_lang": "de"})
    result = await manager(pctx).action("clipboard", "ai", {"action": "translate", "text": "Привет, мир!"})
    assert result == {
        "ok": True,
        "action": "translate",
        "title": "Перевод на немецкий",
        "text": "Hello, world!",
        "replaced": False,
    }
    assert (
        "на немецкий язык" in system_prompt(ai) and ai.calls[-1]["messages"][1]["content"] == "Привет, мир!"
    )
    await plugin.run_ai("translate", "Good morning", show=False)
    assert "на русский язык" in system_prompt(ai)
    await copy(pctx, "Длинное письмо. " * 40)  # запись истории — целиком, а не начало для списка
    item = plugin.history.items[0]
    await manager(pctx).action("clipboard", "ai", {"action": "shorten", "id": item.id})
    assert ai.calls[-1]["messages"][1]["content"] == item.text and len(item.text) == 640
    for action, marker in (
        ("fix", "корректор"),
        ("shorten", "Сократи"),
        ("polite", "вежливее"),
        ("explain", "Объясни"),
        ("reply", "ответ"),
    ):
        out = await plugin.run_ai(action, "какой-то текст", show=False)
        assert out["action"] == action and marker in system_prompt(ai)
    with pytest.raises(UserFacingError, match="Не знаю действия"):
        await plugin.run_ai("dance", "текст")


async def test_ai_not_configured_and_errors(pctx: AppContext) -> None:
    plugin = await start(pctx)
    with pytest.raises(UserFacingError, match=r"^Подключите ИИ в настройках, сэр$"):
        await manager(pctx).action("clipboard", "ai", {"action": "fix", "text": "превет"})
    pctx.settings.patch({"profile": {"address": ""}})
    with pytest.raises(UserFacingError, match=r"^Подключите ИИ в настройках$"):
        await plugin.run_ai("fix", "превет")
    pctx.ai = FakeAi(configured=True, reply=RuntimeError("сеть упала"))  # type: ignore[assignment]
    with pytest.raises(UserFacingError, match="ИИ не ответил: RuntimeError: сеть упала"):
        await plugin.run_ai("fix", "превет", show=False)
    pctx.ai = FakeAi(configured=True, reply="  ")  # type: ignore[assignment]
    with pytest.raises(UserFacingError, match="пустой ответ"):
        await plugin.run_ai("fix", "превет", show=False)
    with pytest.raises(UserFacingError, match="Слишком длинный"):
        await plugin.run_ai("fix", "а" * 20_000, show=False)


async def test_replace_mode_pastes_over_selection(
    pctx: AppContext, pevents: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    pctx.ai = FakeAi(configured=True, reply="Привет, как дела?")  # type: ignore[assignment]
    await start(pctx, {"replace": True})
    platform = fake(pctx)
    select(monkeypatch, platform, "привет как дила")
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    result = await commands.get("plugin.clipboard:fix").handler({})
    assert result.ok and result.message == "Заменил выделенное"
    assert calls(platform, "paste_text") == [("Привет, как дела?",)]
    states = [e["state"] for e in pevents.of("plugin.clipboard.result")]
    assert states == ["working", "replaced"]


async def test_card_mode_shows_result(
    pctx: AppContext, pevents: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    pctx.ai = FakeAi(configured=True, reply="Hello, how are you?")  # type: ignore[assignment]
    await start(pctx)
    platform = fake(pctx)
    select(monkeypatch, platform, "Привет, как дела?")
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    result = await commands.get("plugin.clipboard:translate").handler({})
    assert result.ok and result.data.get("silent") and calls(platform, "paste_text") == []
    working, done = pevents.of("plugin.clipboard.result")
    assert (
        working["state"] == "working"
        and working["title"] == "Перевожу…"
        and working["at"] == {"x": 960, "y": 540}
    )
    assert (
        done["state"] == "done"
        and done["text"] == "Hello, how are you?"
        and done["title"] == "Перевод на английский"
    )
    assert done["id"] == working["id"] and done["source"] == "Привет, как дела?"
    # «Вставить» на карточке — в окно, где было выделение; оно уже не активно — сначала фокус
    platform.windows = [
        WindowInfo(101, "Документы — Проводник", "explorer.exe", 4100, foreground=True),
        WindowInfo(102, "Jarvis — Google Chrome", "chrome.exe", 5200),
    ]
    await manager(pctx).action("clipboard", "paste", {"text": done["text"]})
    assert calls(platform, "window_action") == [("focus", 102)]
    assert calls(platform, "paste_text") == [("Hello, how are you?",)]
    await manager(pctx).action("clipboard", "copy", {"text": done["text"]})
    assert platform.clipboard == "Hello, how are you?"


async def test_explain_and_wild_fix_are_never_pasted(
    pctx: AppContext, pevents: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = FakeAi(configured=True, reply="Это приветствие: человек спрашивает, как у вас дела.")
    pctx.ai = ai  # type: ignore[assignment]
    await start(pctx, {"replace": True})
    platform = fake(pctx)
    select(monkeypatch, platform, "Привет, как дела?")
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    explained = await commands.get("plugin.clipboard:explain").handler({})
    assert explained.say == "Это приветствие: человек спрашивает, как у вас дела."  # и проговорит
    ai.reply = "Конечно! Вот подробный рецепт борща на четыре порции."
    fixed = await commands.get("plugin.clipboard:fix").handler({})
    assert fixed.ok and fixed.message == "Готово — итог на экране"  # «исправление» ушло от текста
    assert calls(platform, "paste_text") == []
    assert [e["state"] for e in pevents.of("plugin.clipboard.result")] == [
        "working",
        "done",
        "working",
        "done",
    ]


async def test_voice_ai_without_selection(pctx: AppContext) -> None:
    pctx.ai = FakeAi(configured=True, reply="x")  # type: ignore[assignment]
    await start(pctx)
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    result = await commands.get("plugin.clipboard:shorten").handler({})
    assert not result.ok and result.message == "Сначала выделите текст, сэр"


# --- палитра -----------------------------------------------------------------------------------------------


async def test_palette_and_its_actions(
    pctx: AppContext, pevents: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = FakeAi(configured=True, reply="Good morning")
    pctx.ai = ai  # type: ignore[assignment]
    plugin = await start(pctx)
    platform = fake(pctx)
    await copy(pctx, "первая")
    await copy(pctx, "вторая")
    select(monkeypatch, platform, "Доброе утро")
    act = manager(pctx).action
    assert await act("clipboard", "toggle", {}) == {"ok": True, "items": 2, "selection": True}
    assert await act("clipboard", "toggle", {}) == {"ok": True, "repeat": True}  # автоповтор клавиши
    (palette,) = pevents.of("plugin.clipboard.palette")
    assert [i["text"] for i in palette["items"]] == ["вторая", "первая"]
    assert [a["id"] for a in palette["actions"]] == [
        "translate",
        "fix",
        "shorten",
        "polite",
        "explain",
        "reply",
        "read",
    ]
    assert palette["at"] == {"x": 960, "y": 540} and palette["selection"] == "Доброе утро"
    assert (
        palette["target"] == {"title": "Jarvis — Google Chrome", "exe": "chrome.exe"}
        and palette["ai"] is True
    )
    assert platform.clipboard == "вторая"
    presses = len(calls(platform, "press_keys"))
    result = await act("clipboard", "ai", {"action": "translate"})  # кнопка палитры — с выделенным из палитры
    assert result["text"] == "Good morning" and ai.calls[-1]["messages"][1]["content"] == "Доброе утро"
    assert len(calls(platform, "press_keys")) == presses  # второй раз Ctrl+C не нажимали
    await act("clipboard", "paste", {"id": plugin.history.items[1].id})  # Enter в палитре
    assert calls(platform, "paste_text") == [("первая",)] and calls(platform, "window_action") == []
    # палитра взяла клавиатуру: впереди Jarvis, окно под ним — то самое, но не активное → сначала фокус
    behind = WindowInfo(102, "Jarvis — Google Chrome", "chrome.exe", 5200, foreground=False)
    monkeypatch.setattr(platform, "foreground_window", lambda: behind)
    await act("clipboard", "paste", {"id": plugin.history.items[0].id})
    assert calls(platform, "window_action") == [("focus", 102)]
    assert calls(platform, "paste_text")[-1] == ("вторая",)
    with pytest.raises(UserFacingError, match="уже нет"):
        await act("clipboard", "paste", {"id": "нет-такого"})


async def test_palette_in_terminal_does_not_press_ctrl_c(pctx: AppContext) -> None:
    await start(pctx)
    platform = fake(pctx)
    platform.windows = [WindowInfo(201, "Windows PowerShell", "powershell.exe", 77, foreground=True)]
    assert (await manager(pctx).action("clipboard", "palette", {}))["selection"] is False
    assert calls(platform, "press_keys") == []


async def test_read_selection_from_palette(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    await start(pctx)
    select(monkeypatch, fake(pctx), "Прочитай меня")
    await manager(pctx).action("clipboard", "palette", {})
    assert await manager(pctx).action("clipboard", "ai", {"action": "read"}) == {"ok": True, "action": "read"}
    tts = pctx.tts
    assert isinstance(tts, FakeTts) and tts.said == ["Прочитай меня"]


# --- голосовые команды -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("третий", 3),
        ("третье", 3),
        ("вторую", 2),
        ("3-й", 3),
        ("2", 2),
        ("номер пять", 5),
        ("пятый элемент", 5),
        ("десятую запись", 10),
        ("двадцать первый", 21),
        ("последнее", 1),
        ("предпоследнее", 2),
        ("пятьдесят", 50),
        ("что-нибудь", None),
        ("", None),
    ],
)
def test_parse_position(text: str, expected: int | None) -> None:
    assert parse_position(text) == expected


async def test_voice_command_handlers(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = await start(pctx)
    platform = fake(pctx)
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    empty = await commands.get("plugin.clipboard:paste_last").handler({})
    assert not empty.ok and empty.message == "История буфера пуста, сэр"
    for text in ("один", "два", "три"):
        await copy(pctx, text)
    last = await commands.get("plugin.clipboard:paste_last").handler({})
    assert last.ok and last.message == "Вставил: «три»"
    third = await commands.get("plugin.clipboard:paste_nth").handler({"text": "третий"})
    assert third.ok and calls(platform, "paste_text") == [("три",), ("один",)]
    too_far = await commands.get("plugin.clipboard:paste_nth").handler({"text": "пятый"})
    assert not too_far.ok and too_far.message == "В истории буфера всего 3 записи"
    unclear = await commands.get("plugin.clipboard:paste_nth").handler({"text": "мой адрес"})
    assert not unclear.ok and unclear.message == "Не понял, какую запись вставить, сэр"
    await manager(pctx).action("clipboard", "pin", {"id": plugin.history.items[0].id})
    cleared = await commands.get("plugin.clipboard:clear").handler({})
    assert cleared.ok and cleared.message == "История очищена — закреплённые остались (1)"

    select(monkeypatch, platform, "Текст для чтения")
    read = await commands.get("plugin.clipboard:read").handler({})
    assert read.ok and read.say == "Читаю."
    tts = pctx.tts
    assert isinstance(tts, FakeTts)
    await wait_until(lambda: tts.said == ["Текст для чтения"])
    assert commands.by_phrase("прочитай выделенное").id == "plugin.clipboard:read"


VOICE = [
    ("вставь последнее из буфера", "plugin.clipboard:paste_last"),
    ("вставь третий из буфера", "plugin.clipboard:paste_nth"),
    ("вставь 2-й из буфера", "plugin.clipboard:paste_nth"),
    ("вставь пятую запись из буфера", "plugin.clipboard:paste_nth"),
    ("очисти историю буфера", "plugin.clipboard:clear"),
    ("переведи выделенное", "plugin.clipboard:translate"),
    ("исправь ошибки в выделенном", "plugin.clipboard:fix"),
    ("сократи выделенное", "plugin.clipboard:shorten"),
    ("сделай вежливее", "plugin.clipboard:polite"),
    ("объясни выделенное", "plugin.clipboard:explain"),
    ("ответь на выделенное", "plugin.clipboard:reply"),
    ("прочитай выделенное", "plugin.clipboard:read"),
    ("Джарвис, переведи выделенное пожалуйста", "plugin.clipboard:translate"),
]


def test_voice_phrases_match(client: TestClient) -> None:
    assert client.put("/api/plugins/clipboard", json={"enabled": True}).status_code == 200
    for text, command_id in VOICE:
        match = client.post("/api/commands/test", json={"text": text}).json()["match"]
        assert match is not None, text
        assert [p["command_id"] for p in match["parts"]] == [command_id], text
    nth = client.post("/api/commands/test", json={"text": "вставь третий из буфера"}).json()["match"]
    assert nth["parts"][0]["slots"]["text"] == "третий"
    # обычная вставка Ctrl+V осталась встроенной командой
    plain = client.post("/api/commands/test", json={"text": "вставь из буфера"}).json()["match"]
    assert plain["parts"][0]["command_id"] == "builtin.keyboard.paste"


def test_api_actions(client: TestClient) -> None:
    ctx: AppContext = client.ctx  # type: ignore[attr-defined]
    plugin = plugin_of(ctx)
    plugin.poll_s = 0.01
    r = client.put("/api/plugins/clipboard", json={"enabled": True})
    assert r.status_code == 200 and r.json()["status"]["running"] is True
    assert r.json()["config"]["hotkey"] == "Ctrl+Alt+V"

    def action(name: str, **args: Any) -> Any:
        return client.post("/api/plugins/clipboard/action", json={"action": name, "args": args})

    def listed_texts() -> list[str]:
        return [i["text"] for i in action("list").json()["items"]]

    def wait_for(expected: list[str]) -> None:
        deadline = time.monotonic() + 3
        while listed_texts() != expected:
            assert time.monotonic() < deadline, listed_texts()
            time.sleep(0.02)

    platform = fake(ctx)
    platform.clipboard_set("альфа")
    wait_for(["альфа"])
    platform.clipboard_set("бета")
    wait_for(["бета", "альфа"])
    listed = action("list").json()
    assert [i["text"] for i in listed["items"]] == ["бета", "альфа"] and listed["total"] == 2
    alpha = listed["items"][1]["id"]
    assert action("pin", id=alpha).json()["item"]["pinned"] is True
    assert action("copy", id=alpha).json() == {"ok": True} and platform.clipboard == "альфа"
    assert action("paste", id=alpha).json() == {"ok": True}
    assert ("paste_text", ("альфа",)) in platform.calls
    r = action("ai", action="translate", text="привет")
    assert r.status_code == 400 and r.json()["detail"] == "Подключите ИИ в настройках, сэр"
    assert action("palette").status_code == 200
    assert action("clear").json()["kept"] == 1
    r = action("delete", id=alpha)
    assert r.status_code == 200 and action("list").json()["items"] == []
    assert action("delete", id=alpha).status_code == 400
