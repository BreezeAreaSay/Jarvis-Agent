"""Агент «работает сам»: план, шаги с элементами окон, подтверждения, остановка, лимит шагов, чат «Агент»."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from jarvis.ai.uia import UiElement
from jarvis.context import AppContext
from jarvis.services import ExecResult, UserFacingError
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply
from tests.conftest import EventLog

PLAN = reply(
    json.dumps({"steps": ["Открыть почту", "Нажать «Написать»", "Заполнить письмо", "Нажать «Отправить»"]})
)
TASK = "Напиши Ивану письмо «Отчёт готов» и отправь"


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    return fake


def _answer_confirmations(ctx: AppContext, allow: bool) -> list[tuple[str, str]]:
    asked: list[tuple[str, str]] = []

    def answer(event: Any) -> None:
        asked.append((event.data["title"], event.data["detail"]))
        assert event.data["kind"] == "agent"
        ctx.confirmations.resolve(event.data["id"], allow)

    ctx.bus.on("confirm.request", answer)
    return asked


def _mail_script(llm: ScriptedLlm, final: str) -> None:
    llm.add(
        PLAN,
        calls(("ui_click", {"id": "e1", "step": 2})),
        calls(
            ("ui_type", {"id": "e1", "text": "ivan@example.com", "step": 3}),
            ("ui_type", {"id": "e3", "text": "Отчёт готов, смотри вложение", "step": 3}),
        ),
        calls(("ui_click", {"id": "e4", "step": 4})),
        calls(("finish", {"summary": final})),
    )


async def test_agent_sends_mail_after_approval(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog
) -> None:
    asked = _answer_confirmations(ctx, True)
    _mail_script(llm, "Письмо Ивану отправлено.")
    run_id = await ctx.agent.run(TASK, "text")
    result = await ctx.agent.wait(run_id)  # type: ignore[attr-defined]

    assert result.status == "done" and result.summary == "Письмо Ивану отправлено."
    assert asked == [
        ("Нажать «Отправить» в «Новое письмо — Почта»?", "Будет отправлено: «Отчёт готов, смотри вложение»")
    ]
    assert ui.clicks == ["Написать", "Отправить"]
    assert ui.typed == [("Кому", "ivan@example.com"), ("Текст письма", "Отчёт готов, смотри вложение")]

    states = events.of("agent.state")
    assert (states[0]["status"], states[0]["text"]) == ("planning", "Составляю план…")
    assert any(s["status"] == "running" and s["step"] == 2 and s["total"] == 4 for s in states)
    assert any(s["status"] == "running" and s["text"] == "Нажимаю «Написать»" for s in states)
    assert any(s["status"] == "waiting" for s in states)
    assert (states[-1]["status"], states[-1]["step"], states[-1]["total"]) == ("done", 4, 4)
    steps = events.of("agent.step")
    assert [(s["index"], s["status"]) for s in steps] == [
        (1, "running"),
        (1, "ok"),
        (2, "running"),
        (2, "ok"),
        (3, "running"),
        (3, "ok"),
        (4, "running"),
        (4, "ok"),
    ]
    assert steps[-1]["title"] == "Нажимаю «Отправить»"

    # итог — сообщением в новый чат режима «Агент»
    chat_id = events.of("chat.created")[0]["chat"]["id"]
    chat = ctx.ai.get_chat(chat_id)  # type: ignore[attr-defined]
    assert chat["chat"]["mode"] == "agent"
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
    final = chat["messages"][1]
    assert final["content"] == "Письмо Ивану отправлено."
    assert [a["title"] for a in final["meta"]["actions"]] == [
        "Нажать «Написать»",
        "Ввести текст в «Кому»",
        "Ввести текст в «Текст письма»",
        "Нажать «Отправить»",
    ]
    assert events.of("chat.done")[-1]["message_id"] == final["id"]
    assert events.of("toast")[-1]["title"] == "Агент"

    # модель видела план, снимок окна и инструменты; старые снимки скрыты
    plan_request, first_step = llm.requests[0], llm.requests[1]
    assert plan_request["response_format"]["type"] == "json_schema"
    names = {t["function"]["name"] for t in first_step["tools"]}
    assert {"ui_snapshot", "ui_click", "ui_type", "press_keys", "launch_app", "finish"} <= names
    assert "screenshot" not in names  # модель без зрения
    first_message = first_step["messages"][1]["content"]
    assert "План:\n1. Открыть почту" in first_message and "e1 Button «Написать»" in first_message
    last = llm.requests[-1]["messages"]
    hidden = [m for m in last if "(устаревший снимок окна скрыт)" in str(m.get("content"))]
    current = [m for m in last if "[Снимок окна]" in str(m.get("content"))]
    assert len(hidden) >= 2 and len(current) == 1


async def test_agent_respects_denial(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    asked = _answer_confirmations(ctx, False)
    _mail_script(llm, "Письмо не отправлено: вы отклонили отправку.")
    result = await ctx.agent.wait(await ctx.agent.run(TASK))  # type: ignore[attr-defined]
    assert len(asked) == 1
    assert ui.clicks == ["Написать"]  # «Отправить» не нажата
    denial = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][-1]
    assert "Пользователь отклонил" in denial["content"]
    assert result.status == "done" and "отклонили" in result.summary
    assert result.actions[-1] == {"title": "Нажать «Отправить»", "ok": False, "detail": "Отклонено"}


async def test_agent_stop(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog) -> None:
    waiting = asyncio.Event()

    async def hang(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("не дойдёт")

    llm.add(PLAN, hang)
    run_id = await ctx.agent.run(TASK)
    await asyncio.wait_for(waiting.wait(), 5)
    await ctx.agent.stop_run()
    result = await ctx.agent.wait(run_id)  # type: ignore[attr-defined]
    assert result.status == "stopped" and result.summary.startswith("Остановлено по вашей команде")
    assert events.of("agent.state")[-1]["status"] == "stopped"
    assert events.of("chat.done")[-1]["stopped"] is True
    await ctx.agent.stop_run()  # повторная остановка ничего не ломает


async def test_agent_step_limit_and_always_confirm(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    ctx.settings.patch({"ai": {"max_agent_steps": 3}})
    llm.add(PLAN, *[calls(("press_keys", {"combo": "ctrl+c"})) for _ in range(4)])
    result = await ctx.agent.wait(await ctx.agent.run("Копируй без конца"))  # type: ignore[attr-defined]
    assert result.status == "error" and "не уложился в 3 шагов" in result.summary
    assert result.summary.endswith("Успел: Нажать ctrl+c, Нажать ctrl+c, Нажать ctrl+c.")

    ctx.settings.patch({"ai": {"agent_confirm": "always", "max_agent_steps": 10}})
    asked = _answer_confirmations(ctx, True)
    llm.add(
        PLAN,
        calls(("ui_click", {"id": "e2"})),
        calls(("press_keys", {"combo": "ctrl+r"})),
        calls(("finish", {"summary": "Готово"})),
    )
    result = await ctx.agent.wait(await ctx.agent.run("Обнови входящие"))  # type: ignore[attr-defined]
    assert [title for title, _ in asked] == [
        "Нажать «Входящие» в «Входящие — Почта»?",
        "Нажать ctrl+r в «Входящие — Почта»?",
    ]
    assert result.status == "done"


async def test_password_field_needs_confirmation(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    ui.elements = [UiElement("e1", "Пароль", "Edit", (0, 0, 10, 10), password=True)]
    ui.window = "Вход — Банк"
    asked = _answer_confirmations(ctx, False)
    llm.add(
        PLAN, calls(("ui_type", {"id": "e1", "text": "secret"})), calls(("finish", {"summary": "Не вошёл"}))
    )
    await ctx.agent.wait(await ctx.agent.run("Войди в банк"))  # type: ignore[attr-defined]
    assert asked == [("Ввести пароль в поле «Пароль» в «Вход — Банк»?", "")]
    assert ui.typed == []


async def test_agent_via_chat_phrase(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog) -> None:
    llm.add(reply('{"steps": ["Открыть почту"]}'), calls(("finish", {"summary": "Почта открыта, сэр."})))
    result = await ctx.ai.ask("агент, открой почту", source="voice")
    assert result.text == "Почта открыта, сэр." and result.error == ""
    messages = ctx.ai.get_chat(result.chat_id)["messages"]  # type: ignore[attr-defined]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "агент, открой почту"
    assert "Задача: открой почту" in llm.requests[1]["messages"][1]["content"]
    assert len(events.of("chat.done")) == 1  # итог пишет агент, без дублей


async def test_agent_without_windows_ui_and_guards(ctx: AppContext, llm: ScriptedLlm) -> None:
    # на Linux элементов окон нет — агент получает понятную ошибку и может работать клавишами
    llm.add(PLAN, calls(("ui_snapshot", {})), calls(("finish", {"summary": "Не получилось"})))
    result = await ctx.agent.wait(await ctx.agent.run("Нажми кнопку"))  # type: ignore[attr-defined]
    assert result.status == "done"
    assert "только в Windows" in llm.requests[1]["messages"][1]["content"]
    tool = next(m for m in llm.requests[2]["messages"] if m["role"] == "tool")
    assert "только в Windows" in tool["content"] and "Открытые окна" in tool["content"]

    with pytest.raises(UserFacingError, match="Опишите задачу"):
        await ctx.agent.run("  ")
    ctx.secrets.set("ai_api_key", "")
    with pytest.raises(UserFacingError, match="ключ API"):
        await ctx.agent.run("Открой почту")


async def test_agent_is_one_at_a_time(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    release = asyncio.Event()

    async def slow(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"steps": []}'}}]})

    llm.add(slow, calls(("finish", {"summary": "Готово"})))
    run_id = await ctx.agent.run("Первая задача")
    with pytest.raises(UserFacingError, match="уже работает"):
        await ctx.agent.run("Вторая задача")
    release.set()
    assert (await ctx.agent.wait(run_id)).status == "done"  # type: ignore[attr-defined]
    assert ctx.agent.state()["status"] == "done"  # type: ignore[attr-defined]


async def test_agent_other_actions(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, events: EventLog) -> None:
    async def run_text(text: str, source: str) -> ExecResult | None:
        return ExecResult(True, "Свернуть все окна", "Выполнено") if source == "agent" else None

    ctx.commands.run_text = run_text  # type: ignore[method-assign]
    ctx.settings.patch({"ai": {"vision": True}})
    asked = _answer_confirmations(ctx, False)
    llm.add(
        PLAN,
        calls(("focus_window", {"title": "spotify"}), ("launch_app", {"name": "PowerShell"})),
        calls(
            ("open_url", {"url": "https://mail.example.com"}), ("run_command", {"text": "сверни все окна"})
        ),
        calls(("screenshot", {})),
        calls(("wait", {"seconds": 0.5})),
        calls(("finish", {"summary": "Готово"})),
    )
    result = await ctx.agent.wait(await ctx.agent.run("Разное"))  # type: ignore[attr-defined]
    assert result.status == "done"
    platform_calls = ctx.platform.calls  # type: ignore[attr-defined]
    assert ("window_action", ("focus", 103)) in platform_calls  # окно Spotify
    assert not any(name == "launch" for name, _ in platform_calls)  # PowerShell не разрешили
    assert asked == [("Запустить «PowerShell»?", "Командная строка может изменить систему")]
    assert ("open_url", ("https://mail.example.com",)) in platform_calls
    titles = [a["title"] for a in result.actions]
    assert titles == [
        "Переключиться на «spotify»",
        "Открыть «PowerShell»",
        "Открыть mail.example.com",
        "Сверни все окна",
    ]
    # снимок экрана уходит модели картинкой; в следующем запросе остаётся только последний
    after_screenshot = llm.requests[4]["messages"]
    images = [m for m in after_screenshot if isinstance(m.get("content"), list)]
    assert len(images) == 1 and images[0]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert "screenshot" in {t["function"]["name"] for t in llm.requests[1]["tools"]}
