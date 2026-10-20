"""«Что на экране?» за один ход: вопрос о том, что видно, — снимок экрана к вопросу в обычный чат (модель
с картинками отвечает сразу), а не агенту; дела и то, чего на экране нет, — агенту, как раньше; модель без
картинок — тоже агенту."""

import asyncio
from typing import Any

import pytest

from jarvis.ai import screen
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from tests.ai.fakes import ScriptedLlm, reply


@pytest.mark.parametrize(
    "text",
    [
        "что на экране?",
        "Джарвис, что это за модель?",
        "трейлер какого фильма сейчас у нас на экране?",
        "где ошибка в коде?",
        "прочитай, что здесь написано",
        "переведи текст на экране",
        "а это что?",
        "что за игра?",
        "посмотри на экран и скажи, что не так",
    ],
)
def test_screen_questions(text: str) -> None:
    assert screen.is_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "что такое квантовая физика",
        "напиши Ане, что я опоздаю",  # дело — агенту
        "закрой это окно",
        "сделай скриншот",
        "что мне написала Аня в телеграме",  # надо открыть — агенту
        "что нового в почте",
        "что это значит в программировании",
        "что за погода сегодня в москве",
    ],
)
def test_not_screen_questions(text: str) -> None:
    assert not screen.is_question(text)


def _pictures(body: dict[str, Any]) -> list[str]:
    last = body["messages"][-1]
    content = last["content"] if isinstance(last["content"], list) else []
    return [p["image_url"]["url"] for p in content if p.get("type") == "image_url"]


async def test_screen_question_is_one_turn_with_a_snapshot(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    started: list[str] = []
    ctx.agent.run = lambda task, *a, **k: started.append(task)  # type: ignore[method-assign]
    llm.add(reply("Это Lamborghini Revuelto, сэр."))
    result = await ctx.ai.ask("Джарвис, что это за модель?", source="voice")
    assert result.text == "Это Lamborghini Revuelto, сэр." and started == []  # без агента
    (body,) = llm.requests
    pictures = _pictures(body)
    assert pictures and all(url.startswith("data:image/jpeg;base64,") for url in pictures)
    text = body["messages"][-1]["content"][0]["text"]
    assert text.startswith("Джарвис, что это за модель?\n\n[Jarvis приложил снимок экрана, сделанный сейчас")
    assert "окно впереди «Jarvis — Google Chrome» (chrome.exe)" in text
    # в чате — слова человека и снимок, без пометки для модели
    user = next(m for m in ctx.ai.get_chat(result.chat_id)["messages"] if m["role"] == "user")  # type: ignore[attr-defined]
    assert user["content"] == "Джарвис, что это за модель?" and user["meta"]["images"] == pictures

    # из окна чата — так же
    llm.add(reply("На экране браузер со страницей Jarvis."))
    await ctx.ai.post_message(result.chat_id, "что на экране?")  # type: ignore[attr-defined]
    for _ in range(200):
        if len(llm.requests) == 2:
            break
        await asyncio.sleep(0.01)
    assert _pictures(llm.requests[1]) and started == []


async def test_without_vision_the_agent_reads_the_window(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})
    started: list[str] = []

    async def run(task: str, *args: Any, **kwargs: Any) -> str:
        started.append(task)
        raise UserFacingError("агент в этом тесте не нужен")

    ctx.agent.run = run  # type: ignore[method-assign]
    await ctx.ai.ask("что на экране?", source="voice")
    assert started == ["что на экране?"]  # модель без картинок: прочтёт окно агент (UI Automation)
    assert not llm.requests or not _pictures(llm.requests[0])


async def test_follow_up_sees_the_same_snapshot(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    llm.add(reply("Это Lamborghini Revuelto, сэр."))
    first = await ctx.ai.ask("что это за модель?", source="text")
    llm.add(reply("2023 года, сэр."))
    await ctx.ai.ask("а какого она года?", chat_id=first.chat_id, source="text")
    shot = _pictures(llm.requests[0])
    asked = llm.requests[1]["messages"]
    assert asked[-1]["content"] == "а какого она года?"  # нового снимка нет: вопрос не о том, что видно
    earlier = next(m for m in asked if m["role"] == "user" and isinstance(m["content"], list))
    assert [p["image_url"]["url"] for p in earlier["content"] if p["type"] == "image_url"] == shot

    llm.add(reply("Пожалуйста."))
    await ctx.ai.ask("спасибо", chat_id=first.chat_id, source="text")  # снимок старше — только пометка
    assert not any(isinstance(m["content"], list) for m in llm.requests[2]["messages"])


async def test_agent_mode_chat_keeps_the_agent(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"vision": "on"}})
    started: list[str] = []

    async def run(task: str, *args: Any, **kwargs: Any) -> str:
        started.append(task)
        raise UserFacingError("агент в этом тесте не нужен")

    ctx.agent.run = run  # type: ignore[method-assign]
    result = await ctx.ai.ask("что на экране?", source="text", mode="agent")
    assert started == ["что на экране?"]  # человек выбрал «Агент» — снимок не снимаем впустую
    user = next(m for m in ctx.ai.get_chat(result.chat_id)["messages"] if m["role"] == "user")  # type: ignore[attr-defined]
    assert not user["meta"].get("images")
