"""Провайдеры через программы человека (Claude Code, Gemini CLI, Qwen Code) — целиком: настоящее ядро на порту
127.0.0.1, фальшивая программа (tests/ai/fake_cli.py) в отдельном процессе, инструменты Jarvis — по MCP
настоящим клиентом SDK. Сети и настоящих программ нет."""

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.ai.cli import errors
from jarvis.ai.llm import LlmError
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from tests.ai.cli_helpers import FakeCli, collect, live_core, until
from tests.ai.fakes import FakeUi


@pytest.fixture
async def core(data_dir: Path) -> AsyncIterator[AppContext]:
    async with live_core() as ctx:
        yield ctx


def fake_for(ctx: AppContext, provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    fake = FakeCli(ctx, provider, tmp_path, monkeypatch)
    ctx.settings.patch({"ai": {"provider": provider, "model": ""}})
    return fake


# --- Claude Code ------------------------------------------------------------------------------------------


async def test_claude_turn_streams_text_and_runs_jarvis_tools_over_mcp(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("JARVIS_CORE_TOKEN", "core-token-should-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/jarvis/app")
    core.settings.patch({"ai": {"proxy": "http://proxy.local:3128"}})
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    events = collect(core)
    fake.add(
        [{"say": "Ставлю громкость."}, {"tool": "set_volume", "args": {"level": 30}}, {"say": "Готово, сэр."}]
    )
    result = await core.ai.ask("Сделай громкость 30")
    assert result.error == ""
    assert result.text == "Ставлю громкость.\n\nГотово, сэр."
    assert result.actions == [{"title": "Громкость 30%", "ok": True, "detail": "Выполнено"}]
    assert ("volume_set", (30,)) in core.platform.calls  # type: ignore[attr-defined]

    tools = events.of("chat.tool")
    assert [t["status"] for t in tools] == ["running", "ok"] and tools[1]["title"] == "Громкость 30%"
    assert "".join(e["delta"] for e in events.of("chat.delta")) == result.text
    done = events.of("chat.done")[-1]
    assert done["text"] == result.text and done["usage"]["prompt_tokens"] == 105

    run = fake.runs[0]
    argv = run["argv"]
    # встроенные инструменты выключены, разрешены только инструменты Jarvis, настройки человека не читаются
    assert fake.arg(run, "--tools") == "" and fake.arg(run, "--permission-mode") == "dontAsk"
    assert "--strict-mcp-config" in argv and fake.arg(run, "--setting-sources") == ""
    assert fake.arg(run, "--allowedTools") == "mcp__jarvis__*" and "--disable-slash-commands" in argv
    assert fake.arg(run, "--output-format") == "stream-json" and "--include-partial-messages" in argv
    assert fake.arg(run, "--model") == "sonnet" and fake.arg(run, "--session-id")
    server = run["mcp_config"]["mcpServers"]["jarvis"]
    assert server["type"] == "http" and server["url"].endswith("/api/ai/mcp")
    assert set(server["headers"]) == {"X-Jarvis-Token", "X-Jarvis-Turn"} and server["timeout"] == 600000
    assert run["tools"] == [{"name": "set_volume", "text": "Громкость: 30%", "error": False, "images": 0}]
    # ключ API не уходит программе (иначе она платит ключом, а не подпиской); MCP — мимо прокси
    env = run["env"]
    assert env["ANTHROPIC_API_KEY"] is None and env["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert env["JARVIS_CORE_TOKEN"] is None and env["PYTHONPATH"] is None  # окружение самого Jarvis не нужно
    assert "127.0.0.1" in env["NO_PROXY"].split(",") and env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    # текст человека — только через stdin (stream-json), системный промпт — файлом
    message = json.loads(run["stdin"])
    text = message["message"]["content"][0]["text"]
    assert message["type"] == "user" and text.startswith("<jarvis>") and text.endswith("Сделай громкость 30")
    assert "Обращайся к пользователю «сэр»" in text and "Сейчас:" in text
    assert "внутри Jarvis" in run["system"] and "Правила:" in run["system"]
    # временные файлы хода удалены, токен хода отозван
    assert not list(Path(run["cwd"]).glob("mcp-*.json"))
    usage = core.db.query("SELECT provider, cost, prompt_tokens FROM usage")
    assert usage == [{"provider": "claude_code", "cost": 0.0, "prompt_tokens": 105}]


async def test_claude_session_is_resumed_then_rebuilt_with_history(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    fake.add([{"say": "Привет, сэр."}], [{"say": "Помню: вы Тони."}])
    first = await core.ai.ask("Привет, я Тони")
    second = await core.ai.ask("Как меня зовут?", chat_id=first.chat_id)
    assert second.text == "Помню: вы Тони."
    run1, run2 = fake.runs
    session = fake.arg(run1, "--session-id")
    assert session and fake.arg(run2, "--resume") == session and fake.arg(run2, "--session-id") is None
    text2 = json.loads(run2["stdin"])["message"]["content"][0]["text"]
    assert "Ранее в этом чате" not in text2
    # неизменные разделы контекста повторно не уходят: только время
    assert "Обращайся к пользователю" not in text2 and "Сейчас:" in text2

    # сессия пропала у программы — новая, с пересказом переписки
    fake.add([{"resume_fail": True}], [{"say": "Вы Тони."}])
    third = await core.ai.ask("Ещё раз: как меня зовут?", chat_id=first.chat_id)
    assert third.error == "" and third.text == "Вы Тони."
    run3, run4 = fake.runs[2:]
    assert fake.arg(run3, "--resume") == session and fake.arg(run4, "--session-id") not in (None, session)
    text4 = json.loads(run4["stdin"])["message"]["content"][0]["text"]
    assert "Ранее в этом чате" in text4 and "Пользователь: Привет, я Тони" in text4

    # удалили чат — сессия забыта
    core.ai.delete_chat(first.chat_id)  # type: ignore[attr-defined]
    assert core.db.kv_get(f"cli:claude_code:session:{first.chat_id}") is None


async def test_chat_mode_refuses_pc_tools_but_memory_works(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    events = collect(core)
    fake.add(
        [
            {"tool": "set_volume", "args": {"level": 10}},
            {"tool": "remember", "args": {"fact": "Тони любит рок"}},
            {"say": "Запомнил."},
        ]
    )
    result = await core.ai.ask("Тише и запомни про рок", mode="chat")
    assert result.error == ""
    refused, remembered = fake.runs[0]["tools"]
    assert refused["error"] is True and refused["text"].startswith("Ошибка: сейчас режим «Чат»")
    assert not any(c[0] == "volume_set" for c in core.platform.calls)  # type: ignore[attr-defined]
    assert remembered["error"] is False
    assert events.of("memory.added")[0]["item"]["text"] == "Тони любит рок"
    message = core.ai.chats.ui_messages(result.chat_id)[-1]  # type: ignore[attr-defined]
    assert message["meta"]["memory"] == ["Тони любит рок"]


async def test_claude_errors_become_russian_messages(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    backend: Any = fake.backend
    await until(lambda: backend.logged_in() is True)  # фоновая проверка «claude auth status» при выборе
    events = collect(core)
    fake.add([{"error": {"code": "authentication_failed", "text": "Invalid API key · Please run /login"}}])
    result = await core.ai.ask("Привет")
    assert result.error == errors.NOT_LOGGED_IN["claude_code"]
    assert backend.logged_in() is False and core.ai.configured is False  # type: ignore[attr-defined]
    # панель настроек узнаёт о смене входа событием
    assert {"provider": "claude_code", "state": "auth", "logged_in": False} in events.of("ai.cli")

    resets = int(time.time()) + 3600
    fake.add(
        [{"rate_limit": {"resetsAt": resets}}, {"error": {"code": "rate_limit", "text": "limit reached"}}]
    )
    result = await core.ai.ask("Привет")
    assert result.error.startswith("Лимит Claude исчерпан — обновится в ")

    fake.add([{"retry": "overloaded"}, {"result_error": "API Error: 529 Overloaded"}])
    assert (await core.ai.ask("Привет")).error == errors.SERVER.format(name="Claude")

    fake.add([{"error": {"code": "model_not_found", "text": "model: claude-x not found"}}])
    core.settings.patch({"ai": {"model": "claude-x"}})
    assert (await core.ai.ask("Привет")).error == errors.MODEL.format(model="claude-x", name="Claude")

    fake.add([{"exit": 1, "stderr": "Error: getaddrinfo ENOTFOUND api.anthropic.com"}])
    assert (await core.ai.ask("Привет")).error == errors.NETWORK.format(name="Claude")

    # не установлен — понятный текст, без запуска
    backend.locator = lambda: None
    backend.launch(refresh=True)
    assert (
        await core.ai.ask("Привет")
    ).error == "Claude Code не установлен — установите его в настройках Jarvis AI"


async def test_stop_kills_the_program(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    events = collect(core)
    fake.add([{"say": "Думаю…"}, {"sleep": 30}, {"say": "не дойдёт"}])
    chat = core.ai.create_chat()  # type: ignore[attr-defined]
    posted = await core.ai.post_message(chat["id"], "Долгий вопрос")  # type: ignore[attr-defined]
    await until(lambda: len(fake.runs) == 1)
    started = time.monotonic()
    assert core.ai.stop_chat(chat["id"]) is True  # type: ignore[attr-defined]
    await until(lambda: any(d.get("stopped") for d in events.of("chat.done")))
    assert time.monotonic() - started < 10
    done = events.of("chat.done")[-1]
    assert done["message_id"] == posted["reply_id"] and done["text"] == "Думаю…"


# --- одиночные запросы и агент ------------------------------------------------------------------------------


async def test_complete_runs_without_tools_and_extracts_json(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    fake.add([{"say": 'Вот: ```json\n{"steps": ["Открыть почту"]}\n```'}], [{"say": "просто текст"}])
    schema = {"type": "object", "properties": {"steps": {"type": "array", "items": {"type": "string"}}}}
    messages = [{"role": "system", "content": "Ты планировщик."}, {"role": "user", "content": "Задача"}]
    assert await core.ai.complete(messages, json_schema=schema) == {"steps": ["Открыть почту"]}
    run = fake.runs[0]
    assert "--mcp-config" not in run["argv"] and "--no-session-persistence" in run["argv"]
    assert fake.arg(run, "--session-id") is None and run["cwd"].endswith("claude-once")
    assert run["system"].startswith("Ты планировщик.") and "инструментов в этом запросе нет" in run["system"]
    assert "Ответь только одним JSON-объектом" in json.loads(run["stdin"])["message"]["content"][0]["text"]
    with pytest.raises(LlmError, match="не в нужном формате"):
        await core.ai.complete(messages, json_schema=schema)
    # проверка связи — тот же крошечный запуск
    fake.add([{"say": "готов"}])
    assert (await core.ai.test_connection())["ok"] is True  # type: ignore[attr-defined]


async def test_agent_works_as_one_program_run(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    ui = FakeUi()
    agent: Any = core.agent
    agent.ui, agent.settle_s, agent.launch_settle_s = ui, 0.0, 0.0
    events = collect(core)
    fake.add(
        [{"say": '{"steps": ["Нажать «Написать»"]}'}],
        [
            {"tool": "ui_click", "args": {"id": "e1", "step": 1}},
            {"tool": "finish", "args": {"summary": "Открыл новое письмо."}},
            {"say": "Готово."},
        ],
    )
    run_id = await core.agent.run("Открой новое письмо", "text")
    result = await asyncio.wait_for(core.agent.wait(run_id), 20)
    assert (result.status, result.summary, result.error) == ("done", "Открыл новое письмо.", "")
    assert ui.clicks == ["Написать"]
    assert [s["status"] for s in events.of("agent.step")] == ["running", "ok"]
    work = next(r for r in fake.runs if "одним JSON" not in r["stdin"])  # план идёт параллельно
    assert work["tools"][0]["name"] == "ui_click" and "«Написать»: нажато" in work["tools"][0]["text"]
    assert "--no-session-persistence" in work["argv"] and "агент" in work["system"]
    assert "Задача: Открой новое письмо" in json.loads(work["stdin"])["message"]["content"][0]["text"]


async def test_agent_step_limit_kills_the_program(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    core.settings.patch({"ai": {"max_agent_steps": 3}})
    agent: Any = core.agent
    agent.ui, agent.settle_s, agent.launch_settle_s = FakeUi(), 0.0, 0.0
    fake.add(
        [{"say": '{"steps": []}'}],
        [{"tool": "ui_snapshot", "args": {}}] * 4 + [{"sleep": 30}],
    )
    events = collect(core)
    started = time.monotonic()
    run_id = await core.agent.run("Посмотри", "text")
    result = await asyncio.wait_for(core.agent.wait(run_id), 20)
    assert result.status == "error" and result.error == "Агент не уложился в 3 шагов"
    # четвёртое действие не выполнено, программа остановлена сразу, а не через 30 с своего сценария
    work = next(r for r in fake.runs if "одним JSON" not in r["stdin"])
    assert [t["name"] for t in work["tools"]] == ["ui_snapshot"] * 3
    assert [s["status"] for s in events.of("agent.step")].count("ok") == 3
    assert time.monotonic() - started < 15


# --- Gemini CLI и Qwen Code ---------------------------------------------------------------------------------


async def test_gemini_cli_turn_tools_session_and_settings(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "gemini_cli", tmp_path, monkeypatch)
    fake.add(
        [{"session": "gem-1"}, {"tool": "set_volume", "args": {"level": 55}}, {"say": "Сделано."}],
        [{"say": "Ещё раз привет."}],
    )
    first = await core.ai.ask("Громкость 55")
    assert first.error == "" and first.text == "Сделано."
    assert ("volume_set", (55,)) in core.platform.calls  # type: ignore[attr-defined]
    second = await core.ai.ask("Привет", chat_id=first.chat_id)
    assert second.text == "Ещё раз привет."
    run1, run2 = fake.runs
    assert fake.arg(run1, "--resume") is None and fake.arg(run2, "--resume") == "gem-1"
    assert fake.arg(run1, "--approval-mode") == "default" and fake.arg(run1, "--extensions") == "none"
    assert fake.arg(run1, "--allowed-mcp-server-names") == "jarvis" and fake.arg(run1, "--model") == "auto"
    settings = run1["gemini_settings"]
    server = settings["mcpServers"]["jarvis"]
    # в файле нет секретов: адрес и токены — переменные окружения запуска
    assert server["httpUrl"] == "${JARVIS_MCP_URL}" and server["trust"] is True
    assert server["headers"]["X-Jarvis-Turn"] == "${JARVIS_MCP_TURN}"
    assert (
        settings["tools"]["core"] == ["mcp_jarvis_*"] and "run_shell_command" in settings["tools"]["exclude"]
    )
    assert settings["mcp"]["allowed"] == ["jarvis"] and settings["hooksConfig"]["enabled"] is False
    assert run1["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true" and run1["env"]["GEMINI_SYSTEM_MD"]
    assert run1["stdin"].startswith("<jarvis>") and run1["stdin"].endswith("Громкость 55")
    assert run1["env"]["JARVIS_MCP_TURN"] != run2["env"]["JARVIS_MCP_TURN"]  # токен — на один ход


async def test_gemini_cli_errors(core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_for(core, "gemini_cli", tmp_path, monkeypatch)
    fake.add([{"exit": 41, "stderr": "Please set an Auth method in your settings.json"}])
    assert (await core.ai.ask("Привет")).error == errors.NOT_LOGGED_IN["gemini_cli"]
    fake.add(
        [
            {
                "error": {
                    "text": "This client is no longer supported for Gemini Code Assist for individuals.",
                    "exit": 1,
                }
            }
        ]
    )
    assert (await core.ai.ask("Привет")).error == errors.GEMINI_CONSUMER
    fake.add([{"result_error": "[API Error: Quota exceeded for quota metric 'Gemini requests per day']"}])
    assert (await core.ai.ask("Привет")).error.startswith("Лимит Gemini исчерпан")
    fake.add([{"result_error": "User location is not supported for the API use."}])
    assert (await core.ai.ask("Привет")).error == errors.REGION.format(name="Gemini")


async def test_claude_code_with_socks_proxy_explains_why_there_is_no_connection(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core.settings.patch({"ai": {"proxy": "socks5://127.0.0.1:1080"}})
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    fake.add([{"result_error": "API Error: 403 Request not allowed"}])
    result = await core.ai.ask("Привет")
    # Claude Code не понимает SOCKS: адрес ему не передан, а вместо «включите VPN» — что поменять
    assert result.error == errors.NO_SOCKS.format(program="Claude Code", proxy="socks5://127.0.0.1:1080")
    assert fake.runs[0]["env"]["HTTPS_PROXY"] != "socks5://127.0.0.1:1080"


async def test_qwen_code_flags_and_complete(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "qwen_code", tmp_path, monkeypatch)
    fake.add([{"tool": "get_rates", "args": {}}, {"say": "Курс посмотрел."}], [{"say": "готов"}])
    result = await core.ai.ask("Курс доллара", images=["data:image/png;base64,QUJD"])
    assert result.error == "" and result.text == "Курс посмотрел."
    run = fake.runs[0]
    assert fake.arg(run, "--core-tools") == "structured_output" and "run_shell_command" in fake.arg(
        run, "--exclude-tools"
    )
    assert fake.arg(run, "--allowed-mcp-server-names") == "jarvis"
    assert fake.arg(run, "--allowed-tools") == "mcp__jarvis__*" and fake.arg(run, "--model") is None
    assert run["mcp_config"]["mcpServers"]["jarvis"]["httpUrl"].endswith("/api/ai/mcp")
    assert run["tools"][0]["name"] == "get_rates"  # вызов дошёл до инструмента Jarvis
    assert "эта программа их не получает" in run["stdin"] and run["env"]["QWEN_SYSTEM_MD"]
    assert await core.ai.complete([{"role": "user", "content": "Проверка"}]) == "готов"
    once = fake.runs[1]
    assert (
        "--no-chat-recording" in once["argv"]
        and fake.arg(once, "--allowed-mcp-server-names") == "jarvis-none"
    )


# --- состояние, вход, установка -----------------------------------------------------------------------------


async def test_status_login_terminal_and_install_rules(
    core: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_for(core, "claude_code", tmp_path, monkeypatch)
    backend: Any = fake.backend
    status = await backend.status()
    assert status["installed"] is True and status["can_install"] is False and status["npm"] == ""
    assert status["login_hint"] == errors.NOT_LOGGED_IN["claude_code"] and status["agent"] is True
    assert status["tools"] is True and "Pro или Max" in status["note"]
    opened = await backend.open_login()
    assert opened["opened"] is True and opened["command"] == "claude"
    assert fake.terminals and fake.terminals[0].name.startswith("login-claude")
    # на Windows — окно cmd: UTF-8, без токена ядра, с прокси Jarvis, подсказка без спецсимволов cmd
    core.settings.patch({"ai": {"proxy": "http://127.0.0.1:3128"}})
    monkeypatch.setattr(sys, "platform", "win32")
    await backend.open_login()
    monkeypatch.undo()
    script = fake.terminals[-1]
    lines = script.read_bytes().decode("utf-8").split("\r\n")  # строки cmd — CRLF
    assert script.suffix == ".cmd" and lines[:3] == ["@echo off", "chcp 65001 >nul", 'cd /d "%~dp0"']
    assert 'set "JARVIS_CORE_TOKEN="' in lines and 'set "HTTPS_PROXY=http://127.0.0.1:3128"' in lines
    *_, hint, blank, run_line, end = lines
    assert hint.startswith("echo Войдите в аккаунт Claude") and "(" in hint and blank == "echo." and end == ""
    assert run_line == f'"{fake.backend.launch().path}"'  # сама программа, её вход — внутри неё
    # Claude Code ставит только сам человек (правила Anthropic) — кнопки «Установить» нет
    with pytest.raises(UserFacingError, match="официальным установщиком"):
        await backend.install()
    gemini: Any = core.ai.cli.get("gemini_cli")  # type: ignore[attr-defined]
    gemini.locator = lambda: None
    gemini.launch(refresh=True)
    state = await gemini.status()
    assert (
        state["installed"] is False and state["npm"] == "@google/gemini-cli" and state["node_required"] == 20
    )
    assert "18 июня 2026" in state["note"]
    with pytest.raises(UserFacingError, match="не установлен"):
        await gemini.open_login()
