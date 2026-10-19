"""Подсказки модели о программах: приёмы программ и браузеров, готовые команды паков (по id), свои команды,
рецепт Telegram через MCP и инструкции серверов; сразу агент для дел в программах; скорость ответа."""

from typing import Any

import pytest

from jarvis import paths
from jarvis.ai import apphints, playbooks
from jarvis.ai.llm import LlmClient, LlmConfig, _quirk_for, reasoning_effort
from jarvis.ai.prompts import APPS_HEADER
from jarvis.ai.providers import PRESETS
from jarvis.ai.tools import ToolEnv, ToolRegistry
from jarvis.commands.model import CommandIn
from jarvis.commands.packs import PackStore
from jarvis.context import AppContext
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply
from tests.conftest import EventLog

TELEGRAM_TOOLS = [
    (
        "mcp__telegram__get_chats",
        "Список чатов",
        {"type": "object", "properties": {"page": {}, "page_size": {}}},
    ),
    (
        "mcp__telegram__search_contacts",
        "Найти контакт",
        {"type": "object", "properties": {"query": {}}, "required": ["query"]},
    ),
    (
        "mcp__telegram__get_messages",
        "Сообщения",
        {"type": "object", "properties": {"chat_id": {}, "limit": {}}},
    ),
    (
        "mcp__telegram__send_file",
        "Отправить файл",
        {"type": "object", "properties": {"chat_id": {}, "file_path": {}}},
    ),
    (
        "mcp__telegram__send_message",
        "Отправить сообщение",
        {"type": "object", "properties": {"chat_id": {}, "message": {}}, "required": ["chat_id", "message"]},
    ),
    ("mcp__telegram__delete_message", "Удалить", {"type": "object", "properties": {"chat_id": {}}}),
]


def _fake_mcp(ctx: AppContext, instructions: str = "") -> None:
    def spec(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": schema},
        }

    ctx.mcp.tools = lambda: [spec(*t) for t in TELEGRAM_TOOLS]  # type: ignore[method-assign]
    ctx.mcp.tool_title = lambda name: f"Telegram: {name.rsplit('__', 1)[-1]}"  # type: ignore[attr-defined]
    ctx.mcp.server_notes = lambda: [  # type: ignore[attr-defined]
        {"name": "Telegram", "prefix": "mcp__telegram__", "instructions": instructions}
    ]


def test_playbook_library_is_consistent() -> None:
    ids = [p.id for p in playbooks.PLAYBOOKS]
    assert len(ids) == len(set(ids)) and len(ids) >= 30
    packs = {p.id for p in PackStore(paths.resource_dir() / "packs").all()}
    for p in playbooks.PLAYBOOKS:
        assert p.title and p.text.startswith("- ") and (not p.chat or p.chat.startswith("- ")), p.id
        assert all(exe == exe.casefold() and exe.endswith(".exe") for exe in p.exes), p.id
        assert set(p.packs) <= packs, p.id
    assert set(playbooks.PACK_WORDS) <= packs


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("найди на ютубе обзор нового айфона", "youtube"),
        ("закрой вкладку в хроме", "browser"),
        ("проложи маршрут до работы", "maps"),
        ("найди на озоне наушники", "shopping"),
        ("напиши письмо Ивану", "mail"),
        ("создай в ворде документ", "word"),
        ("включи камеру в зуме", "zoom"),
        ("открой библиотеку стима", "steam"),
        ("напиши в вотсапе маме", "whatsapp"),
        ("переведи на английский", "translate"),
    ],
)
def test_playbooks_found_by_words(text: str, expected: str) -> None:
    assert expected in [p.id for p in playbooks.relevant(text, [], [])]


def test_packs_for_words_and_playbooks() -> None:
    found = playbooks.relevant("сделай в фотошопе новый слой и закрой вкладку в хроме", [], [])
    assert playbooks.packs_for("сделай в фотошопе новый слой", found)[:3] == [
        "chrome",
        "firefox",
        "yandex_browser",
    ]
    assert "photoshop" in playbooks.packs_for("сделай в фотошопе новый слой", found)
    assert "Как сделать в «Браузер»" in playbooks.render_chat(found)


async def test_pack_commands_installed_by_id_and_others_as_keys(ctx: AppContext) -> None:
    ctx.commands.set_pack("telegram", True)  # type: ignore[attr-defined]
    ctx.commands.set_enabled("pack.telegram.quit", False)  # type: ignore[attr-defined]
    found = ctx.commands.app_commands(["Telegram.exe"], ["zoom", "steam"])  # type: ignore[attr-defined]
    by_pack = {p["pack"]: p for p in found}
    telegram = by_pack["telegram"]
    assert telegram["installed"] is True
    items = {i["id"]: i for i in telegram["items"]}
    assert items["pack.telegram.saved"]["keys"] == "ctrl+0" and "pack.telegram.quit" not in items
    assert by_pack["zoom"]["installed"] is False and by_pack["zoom"]["items"][0]["keys"] == "alt+a"

    text = apphints.commands_block(ctx, ["Telegram.exe"], ["zoom", "steam"])
    assert "Готовые команды Jarvis" in text and "pack.telegram.saved — Избранное (ctrl+0)" in text
    assert "Сочетания клавиш программ" in text and "микрофон вкл/выкл — alt+a" in text
    assert "steam://" not in text  # адрес Steam открывает только команда пака


async def test_added_commands_are_always_offered(ctx: AppContext) -> None:
    created = ctx.commands.create(  # type: ignore[attr-defined]
        CommandIn(
            name="Рабочий режим",
            phrases=["включи рабочий режим"],
            actions=[{"type": "Keys.Press", "value": "win+d"}],  # type: ignore[list-item]
        )
    )
    text = apphints.added_block(ctx)
    assert f"{created['id']} — Рабочий режим («включи рабочий режим»)" in text


def test_telegram_recipe_uses_real_tool_names(ctx: AppContext) -> None:
    _fake_mcp(ctx, instructions="Всегда сначала вызывай get_chats. " + "x" * 3000)
    text = apphints.mcp_block(ctx, TELEGRAM_TOOLS)
    assert "Telegram через MCP (сервер «Telegram»)" in text
    assert (
        "mcp__telegram__search_contacts(query)" in text
        and "mcp__telegram__get_chats(page, page_size)" in text
    )
    assert "3. Прочитать сообщения: mcp__telegram__get_messages(chat_id, limit)" in text
    # отправка текста — первой, файл — после; удаление в рецепт не попадает
    assert "4. Написать: mcp__telegram__send_message(chat_id, message), mcp__telegram__send_file" in text
    assert "delete_message" not in text
    assert "Инструкции сервера MCP «Telegram»" in text and "Всегда сначала вызывай get_chats." in text
    assert len(text) < 3500  # инструкции сервера обрезаны


@pytest.mark.parametrize(
    ("text", "agent"),
    [
        ("напиши Ане в телеграме что буду в семь", True),
        ("создай в ворде документ с планом на неделю", True),
        ("ответь в дискорде Пете что всё готово", True),
        ("как в телеграме создать папку?", False),
        ("открой телеграм", False),
        ("какая завтра погода", False),
        ("найди на ютубе обзор айфона", False),  # сайт чат откроет сам
    ],
)
def test_app_task_detection(text: str, agent: bool) -> None:
    assert apphints.app_task(text) is agent


async def test_chat_hints_and_system_prompt(ctx: AppContext, llm: ScriptedLlm) -> None:
    _fake_mcp(ctx)
    tools = ToolRegistry(ctx).available("all")
    hints = await apphints.chat_hints(ctx, "напиши маме в телеграме привет", tools)
    assert "Как сделать в «Telegram»" in hints and "Telegram через MCP" in hints
    assert "Клавиши" not in hints or "Сочетания клавиш программ" in hints
    assert (
        await apphints.chat_hints(ctx, "напиши маме в телеграме привет", ToolRegistry(ctx).available("chat"))
        == ""
    )

    llm.add(reply("Открыл поиск на YouTube."))
    await ctx.ai.ask("найди на ютубе обзор айфона")  # type: ignore[attr-defined]
    system = llm.requests[0]["messages"][0]["content"]
    assert APPS_HEADER in system and "youtube.com/results?search_query" in system
    assert "пауза и пуск" not in system  # агенту — приёмы окна, чату — инструменты


async def test_app_task_goes_straight_to_agent(ctx: AppContext, llm: ScriptedLlm, events: EventLog) -> None:
    agent: Any = ctx.agent
    agent.ui, agent.settle_s, agent.launch_settle_s, agent.pointer_delay_s = FakeUi(), 0.0, 0.0, 0.0
    llm.add(
        reply('{"steps": ["Найти чат", "Написать"]}'),
        calls(("finish", {"status": "done", "summary": "Написал Ане."})),
    )
    result = await ctx.ai.ask("напиши Ане в телеграме что буду в семь")  # type: ignore[attr-defined]
    assert result.text == "Написал Ане."
    assert events.of("agent.state")[0]["task"] == "напиши Ане в телеграме что буду в семь"
    step = next(r for r in llm.requests if "response_format" not in r)
    assert "run_agent" not in {t["function"]["name"] for t in step["tools"]}  # это сам агент, не чат

    ctx.settings.patch({"ai": {"auto_agent": False}})
    llm.add(reply("Готов помочь."))
    before = len(events.of("agent.state"))
    await ctx.ai.ask("напиши Ане в телеграме что буду в семь")  # type: ignore[attr-defined]
    assert len(events.of("agent.state")) == before  # теперь решает чат


async def test_run_command_by_id_and_app_addresses(ctx: AppContext) -> None:
    ctx.commands.set_pack("telegram", True)  # type: ignore[attr-defined]
    tools = ToolRegistry(ctx).available("all")
    env = ToolEnv(ctx, ctx.ai.web, "text", "", user_text="")  # type: ignore[attr-defined,arg-type]
    out = await tools["run_command"].handler(env, {"text": "избранное", "id": "pack.telegram.saved"})
    assert out.ok and ctx.platform.calls[-2:] == [  # type: ignore[attr-defined]
        ("window_action", ("focus", "Telegram.exe")),
        ("press_keys", ("ctrl+0",)),
    ]
    out = await tools["open_url"].handler(env, {"url": "tg://resolve?domain=durov"})
    assert out.ok and ctx.platform.calls[-1] == ("open_url", ("tg://resolve?domain=durov",))  # type: ignore[attr-defined]
    out = await tools["open_url"].handler(env, {"url": "steam://run/1"})
    assert not out.ok


def _cfg(provider: str, model: str, speed: str) -> LlmConfig:
    return LlmConfig(
        provider, PRESETS[provider], "https://x", "k", model, 0.6, "", False, True, False, speed=speed
    )


def test_reasoning_effort_follows_speed() -> None:
    assert reasoning_effort(_cfg("openai", "gpt-5.4-mini", "fast")) == "low"
    assert reasoning_effort(_cfg("openai", "gpt-5.4-mini", "deep")) == "high"
    assert reasoning_effort(_cfg("openai", "gpt-5.4-mini", "balanced")) == ""
    assert reasoning_effort(_cfg("openai", "gpt-4.1", "fast")) == ""  # не думает — и не надо
    assert reasoning_effort(_cfg("gemini", "gemini-2.5-flash", "fast")) == "low"
    assert reasoning_effort(_cfg("gemini", "gemini-2.0-flash", "fast")) == ""
    client = LlmClient.__new__(LlmClient)
    body = client._payload(  # type: ignore[attr-defined]
        _cfg("openrouter", "openai/gpt-5.4", "fast"),
        [{"role": "user", "content": "привет"}],
        set(),
        tools=None,
        tool_choice=None,
        temperature=None,
        max_tokens=None,
        json_schema=None,
        stream=False,
    )
    assert body["reasoning"] == {"effort": "low"} and "reasoning_effort" not in body
    assert _quirk_for(
        "Unrecognized request argument supplied: reasoning_effort", {"reasoning_effort": "low"}
    ) == ("no_reasoning")


def test_codex_effort_follows_speed(ctx: AppContext) -> None:
    backend: Any = ctx.ai.chatgpt  # type: ignore[attr-defined]
    backend._models = [
        {
            "id": "m",
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
        },
        {"id": "n", "defaultReasoningEffort": "medium"},
    ]
    assert backend._effort("n", "text") == "low"  # «Быстро» — по умолчанию
    assert backend._effort("m", "text") == "medium"  # low модель не знает — её умолчание
    ctx.settings.patch({"ai": {"speed": "deep"}})
    assert backend._effort("n", "text") == "high" and backend._agent_effort("n") == "high"
    ctx.settings.patch({"ai": {"speed": "balanced"}})
    assert backend._effort("n", "text") == "medium" and backend._effort("n", "voice") == "low"
