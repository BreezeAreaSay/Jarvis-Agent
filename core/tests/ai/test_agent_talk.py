"""Разговор с агентом посреди работы: поправки («нет, не Ивану, а Маше») фразой и в чате, вопрос ask_user
(ответ в чате, голосом, нет ответа), «да» / «нет» фразой на его запрос разрешения, проверка по свежему снимку
перед «готово», снимок экрана — монитор окна впереди."""

import asyncio
import io
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jarvis.ai import agent as agent_module
from jarvis.ai import vision
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from tests.ai.fakes import FakeUi, ScriptedLlm, Spec, calls, completion, reply

PLAN = reply(json.dumps({"steps": ["Открыть почту", "Написать письмо", "Отправить"]}))


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    ctx.settings.patch({"ai": {"vision": "off"}})  # модель без зрения: в ответах только текст
    return fake


@pytest.fixture
def spoken(ctx: AppContext) -> list[str]:
    said: list[str] = []

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        said.append(text)

    ctx.tts.say = say  # type: ignore[method-assign]
    return said


def after(spec: Spec, event: Callable[[], Any]) -> Callable[[httpx.Request, dict[str, Any]], httpx.Response]:
    """Ответ модели, пока она думала над которым человек что-то сказал агенту."""

    def respond(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        event()
        return httpx.Response(200, json=completion(spec, body.get("model", "")))

    return respond


def tool_results(llm: ScriptedLlm) -> list[str]:
    return [str(m["content"]) for m in llm.requests[-1]["messages"] if m["role"] == "tool"]


async def until(check: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


async def test_interjection_reaches_the_model_and_finish_is_checked(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    llm.add(
        PLAN,
        after(calls(("ui_click", {"id": "e1", "step": 2})), lambda: agent.interject("нет, не Ивану, а Маше")),
        after(
            calls(("finish", {"summary": "Письмо Маше готово."})), lambda: agent.interject("и подпиши «Дима»")
        ),
        calls(("finish", {"summary": "Письмо Маше готово."})),
        calls(("finish", {"summary": "Письмо Маше готово, подпись «Дима»."})),
    )
    result = await agent.wait(await agent.run("Напиши Ивану письмо"))

    assert result.status == "done" and result.summary == "Письмо Маше готово, подпись «Дима»."
    tools = tool_results(llm)
    # поправка — с ответом на ближайшее действие и до снимка окна: устаревший снимок вырезан, она осталась
    assert tools[0].startswith("Пользователь перебил: «нет, не Ивану, а Маше»")
    assert "(устаревший снимок окна скрыт)" in tools[0]
    # реплика перед итогом — работа продолжается
    assert tools[1] == "Пользователь перебил: «и подпиши «Дима»»\nУчти это, прежде чем заканчивать."
    # «готово» после действий на экране — сначала проверка по свежему снимку, и только один раз
    assert tools[2].startswith("Проверка перед итогом. Задача: «Напиши Ивану письмо».")
    assert "[Снимок окна]" in tools[2] and len(tools) == 3
    assert agent.interject("ещё одно") == ""  # агент закончил — реплика уже не ему


async def test_interjection_when_the_model_was_about_to_stop(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    llm.add(
        PLAN,
        after(reply("Кажется, всё."), lambda: agent.interject("ещё закрой окно")),
        calls(("finish", {"status": "partial", "summary": "Окно не закрыл."})),
    )
    result = await agent.wait(await agent.run("Открой почту"))
    assert result.summary == "Окно не закрыл."
    assert llm.requests[-1]["messages"][-2:] == [
        {"role": "assistant", "content": "Кажется, всё."},
        {"role": "user", "content": "Пользователь перебил: «ещё закрой окно»\nУчти это и продолжай работу."},
    ]


async def test_finish_without_changes_or_not_done_is_not_rechecked(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    llm.add(PLAN, calls(("wait", {"seconds": 0.01})), calls(("finish", {"summary": "Почта открыта."})))
    assert (await agent.wait(await agent.run("Посмотри почту"))).summary == "Почта открыта."
    llm.add(
        PLAN,
        calls(("press_keys", {"combo": "ctrl+n"})),
        calls(("finish", {"status": "partial", "summary": "Не нашёл адрес."})),
    )
    result = await agent.wait(await agent.run("Напиши письмо"))
    assert result.outcome == "partial" and len(tool_results(llm)) == 1


async def test_phrases_while_the_agent_works(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, spoken: list[str]
) -> None:
    agent: Any = ctx.agent
    thinking, release = asyncio.Event(), asyncio.Event()

    async def hold(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        thinking.set()
        await release.wait()
        return httpx.Response(200, json=completion(calls(("ui_click", {"id": "e1"})), body.get("model", "")))

    llm.add(PLAN, hold, calls(("finish", {"status": "partial", "summary": "Открыл письмо Маше."})))
    run_id = await agent.run("Напиши Ивану")
    await asyncio.wait_for(thinking.wait(), 5)
    assert agent.busy and not agent.waiting_answer

    # не готовая команда — агенту поправкой, «Понял, учту» вслух
    res = await ctx.dispatcher.handle("нет, не Ивану, а Маше", "voice")
    assert res["kind"] == "ai" and res["text"] == "Агенту — поправка к задаче"
    assert (
        res["chat_id"] == agent.current.chat_id and res["history"]["detail"] == "Агенту — поправка к задаче"
    )
    assert spoken == ["Понял, учту."]
    # готовая команда, пока агент работает, — по-прежнему команда
    res = await ctx.dispatcher.handle("сделай громче", "text")
    assert res["kind"] == "command" and res["result"]["ok"]

    release.set()
    await agent.wait(run_id)
    assert tool_results(llm)[0].startswith("Пользователь перебил: «нет, не Ивану, а Маше»")
    assert not agent.busy


async def test_ask_user_answer_in_chat(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    llm.add(
        PLAN,
        calls(("ask_user", {"question": "Какой Ане: Петровой или Смирновой?", "step": 1})),
        calls(("finish", {"summary": "Написал Ане Петровой."})),
    )
    run_id = await agent.run("Напиши Ане")
    await until(lambda: agent.waiting_answer)
    run = agent.current
    assert (run.status, run.text) == ("waiting", "Жду ответа: Какой Ане: Петровой или Смирновой?")
    # пока агент ждёт, сообщение в его чат — ответ, а не новый ход
    sent = await ctx.ai.post_message(run.chat_id, "Петровой")  # type: ignore[attr-defined]
    assert sent["reply_id"] == run.reply_id
    result = await agent.wait(run_id)

    assert result.status == "done" and result.summary == "Написал Ане Петровой."
    assert tool_results(llm) == ["Ответ пользователя: «Петровой»"]  # вопрос ничего не менял — без проверки
    assert result.actions[0] == {
        "title": "Вопрос: Какой Ане: Петровой или Смирновой?",
        "ok": True,
        "detail": "Петровой",
    }
    messages = ctx.ai.get_chat(run.chat_id)["messages"]  # type: ignore[attr-defined]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "Напиши Ане"),
        ("assistant", "Какой Ане: Петровой или Смирновой?"),
        ("user", "Петровой"),
        ("assistant", "Написал Ане Петровой."),
    ]


async def test_ask_user_by_voice_and_without_answer(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent: Any = ctx.agent
    listened: list[float] = []

    async def capture_text(timeout: float = 15.0, *, punct: bool = True) -> str:
        listened.append(timeout)
        return "Смирновой"

    ctx.voice.capture_text = capture_text  # type: ignore[method-assign]
    llm.add(
        PLAN,
        calls(("ask_user", {"question": "Какой Ане?"})),
        calls(("finish", {"summary": "Написал Ане Смирновой."})),
    )
    await agent.wait(await agent.run("Напиши Ане", "voice"))
    assert "Какой Ане?" in spoken and listened == [20.0]  # вопрос вслух, ответ — сразу, без «Джарвис»
    assert tool_results(llm) == ["Ответ пользователя: «Смирновой»"]

    monkeypatch.setattr(agent_module, "ASK_TIMEOUT_S", 0.05)
    llm.add(
        PLAN,
        calls(("ask_user", {"question": "Какой файл?"}), ("ask_user", {"question": " "})),
        calls(("finish", {"status": "partial", "summary": "Не знаю, какой файл."})),
    )
    result = await agent.wait(await agent.run("Открой файл"))
    first, second = tool_results(llm)
    assert first.startswith("Пользователь не ответил") and second == "Ошибка: нет вопроса"
    assert result.actions[0]["detail"] == "Без ответа" and result.outcome == "partial"


async def test_yes_or_no_phrase_answers_the_agent_approval(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    asked: list[dict[str, Any]] = []
    ctx.bus.on("confirm.request", lambda event: asked.append(event.data))

    def send_mail(final: str) -> None:
        llm.add(
            PLAN,
            calls(("ui_click", {"id": "e1"})),
            calls(("ui_type", {"id": "e3", "text": "Отчёт готов"})),
            calls(("ui_click", {"id": "e4"})),
            calls(("finish", {"status": "partial", "summary": final})),
        )

    send_mail("Отправил.")
    run_id = await agent.run("Отправь отчёт")
    await until(lambda: bool(asked))
    res = await ctx.dispatcher.handle("да", "text")
    assert res["text"] == "Решение для агента"
    await agent.wait(run_id)
    assert ui.clicks == ["Написать", "Отправить"]
    assert not any("Пользователь перебил" in t for t in tool_results(llm))

    # «нет» с поправкой — и отказ, и поправка модели
    ui.__init__()  # снова «Входящие»
    send_mail("Не отправил: напишу Маше.")
    run_id = await agent.run("Отправь отчёт")
    await until(lambda: len(asked) == 2)
    assert agent.interject("нет, напиши лучше Маше") == "approval"
    await agent.wait(run_id)
    assert ui.clicks == ["Написать"]  # «Отправить» не нажата
    last = tool_results(llm)[-1]
    assert (
        last.startswith("Пользователь перебил: «нет, напиши лучше Маше»") and "Пользователь отклонил" in last
    )


async def test_late_plan_is_not_cut_with_old_snapshots(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    steps = [
        calls(("ui_click", {"id": "e1"})),
        calls(("ui_type", {"id": "e1", "text": "ivan@example.com"})),
        calls(("finish", {"summary": "Готово."})),
        calls(("finish", {"summary": "Готово."})),
    ]
    first_step = asyncio.Event()

    async def respond(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if "response_format" in body:  # план приходит, когда агент уже взялся за дело
            await first_step.wait()
            return httpx.Response(200, json=completion(PLAN, body.get("model", "")))
        first_step.set()
        return httpx.Response(200, json=completion(steps.pop(0), body.get("model", "")))

    llm.add(*[respond] * (len(steps) + 1))
    await agent.wait(await agent.run("Напиши Ивану"))
    assert "План:" not in llm.requests[1]["messages"][1]["content"]  # в первом сообщении плана ещё нет
    tools = tool_results(llm)
    with_plan = [t for t in tools if "План работы" in t]
    # план — до снимка окна: снимок этого шага давно вырезан, а план остался
    assert len(with_plan) == 1 and "1. Открыть почту" in with_plan[0]
    assert with_plan[0].endswith("(устаревший снимок окна скрыт)")


async def test_phrase_after_the_result_is_a_new_request(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    seen: dict[str, Any] = {}
    original = agent.finish_summary

    def finish_summary(run: Any, args: dict[str, Any]) -> str:
        summary = original(run, args)
        # итог принят, агент вот-вот закончит: реплика — уже не ему (иначе бы потерялась)
        seen["busy"], seen["kind"] = agent.busy, agent.interject("нет, не Ане, а Маше")
        return str(summary)

    agent.finish_summary = finish_summary
    llm.add(PLAN, calls(("finish", {"summary": "Написал Ане."})))
    await agent.wait(await agent.run("Напиши Ане"))
    assert seen == {"busy": False, "kind": ""}


async def test_codex_or_cli_run_continues_with_a_late_interjection(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent

    class Runner:
        """Codex или программа человека: первый заход закончился без finish, пока человек перебивал."""

        def __init__(self) -> None:
            self.firsts: list[str] = []

        def problem(self) -> str:
            return ""

        async def complete(self, messages: Any, json_schema: Any = None, usage: Any = None) -> dict[str, Any]:
            return {"steps": ["Открыть почту"]}

        async def run_agent(self, agent: Any, run: Any, *, system: str, first: str, specs: Any) -> str:
            self.firsts.append(first)
            if len(self.firsts) == 1:
                agent.interject("и закрой окно")
                return "Кажется, всё."
            return str(agent.finish_summary(run, {"status": "partial", "summary": "Окно не закрыл."}))

    runner = Runner()
    agent._chatgpt = lambda: runner
    result = await agent.wait(await agent.run("Открой почту"))
    assert result.summary == "Окно не закрыл." and len(runner.firsts) == 2
    again = runner.firsts[1]
    assert again.startswith("Продолжение той же задачи: «Открой почту». Уже сделано: ничего.")
    assert "Пользователь перебил: «и закрой окно»" in again and "[Снимок окна]" in again


async def test_windows_that_open_or_close_are_reported(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    platform: Any = ctx.platform
    dialog = WindowInfo(9001, "Сохранение документа", "notepad.exe", 77)

    def open_dialog() -> None:
        platform.windows.append(dialog)

    def close_dialog() -> None:
        platform.windows.remove(dialog)

    llm.add(
        PLAN,
        after(calls(("press_keys", {"combo": "ctrl+s"})), open_dialog),
        after(calls(("press_keys", {"combo": "esc"})), close_dialog),
        calls(("finish", {"status": "partial", "summary": "Не сохранил."})),
    )
    await agent.wait(await agent.run("Сохрани документ"))
    first, second, *_ = tool_results(llm)
    # экран текстом — и модели без картинок: что открылось и что закрылось после действия
    assert "Окна на экране с прошлого снимка" in first
    assert "появилось: «Сохранение документа» (notepad.exe)" in first
    assert "закрылось: «Сохранение документа» (notepad.exe)" in second and "появилось" not in second


async def test_new_windows_notifications_are_reported(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    from jarvis.plugins.notify_voice import Toast

    agent: Any = ctx.agent

    class Source:
        def __init__(self, access: str) -> None:
            self.items = [Toast(1, "Почта", "Старое письмо")]
            self.answer, self.asked = access, 0

        async def access(self, request: bool = False) -> str:
            self.asked += 1
            return self.answer

        async def fetch(self) -> list[Toast]:
            return list(self.items)

        async def close(self) -> None:
            return None

    source = agent.toast_source = Source("allowed")

    def arrive() -> None:
        source.items.append(Toast(2, "Telegram", "Аня", "Привет, ты где?"))

    llm.add(
        PLAN,
        after(calls(("press_keys", {"combo": "ctrl+r"})), arrive),
        calls(("finish", {"status": "partial", "summary": "Обновил."})),
    )
    await agent.wait(await agent.run("Обнови почту"))
    first = tool_results(llm)[0]
    # пришло во время работы — текстом, как данные; то, что было до начала, не повторяется
    assert "Новые уведомления Windows" in first and "Telegram: Аня — Привет, ты где?" in first
    assert "Старое письмо" not in first and source.asked == 1

    # нет доступа к уведомлениям — молча, и Windows не спрашиваем на каждом шаге
    denied = agent.toast_source = Source("denied")
    agent._toast_access, agent._toast_checked = "", -1e9
    llm.add(
        PLAN,
        after(
            calls(("press_keys", {"combo": "ctrl+r"})),
            lambda: denied.items.append(Toast(3, "Telegram", "Ещё")),
        ),
        calls(("finish", {"status": "partial", "summary": "Обновил."})),
    )
    await agent.wait(await agent.run("Обнови почту"))
    assert "уведомления" not in tool_results(llm)[0] and denied.asked == 1


def test_screen_shot_shows_the_monitor_of_the_front_window() -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (448, 144), (10, 10, 10)).save(buffer, "PNG")  # экран 4480×1440, снятый в 10 раз мельче
    screen = (0, 0, 4480, 1440)  # слева 1920×1080, справа 2560×1440
    right = (1920, 0, 4480, 1440)
    assert vision.screen_part(screen, right) == right
    assert vision.screen_part(screen, None) == (0, 0, 4480, 1440)
    assert vision.screen_part(screen, (5000, 0, 6000, 1000)) == (0, 0, 4480, 1440)  # монитора нет на снимке
    assert vision.screen_part((-1920, 0, 3840, 1080), (-1920, 0, 0, 1080)) == (-1920, 0, 0, 1080)

    shot = vision.render_screen(
        buffer.getvalue(), screen, monitor=right, focus=(2000, 100, 3000, 900), title="Почта"
    )
    assert shot.rect == right and shot.hwnd == 0 and (shot.width, shot.height) == (256, 144)
    assert shot.to_screen(0, 0) == (1920, 0) and shot.to_screen(256, 144) == (4480, 1440)
    caption = shot.caption()
    assert caption.startswith("Снимок экрана — 256×144 точек; окно впереди «Почта» обведено жёлтым")
