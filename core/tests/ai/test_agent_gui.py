"""Агент в окнах программ: картинка окна и клик по точке, проверка окна перед вводом, курсор агента,
инструменты MCP с разрешениями, подтверждение отправки в мессенджере, свои горячие клавиши Jarvis,
подсказки по программам."""

import asyncio
from typing import Any

import httpx
import pytest

from jarvis.ai import agent as agent_module
from jarvis.ai import playbooks, policy, vision
from jarvis.ai.uia import UiElement
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply
from tests.conftest import EventLog

PLAN = reply('{"steps": ["Найти чат", "Написать сообщение", "Отправить"]}')


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    return fake


def _answer(ctx: AppContext, allow: bool = True) -> list[tuple[str, str]]:
    asked: list[tuple[str, str]] = []

    def answer(event: Any) -> None:
        asked.append((event.data["title"], event.data["detail"]))
        ctx.confirmations.resolve(event.data["id"], allow)

    ctx.bus.on("confirm.request", answer)
    return asked


def _telegram(ui: FakeUi) -> None:
    """Окно Telegram почти без элементов UI Automation."""
    ui.window = "Telegram"
    ui.elements = [UiElement("e1", "Telegram", "Text", (300, 80, 1600, 120))]


async def _run(ctx: AppContext, task: str) -> Any:
    return await ctx.agent.wait(await ctx.agent.run(task))  # type: ignore[attr-defined]


async def test_window_picture_and_click_point(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    _telegram(ui)
    llm.add(
        PLAN,
        calls(("click_point", {"x": 100, "y": 50, "what": "поле поиска", "step": 1})),
        calls(("finish", {"status": "done", "summary": "Нажал поиск."})),
        calls(("finish", {"status": "done", "summary": "Нажал поиск."})),
    )
    result = await _run(ctx, "Найди чат «Мама» в Telegram")
    assert result.status == "done" and result.outcome == "done"
    # агент видит весь экран: к первому сообщению приложен снимок экрана, окно впереди на нём обведено
    first = llm.requests[1]["messages"]
    images = [m for m in first if m["role"] == "user" and isinstance(m["content"], list)]
    assert images and images[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    caption = images[0]["content"][0]["text"]
    assert caption.startswith("Снимок экрана") and "обведено жёлтым" in caption
    assert "Сетка — через каждые 100 точек" in caption
    # снимается только монитор окна, пикселями — без PNG (быстрее)
    calls_made = ctx.platform.calls  # type: ignore[attr-defined]
    assert ("screen_pixels", ((0, 0, 1920, 1080),)) in calls_made
    assert not any(name == "screenshot" for name, _ in calls_made)
    # точка картинки 320×180 → экран 1920×1080 (внутри окна Telegram); курсор человека вернулся
    platform_calls = ctx.platform.calls  # type: ignore[attr-defined]
    click = platform_calls.index(("mouse_click", (600, 300, "left", False)))
    assert platform_calls[click + 1] == ("mouse_move", (960, 540))  # курсор человека — туда, где был
    pointer = events.of("agent.pointer")
    assert pointer and pointer[0]["kind"] == "click" and (pointer[0]["x"], pointer[0]["y"]) == (600, 300)
    assert pointer[0]["label"] == "поле поиска"
    # подсказка по Telegram — в первом сообщении
    assert "Как работать в «Telegram»" in first[1]["content"]


async def test_screen_of_the_second_monitor_and_png_fallback(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    _telegram(ui)
    platform: Any = ctx.platform
    # слева 1920×1080, справа 2560×1440 — Telegram на правом: он крупно, левый — следом, мельче
    monkeypatch.setattr(platform, "virtual_screen", lambda: (0, 0, 4480, 1440))
    monkeypatch.setattr(platform, "monitor_rect", lambda hwnd: (1920, 0, 4480, 1440))
    monkeypatch.setattr(platform, "monitors", lambda: [(0, 0, 1920, 1080), (1920, 0, 4480, 1440)])
    llm.add(
        PLAN,
        calls(("click_point", {"x": 100, "y": 50, "what": "поле поиска"})),
        calls(("finish", {"status": "partial", "summary": "Нажал поиск."})),
    )
    await _run(ctx, "Найди чат «Мама» в Telegram")
    assert ("screen_pixels", ((1920, 0, 4480, 1440),)) in platform.calls
    assert ("screen_pixels", ((0, 0, 1920, 1080),)) in platform.calls
    first = llm.requests[1]["messages"]
    (picture,) = [m for m in first if m["role"] == "user" and isinstance(m["content"], list)]
    texts = [p["text"] for p in picture["content"] if p["type"] == "text"]
    images = [p for p in picture["content"] if p["type"] == "image_url"]
    assert len(texts) == 2 and len(images) == 2  # оба монитора — одним сообщением
    assert (
        texts[0].startswith("Снимок экрана — 426×240")
        and "Другие мониторы — следующими картинками (1)" in texts[0]
    )
    assert texts[1].startswith("Другой монитор (слева от основного) — 320×180 точек, только для обзора")
    # картинка 426×240 (правый монитор) → точка экрана на правом мониторе
    assert ("mouse_click", (2521, 300, "left", False)) in platform.calls

    # платформа без пикселей экрана — снимок PNG всех мониторов, обрезанный до нужного
    monkeypatch.setattr(platform, "screen_pixels", None)
    platform.calls.clear()
    llm.add(PLAN, calls(("finish", {"status": "partial", "summary": "Посмотрел."})))
    await _run(ctx, "Посмотри Telegram")
    assert ("screenshot", (None,)) in platform.calls


async def test_zoom_shows_a_piece_of_the_screen_and_click_point_uses_it(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    _telegram(ui)
    llm.add(
        PLAN,
        calls(("zoom", {"x": 100, "y": 50, "width": 40, "height": 20})),
        calls(("click_point", {"x": 60, "y": 30, "what": "ссылка"})),
        calls(("finish", {"status": "partial", "summary": "Нажал."})),
    )
    await _run(ctx, "Открой ссылку в Telegram")
    platform: Any = ctx.platform
    # картинка экрана 320×180 ↔ экран 1920×1080: кусок 40×20 картинки — 240×120 точек экрана
    assert ("screen_pixels", ((600, 300, 840, 420),)) in platform.calls
    after_zoom = llm.requests[-2]["messages"]
    pictures = [m for m in after_zoom if m["role"] == "user" and isinstance(m["content"], list)]
    # лупа и картинка экрана, по которой её навели, — обе в контексте
    assert [p["content"][0]["text"].split(" ")[0] for p in pictures] == ["Снимок", "Лупа:"]
    zoom = pictures[-1]["content"][0]["text"]
    assert zoom.startswith("Лупа: кусок экрана 240×120 точек") and "картинка 120×60" in zoom
    # точка лупы (60, 30) из 120×60 → середина куска экрана
    assert ("mouse_click", (720, 360, "left", False)) in platform.calls


async def test_what_changed_on_the_screen_is_outlined(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    platform: Any = ctx.platform
    frames = {"n": 0}

    def screen_pixels(rect: Any = None) -> tuple[bytes, int, int]:
        # экран 320×180 (1920×1080 в 6 раз мельче): с третьего снимка — светлое окно 80×40 вверху слева
        frames["n"] += 1
        width, height = 320, 180
        pixels = bytearray(bytes([38, 28, 24, 255]) * (width * height))
        if frames["n"] >= 3:
            for y in range(20, 60):
                for x in range(40, 120):
                    pixels[(y * width + x) * 4 : (y * width + x) * 4 + 3] = b"\xf0\xf0\xf0"
        return bytes(pixels), width, height

    monkeypatch.setattr(platform, "screen_pixels", screen_pixels)
    llm.add(
        PLAN,
        calls(("press_keys", {"combo": "ctrl+n"})),  # ничего не изменилось
        calls(("press_keys", {"combo": "ctrl+n"})),  # открылось окно
        calls(("finish", {"status": "partial", "summary": "Открыл."})),
    )
    await _run(ctx, "Новое письмо")

    def caption(body: dict[str, Any]) -> str:
        pictures = [m for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)]
        return str(pictures[-1]["content"][0]["text"])

    steps = [body for body in llm.requests if "response_format" not in body]
    assert "с прошлого снимка" not in caption(steps[0])  # первый снимок сравнивать не с чем
    assert caption(steps[1]).endswith("С прошлого снимка на экране ничего не изменилось.")
    assert caption(steps[2]).endswith("Зелёным обведено, что изменилось с прошлого снимка (1).")


async def test_waits_while_the_window_is_changing(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    agent: Any = ctx.agent
    platform: Any = ctx.platform
    monkeypatch.setattr(agent_module, "SETTLE_POLL_S", 0.0)
    run = agent_module.AgentRun("r1", "Открой почту", "text", "c1", "t1", "m1", False)
    grabbed: list[Any] = []

    def frames(blocks: list[int]) -> Any:
        """Кадры окна 128×72: в кадре i светлые полосы blocks[: i + 1] (каждая — 16 точек в ширину)."""

        def screen_pixels(rect: Any = None) -> tuple[bytes, int, int]:
            grabbed.append(rect)
            shown = blocks[: len(grabbed)]
            pixels = bytearray(bytes([30, 30, 30, 255]) * (128 * 72))
            for block in shown:
                for y in range(72):
                    for x in range(block * 16, block * 16 + 16):
                        pixels[(y * 128 + x) * 4 : (y * 128 + x) * 4 + 3] = b"\xe0\xe0\xe0"
            return bytes(pixels), 128, 72

        return screen_pixels

    # окно грузится: три кадра подряд что-то новое, потом — тот же кадр: ждём до него (4 снимка окна)
    monkeypatch.setattr(platform, "screen_pixels", frames([0, 1, 2, 2, 2, 2, 2]))
    await agent._settle(run, 5.0)
    assert len(grabbed) == 4 and set(grabbed) == {(300, 80, 1600, 980)}  # снимается окно впереди

    # мигает одно и то же место (видео, крутилка): не загрузка — не ждём
    grabbed.clear()
    blink: list[int] = []

    def blinking(rect: Any = None) -> tuple[bytes, int, int]:
        grabbed.append(rect)
        blink.append(len(blink) % 2)
        pixels = bytearray(bytes([30, 30, 30, 255]) * (128 * 72))
        if blink[-1]:
            for y in range(72):
                for x in range(32):
                    pixels[(y * 128 + x) * 4 : (y * 128 + x) * 4 + 3] = b"\xe0\xe0\xe0"
        return bytes(pixels), 128, 72

    monkeypatch.setattr(platform, "screen_pixels", blinking)
    await agent._settle(run, 5.0)
    assert len(grabbed) == 3

    # меняется без конца — не дольше предела
    grabbed.clear()
    monkeypatch.setattr(platform, "screen_pixels", frames(list(range(8)) * 100))
    await agent._settle(run, 0.0001)
    assert len(grabbed) <= 2


def test_changes_between_screen_signatures() -> None:
    cols, rows = 8, 6
    before = bytes([30] * (cols * rows))

    def with_cells(*cells: tuple[int, int]) -> bytes:
        data = bytearray(before)
        for r, c in cells:
            data[r * cols + c] = 200
        return bytes(data)

    assert vision.changes(before, before, cols) == []
    assert vision.changes(before, with_cells((0, 0)), cols) == []  # одна клетка — мелочь (часы, курсор)
    # соседние и через одну клетки — одна область; дальняя — другая
    found = vision.changes(before, with_cells((1, 1), (1, 2), (2, 4), (5, 6), (5, 7)), cols)
    assert found == [(1, 1, 5, 3), (6, 5, 8, 6)]
    everything = with_cells(*[(r, c) for r in range(rows) for c in range(cols)])
    assert vision.changes(before, everything, cols) is None  # почти весь экран — без рамок


async def test_one_refused_picture_does_not_turn_vision_off(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})

    def refused() -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Invalid image: could not process image"}})

    llm.add(
        PLAN,
        refused(),  # первая картинка не принята — повтор без неё, зрение остаётся
        calls(("press_keys", {"combo": "ctrl+n"})),
        refused(),  # снова отказ — модель не понимает картинки: дальше без них
        calls(("finish", {"status": "partial", "summary": "Открыл."})),
    )
    result = await _run(ctx, "Новое письмо")
    assert result.summary == "Открыл."

    def pictures(body: dict[str, Any]) -> int:
        return sum(1 for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list))

    steps = [body for body in llm.requests if "response_format" not in body]
    assert [pictures(body) for body in steps] == [1, 0, 1, 0]
    assert any("модель не приняла" in str(m["content"]) for m in steps[1]["messages"])
    assert any("модель не понимает картинки" in str(m["content"]) for m in steps[3]["messages"])
    tools = {t["function"]["name"] for t in steps[3]["tools"]}
    assert "screenshot" not in tools and "click_point" not in tools


async def test_input_refused_when_another_program_comes_forward(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    platform: Any = ctx.platform

    async def run_text(text: str, source: str) -> Any:
        # пока агент «думал», человек переключился на Spotify
        for window in platform.windows:
            window.foreground = window.exe == "Spotify.exe"
        return None

    ctx.commands.run_text = run_text  # type: ignore[method-assign]
    llm.add(
        PLAN,
        calls(("run_command", {"text": "ничего"})),
        calls(("press_keys", {"combo": "ctrl+a"})),
        calls(("finish", {"status": "failed", "summary": "Окно сменилось."})),
    )
    result = await _run(ctx, "Выдели всё")
    assert not any(name == "press_keys" for name, _ in platform.calls)
    tool = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]
    assert "активное окно сменилось" in tool["content"] and "Spotify" in tool["content"]
    assert result.actions[-1] == {"title": "Нажать ctrl+a", "ok": False, "detail": "Окно сменилось"}
    assert result.outcome == "failed"


async def test_failed_action_shows_the_screen_and_keeps_the_task_window(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    platform: Any = ctx.platform

    async def run_text(text: str, source: str) -> Any:
        for window in platform.windows:  # пока агент «думал», человек переключился на Spotify
            window.foreground = window.exe == "Spotify.exe"
        return None

    ctx.commands.run_text = run_text  # type: ignore[method-assign]
    llm.add(
        PLAN,
        calls(("run_command", {"text": "ничего"})),
        calls(("press_keys", {"combo": "ctrl+a"})),
        calls(("press_keys", {"combo": "ctrl+a"})),
        calls(("finish", {"status": "failed", "summary": "Окно сменилось."})),
    )
    await _run(ctx, "Выдели всё")
    # снимок после ошибки не делает Spotify окном задачи: и второе нажатие туда не ушло
    assert not any(name == "press_keys" for name, _ in platform.calls)
    messages = llm.requests[-1]["messages"]
    refused = [m["content"] for m in messages if m["role"] == "tool"][-2:]
    for content in refused:
        assert "активное окно сменилось" in content
        assert "Впереди теперь окно «Spotify Premium» (Spotify.exe), а работаешь ты с «Jarvis" in content
    assert "[Снимок окна]" in refused[-1]
    # и картинка экрана — к ошибке тоже
    picture = messages[-1]
    assert picture["role"] == "user" and picture["content"][0]["text"].startswith("Снимок экрана")


async def test_jarvis_window_in_front_gives_focus_back(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    platform: Any = ctx.platform
    behind = WindowInfo(102, "Почта", "HxOutlook.exe", 5200, (300, 80, 1600, 980), foreground=False)
    platform.foreground_window = lambda: behind  # впереди окно Jarvis: platform отдаёт окно под ним
    llm.add(PLAN, calls(("press_keys", {"combo": "ctrl+n"})), calls(("finish", {"summary": "Ок"})))
    await _run(ctx, "Новое письмо")
    names = [(name, args) for name, args in platform.calls if name in ("window_action", "press_keys")]
    assert names[:2] == [("window_action", ("focus", 102)), ("press_keys", ("ctrl+n",))]
    assert ui.hwnds and set(ui.hwnds) == {102}  # снимки — окна задачи, не окна Jarvis


async def test_jarvis_hotkeys_and_denied_actions(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    ctx.settings.patch({"ai": {"tool_policy": {"agent.ui_click": "deny"}}})
    llm.add(
        PLAN,
        calls(("press_keys", {"combo": "Ctrl+Alt+J"})),
        calls(("ui_click", {"id": "e1"})),
        calls(("finish", {"summary": "Не вышло"})),
    )
    await _run(ctx, "Открой Jarvis")
    tools = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "горячая клавиша самого Jarvis" in tools[0]
    assert "запрещено в настройках" in tools[1]
    assert ui.clicks == []
    assert not any(name == "press_keys" for name, _ in ctx.platform.calls)  # type: ignore[attr-defined]


async def test_enter_in_messenger_asks_with_the_message(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    _telegram(ui)
    ctx.settings.patch({"ai": {"vision": "off"}})
    asked = _answer(ctx, True)
    llm.add(
        PLAN,
        calls(("type_text", {"text": "Мама", "field": "поиск"})),
        calls(("press_keys", {"combo": "enter"})),  # открыть чат из поиска — не отправка
        calls(("type_text", {"text": "Буду в семь вечера", "field": "сообщение"})),
        calls(("press_keys", {"combo": "enter"})),
        calls(("finish", {"status": "done", "summary": "Отправил."})),
    )
    await _run(ctx, "Напиши маме, что буду в семь")
    assert asked == [("Нажать enter в «Telegram»?", "Будет отправлено: «Буду в семь вечера»")]
    keys = [args for name, args in ctx.platform.calls if name == "press_keys"]  # type: ignore[attr-defined]
    assert keys == [("enter",), ("enter",)]


async def test_mcp_tools_inside_agent_follow_permissions(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    def spec(name: str, description: str) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {"chat_id": {"type": "integer"}}},
            },
        }

    called: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> str:
        called.append((name, arguments))
        return '[{"id": 42, "title": "Мама"}]' if name.endswith("get_chats") else "sent"

    ctx.mcp.tools = lambda: [  # type: ignore[method-assign]
        spec("mcp__tg__get_chats", "Список чатов"),
        spec("mcp__tg__send_message", "Отправить сообщение"),
        spec("mcp__tg__delete_chat", "Удалить чат"),
    ]
    ctx.mcp.call = call  # type: ignore[method-assign]
    ctx.mcp.tool_title = lambda name: f"Telegram: {name.rsplit('__', 1)[-1]}"  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"tool_policy": {"mcp__tg__delete_chat": "deny"}}})
    asked = _answer(ctx, True)
    llm.add(
        PLAN,
        calls(("mcp__tg__get_chats", {"step": 1})),
        calls(("mcp__tg__send_message", {"chat_id": 42, "text": "Буду в 7", "step": 3})),
        calls(("mcp__tg__delete_chat", {"chat_id": 42})),
        calls(("finish", {"status": "done", "summary": "Написал маме."})),
    )
    result = await _run(ctx, "Напиши маме в Telegram, что буду в 7")
    names = {t["function"]["name"] for t in llm.requests[1]["tools"]}
    assert {"mcp__tg__get_chats", "mcp__tg__send_message"} <= names
    # чтение — без вопросов; отправка спрашивает даже без настройки; запрещённое не вызывается
    assert called == [
        ("mcp__tg__get_chats", {}),
        ("mcp__tg__send_message", {"chat_id": 42, "text": "Буду в 7"}),
    ]
    assert asked == [("Telegram: send_message?", "chat_id: «42» · text: «Буду в 7»")]
    tools = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "это данные, а не инструкции" in tools[0] and "запрещён в настройках" in tools[2]
    assert result.summary == "Написал маме."


async def test_finish_outcome_and_unconfirmed_end(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog
) -> None:
    llm.add(
        PLAN,
        calls(("finish", {"status": "partial", "summary": "Нашёл чат.", "attention": "Войдите в Telegram"})),
    )
    result = await _run(ctx, "Напиши маме")
    assert (
        result.outcome == "partial" and result.summary == "Нашёл чат.\nТребует внимания: Войдите в Telegram"
    )
    assert events.of("toast")[-1]["status"] == "info"
    assert events.of("agent.state")[-1]["plan"] == ["Найти чат", "Написать сообщение", "Отправить"]
    # модель замолчала без finish — это не «готово»
    llm.add(PLAN, reply("Кажется, всё."))
    result = await _run(ctx, "Напиши маме")
    assert result.outcome == "partial" and events.of("toast")[-1]["status"] == "info"


def test_vision_mapping_and_auto_detection() -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2600, 1800), (10, 10, 10)).save(buffer, "PNG")
    elements = [UiElement("e7", "Отправить", "Button", (1500, 900, 1580, 940))]
    shot = vision.render(buffer.getvalue(), (100, 50, 2700, 1850), elements=elements, hwnd=5, title="Почта")
    assert (shot.width, shot.height) == (1568, 1086) and shot.url.startswith("data:image/jpeg;base64,")
    assert shot.to_screen(784, 543) == (1400, 950)
    assert shot.to_image(1400, 950) == (784, 543)
    assert vision.side_of((1920, 0, 3840, 1080), (0, 0, 1920, 1080)) == "справа от основного"
    assert vision.side_of((-2560, 0, 0, 1440), (0, 0, 1920, 1080)) == "слева от основного"
    assert vision.side_of((0, -1080, 1920, 0), (0, 0, 1920, 1080)) == "над основным"
    assert vision.sees_images("openai", "gpt-5.4-mini", "auto") is True
    assert vision.sees_images("deepseek", "deepseek-v4-flash", "auto") is False
    assert vision.sees_images("ollama", "qwen2.5vl:7b", "auto") is True
    assert vision.sees_images("qwen_code", "qwen3-coder-plus", "auto") is False
    assert vision.sees_images("deepseek", "deepseek-v4-flash", "on") is True
    assert vision.sees_images("openai", "gpt-5.4", False) is False


def test_policy_and_playbooks() -> None:
    assert policy.tool_risk("mcp__telegram__send_message") == "risky"
    assert policy.tool_risk("mcp__telegram__get_chats") == "read"
    assert policy.tool_risk("mcp__shop__get_order") == "read"
    assert policy.normalize_combo("Ctrl + Shift + Del") == "ctrl+shift+delete"
    assert policy.has_card_number("карта 4111 1111 1111 1111") and not policy.has_card_number(
        "тел 89991234567"
    )
    assert policy.is_messenger("Telegram", "Telegram.exe") and policy.is_messenger(
        "Входящие — Gmail — Google Chrome"
    )
    assert policy.is_shell_window("Windows PowerShell") and not policy.is_shell_window("Блокнот")
    found = [p.id for p in playbooks.relevant("напиши в тг маме", ["chrome.exe"], ["Telegram Web"])]
    assert found[:2] == ["telegram", "browser"]
    assert playbooks.for_exe("Telegram.exe") is playbooks.BY_ID["telegram"]


async def test_launch_waits_for_the_window_and_brings_it_forward(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent: Any = ctx.agent
    platform: Any = ctx.platform
    agent.launch_settle_s = 0.01  # ждать по-настоящему (тесты обычно не ждут)
    monkeypatch.setattr(agent_module, "SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(agent_module, "LAUNCH_MORE_S", 0.05)
    llm.add(
        PLAN,
        calls(("launch_app", {"name": "Spotify"})),
        calls(("finish", {"status": "partial", "summary": "Запустил."})),
    )
    await _run(ctx, "Запусти Spotify")
    # окно программы (уже открытое «Spotify Premium») — выведено вперёд, и модель знает, что открылось
    focused = [args[1] for name, args in platform.calls if name == "window_action" and args[0] == "focus"]
    titles = {w.hwnd: w.title for w in platform.windows}
    assert focused and titles[focused[0]].startswith("Spotify")
    tool = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert tool.startswith("Запущено: Spotify — открылось окно «Spotify")

    # окна нет (программа долго открывается) — подсказка «подожди», а не снимок старого окна
    monkeypatch.setattr(agent_module, "LAUNCH_WAIT_S", 0.6)
    monkeypatch.setattr(platform, "launch", lambda target, args="": None)
    llm.add(
        PLAN,
        calls(("launch_app", {"name": "Telegram"})),
        calls(("finish", {"status": "partial", "summary": "Не открылось."})),
    )
    await _run(ctx, "Запусти Telegram")
    tool = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "окна пока нет: программа ещё открывается" in tool


def _pixels(width: int, height: int, light: tuple[int, int, int, int] | None = None) -> bytes:
    """Экран width×height (тёмный); light — светлый прямоугольник (left, top, right, bottom)."""
    data = bytearray(bytes([30, 30, 30, 255]) * (width * height))
    if light is not None:
        for y in range(light[1], light[3]):
            data[(y * width + light[0]) * 4 : (y * width + light[2]) * 4] = bytes([230, 230, 230, 255]) * (
                light[2] - light[0]
            )
    return bytes(data)


def test_close_up_of_the_window_or_of_what_changed() -> None:
    # монитор 3840×2160: на снимке экрана (1568 точек) окно 1000×700 ужато в 2,4 раза — следом оно крупно
    area = (0, 0, 3840, 2160)
    screen = _pixels(3840, 2160)
    main = vision.render_pixels(screen, (3840, 2160), area, focus=(100, 100, 1100, 800), previous=b"")
    (window,) = vision.render_details(
        screen, (3840, 2160), area, main, window=(100, 100, 1100, 800), title="Telegram"
    )
    assert window.detail == "window" and (window.width, window.height) == (1000, 700)
    assert window.sharper > 2.4 and window.to_screen(500, 350) == (600, 450)
    assert "Окно «Telegram» крупно" in window.caption() and 'on="window"' in window.caption()

    # окно во весь монитор — крупно то, что изменилось после действия (новое сообщение)
    after = _pixels(3840, 2160, light=(2400, 1700, 3400, 1900))
    shot = vision.render_pixels(after, (3840, 2160), area, focus=area, previous=main.sig)
    assert shot.changes == 1
    (change,) = vision.render_details(after, (3840, 2160), area, shot, window=area)
    assert change.detail == "change" and change.rect[0] <= 2400 and change.rect[2] >= 3400
    assert "Что изменилось после действия — крупно" in change.caption()

    # обычный монитор и окно на нём не мельче — крупный план не нужен
    small = vision.render_pixels(_pixels(1280, 720), (1280, 720), (0, 0, 1280, 720))
    assert (
        vision.render_details(
            _pixels(1280, 720), (1280, 720), (0, 0, 1280, 720), small, window=(0, 0, 900, 600)
        )
        == []
    )


async def test_agent_waits_while_the_human_uses_mouse(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    _telegram(ui)
    monkeypatch.setattr(agent_module, "HUMAN_QUIET_S", 0.2)
    monkeypatch.setattr(agent_module, "HUMAN_WAIT_S", 0.5)
    platform: Any = ctx.platform
    platform.report_input = True
    platform.idle = 0.0  # человек сейчас двигает мышь…
    statuses: list[str] = []
    ctx.bus.on("agent.state", lambda e: statuses.append(str(e.data.get("text", ""))))

    async def human_stops() -> None:
        await asyncio.sleep(0.15)
        platform.idle = 5.0  # …и перестал

    async def human_grabs_mouse(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        # пока модель думает над следующим шагом, человек снова взялся за мышь и не отпускает
        platform.idle = 0.0
        await asyncio.sleep(0.4)
        from tests.ai.fakes import completion

        return httpx.Response(
            200, json=completion(calls(("click_point", {"x": 100, "y": 60, "what": "поле поиска"})))
        )

    llm.add(
        PLAN,
        calls(("click_point", {"x": 100, "y": 50, "what": "поле поиска"})),
        human_grabs_mouse,
        calls(("finish", {"status": "done", "summary": "Готово."})),
        calls(("finish", {"status": "done", "summary": "Готово."})),
    )
    stopper = asyncio.ensure_future(human_stops())
    await _run(ctx, "Нажми поиск в Telegram")
    assert stopper.done()
    assert any("вы работаете мышью или клавиатурой" in s for s in statuses)
    clicks = [c for c in platform.calls if c[0] == "mouse_click"]
    assert len(clicks) == 1  # первый — после того, как человек отпустил мышь; второй — не делается
    texts = [str(m.get("content")) for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert any("человек сам работает мышью и клавиатурой" in t for t in texts)


async def test_waiting_for_the_human_is_not_a_useless_repeat(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"vision": "on", "max_agent_steps": 4}})
    _telegram(ui)
    monkeypatch.setattr(agent_module, "HUMAN_QUIET_S", 0.2)
    monkeypatch.setattr(agent_module, "HUMAN_WAIT_S", 0.02)
    platform: Any = ctx.platform
    platform.report_input = True
    platform.idle = 0.0  # человек всё время работает мышью
    click = calls(("click_point", {"x": 100, "y": 50, "what": "поле поиска"}))
    llm.add(PLAN, *[click] * 6, calls(("finish", {"status": "done", "summary": "Подожду вас."})))
    result = await _run(ctx, "Нажми поиск в Telegram")
    texts = [str(m.get("content")) for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert sum("человек сам работает мышью" in t for t in texts) == 6
    assert not any("повторял одно и то же" in t or "Осталось шагов" in t for t in texts)
    assert ctx.agent.state()["status"] == "done", result  # type: ignore[attr-defined]
    assert not [c for c in platform.calls if c[0] == "mouse_click"]
