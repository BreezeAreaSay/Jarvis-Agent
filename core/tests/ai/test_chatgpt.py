"""«ChatGPT (подписка)» через Codex: компонент, вход, ход чата с инструментами Jarvis, треды, ошибки, стоп,
одиночные запросы, агент и API. Codex — фальшивый (tests/ai/chatgpt_fakes.py): без процесса, сети и входа.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.chatgpt import backend, component, errors
from jarvis.ai.chatgpt.component import SdkInfo
from jarvis.ai.chatgpt.runtime import RuntimeGone
from jarvis.ai.llm import LlmError, LlmFormatError
from jarvis.ai.vision import Shot
from jarvis.context import AppContext
from jarvis.services import ExecResult, UserFacingError
from tests.ai.chatgpt_fakes import ACCOUNT, Codexes, FakeTurnScript, answer, installed_sdk, text_of
from tests.ai.fakes import FakeUi
from tests.conftest import EventLog


async def until(check: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("не дождался")
        await asyncio.sleep(0.01)


@pytest.fixture
def codex(ctx: AppContext) -> Codexes:
    """Фальшивый Codex с выполненным входом (auth.json в <data>/codex-home, как у прежнего Jarvis)."""
    world = Codexes()
    gpt: Any = ctx.ai.chatgpt  # type: ignore[attr-defined]
    gpt.runtime_factory = world.factory
    gpt.find_sdk = installed_sdk
    component.auth_file().write_text("{}", encoding="utf-8")
    ctx.settings.patch({"ai": {"provider": "chatgpt"}})
    return world


def gpt_of(ctx: AppContext) -> Any:
    return ctx.ai.chatgpt  # type: ignore[attr-defined]


# --- компонент и вход --------------------------------------------------------------------------------------


async def test_install_then_existing_login_is_picked_up(ctx: AppContext, events: EventLog) -> None:
    gpt = gpt_of(ctx)
    world = Codexes()
    gpt.runtime_factory = world.factory
    state = {"installed": False}
    gpt.find_sdk = lambda: installed_sdk() if state["installed"] else SdkInfo(False)

    async def install() -> None:
        await asyncio.sleep(0.05)
        state["installed"] = True

    gpt.install_sdk = install
    ctx.settings.patch({"ai": {"provider": "chatgpt"}})
    await ctx.ai.start()
    try:
        assert ctx.ai.configured is False
        assert gpt.problem() == errors.NOT_INSTALLED
        assert await gpt.status() == {
            "installed": False,
            "installing": False,
            "logged_in": False,
            "account": "",
            "error": "",
            "version": "",
        }
        assert await gpt.install() == {"ok": True, "installing": True}
        assert (await gpt.status())["installing"] is True
        assert gpt.problem() == "Компонент ChatGPT ещё скачивается — подождите немного"
        await until(lambda: [e["state"] for e in events.of("ai.chatgpt")] == ["installing", "installed"])
        # прежний вход в <data>/codex-home подхватился сам: Codex прогрелся и прочитал аккаунт
        await until(lambda: ctx.ai.configured)
        assert events.of("ai.configured")[-1] == {"configured": True}
        status = await gpt.status()
        assert status["installed"] and status["logged_in"] and status["version"] == "0.163.0"
        assert status["account"] == "tony@stark.com (ChatGPT Plus)" and status["error"] == ""
        assert await gpt.install() == {"ok": True, "installed": True}
        config = world.runtime.config
        assert config.env["CODEX_HOME"].endswith("codex-home") and config.cwd.endswith("chatgpt")
        assert 'web_search="disabled"' in config.overrides and "features.shell_tool=false" in config.overrides
    finally:
        await ctx.ai.stop()
    assert world.runtime.closed  # Codex закрыт вместе с ядром


async def test_install_error_and_auth_file_without_codex(ctx: AppContext, events: EventLog) -> None:
    gpt = gpt_of(ctx)
    gpt.find_sdk = lambda: SdkInfo(False)

    async def broken() -> None:
        raise UserFacingError("Не удалось скачать модуль — проверьте интернет и попробуйте ещё раз")

    gpt.install_sdk = broken
    ctx.settings.patch({"ai": {"provider": "chatgpt"}})
    await gpt.install()
    await until(lambda: events.of("ai.chatgpt")[-1]["state"] == "error")
    assert events.of("ai.chatgpt")[-1]["error"].startswith("Не удалось скачать модуль")
    assert (await gpt.status())["error"].startswith("Не удалось скачать модуль")

    # вход прежнего Jarvis (auth.json в <data>/codex-home) виден ещё до запуска Codex
    gpt.find_sdk = installed_sdk
    gpt.sdk(refresh=True)
    assert gpt.problem() == errors.NOT_LOGGED_IN
    component.auth_file().write_text("{}", encoding="utf-8")
    assert ctx.ai.configured is True
    ctx.settings.patch({"ai": {"enabled": False}})
    assert ctx.ai.configured is False and gpt.problem() == "Jarvis AI выключен в настройках"


async def test_login_in_browser_then_logout(ctx: AppContext, codex: Codexes, events: EventLog) -> None:
    codex.account = None
    gpt = gpt_of(ctx)
    status = await gpt.status()
    assert status["installed"] and not status["logged_in"] and not ctx.ai.configured

    result = await gpt.login()
    assert result["ok"] and result["opened"] and not result["logged_in"]
    assert result["auth_url"].startswith("https://auth.openai.com/oauth/authorize")
    assert ("open_url", (result["auth_url"],)) in ctx.platform.calls  # type: ignore[attr-defined]
    assert events.of("ai.chatgpt")[-1] == {"state": "login", "opened": True, "auth_url": result["auth_url"]}
    status = await gpt.status()
    assert status["login_pending"] is True and status["auth_url"] == result["auth_url"]

    codex.login_result.put({"success": True, "loginId": "login-1"})
    await until(lambda: events.of("ai.chatgpt")[-1]["state"] == "logged_in")
    assert events.of("ai.chatgpt")[-1]["account"] == "tony@stark.com (ChatGPT Plus)"
    assert ctx.ai.configured is True
    status = await gpt.status()
    assert status["logged_in"] and "login_pending" not in status
    # уже вошли — повторный вход ничего не открывает
    assert (await gpt.login())["logged_in"] is True

    assert await gpt.logout() == {"ok": True}
    assert ("logout", None) in codex.calls and ctx.ai.configured is False
    assert events.of("ai.chatgpt")[-1] == {"state": "logged_out"}


async def test_device_login_failed_login_and_cancel(
    ctx: AppContext, codex: Codexes, events: EventLog
) -> None:
    codex.account = None
    gpt = gpt_of(ctx)
    result = await gpt.login(device=True)
    assert result["user_code"] == "ABCD-1234" and result["verification_url"].endswith("/codex/device")
    assert events.of("ai.chatgpt")[-1]["state"] == "device"
    assert (await gpt.status())["user_code"] == "ABCD-1234"
    codex.login_result.put({"success": False, "error": "access_denied"})
    await until(lambda: events.of("ai.chatgpt")[-1]["state"] == "error")
    assert events.of("ai.chatgpt")[-1]["error"] == "Вход не выполнен: access_denied"
    assert not ctx.ai.configured

    await gpt.login()
    assert await gpt.cancel_login() == {"ok": True}
    assert ("login_cancel", "login-1") in codex.calls
    assert "login_pending" not in await gpt.status()


async def test_codex_start_error_is_explained(ctx: AppContext, codex: Codexes) -> None:
    # неверный override (или битый codex) роняет процесс при старте: «Error: … in `ключ`» в stderr
    codex.start_error = RuntimeGone(
        "Codex process closed stdout. stderr_tail=Error: invalid type: string, expected u64\n"
        "in `thread_unload_delay_secs`"
    )
    status = await gpt_of(ctx).status()
    assert status["error"].startswith("Ошибка настроек Codex: invalid type")
    result = await ctx.ai.ask("Привет")
    assert result.error.startswith("Ошибка настроек Codex")
    codex.start_error = None
    codex.add(answer("Привет, сэр."))
    assert (await ctx.ai.ask("Привет")).text == "Привет, сэр."


async def test_start_error_hints_at_non_ascii_data_dir(
    ctx: AppContext, codex: Codexes, tmp_path: Any
) -> None:
    from jarvis import paths

    old = paths.data_dir()
    paths.set_data_dir(tmp_path / "Пользователь" / "Jarvis")
    try:
        codex.start_error = OSError("The system cannot find the path specified")
        status = await gpt_of(ctx).status()
    finally:
        paths.set_data_dir(old)
    assert status["error"].startswith("Компонент ChatGPT (Codex) не запустился: The system cannot find")
    assert status["error"].endswith("(например C:\\JarvisData) и войдите в ChatGPT заново")


# --- ход чата


async def test_chat_turn_streams_text_and_runs_jarvis_tools(
    ctx: AppContext, codex: Codexes, events: EventLog
) -> None:
    def script(t: FakeTurnScript) -> None:
        t.message("Сейчас.", phase="commentary")
        remembered = t.call("remember", {"fact": "Тони любит кофе"})
        volume = t.call("set_volume", {"level": 30})
        assert text_of(remembered) == "Запомнил: Тони любит кофе" and remembered["success"] is True
        assert text_of(volume) == "Громкость: 30%"
        t.message("Запомнил и поставил громкость 30%, сэр.")
        t.usage(150, 30)
        t.usage(50, 10)  # второй запрос к модели в том же ходе
        t.complete()

    codex.add(script)
    result = await ctx.ai.ask("Запомни, что я люблю кофе, и сделай громкость 30")
    assert codex.failures == []
    assert result.error == ""
    assert result.text == "Сейчас.\n\nЗапомнил и поставил громкость 30%, сэр."
    assert result.spoken and "Сейчас" not in result.spoken  # голосом — только итоговый ответ
    assert result.actions == [{"title": "Громкость 30%", "ok": True, "detail": "Выполнено"}]
    assert ctx.platform.volume == 30  # type: ignore[attr-defined]
    assert [m["text"] for m in ctx.notes.memory_list()] == ["Тони любит кофе"]

    assert [(t["name"], t["status"]) for t in events.of("chat.tool")] == [
        ("remember", "running"),
        ("remember", "ok"),
        ("set_volume", "running"),
        ("set_volume", "ok"),
    ]
    assert "".join(e["delta"] for e in events.of("chat.delta")) == result.text
    done = events.of("chat.done")[-1]
    assert done["usage"] == {"prompt_tokens": 200, "completion_tokens": 40, "cost": 0.0}
    assert done["actions"] == result.actions
    final = events.of("chat.message")[-1]["message"]
    assert final["meta"]["memory"] == ["Тони любит кофе"] and final["content"] == result.text
    statuses = [(s["status"], s["text"]) for s in events.of("chat.status")]
    assert statuses[0] == ("thinking", "Думаю…") and statuses[-1] == ("done", "")

    # тред: инструменты Jarvis, правила, без shell; ход: модель Codex по умолчанию и контекст <jarvis>
    turn = codex.turns[0]
    thread = codex.threads[turn["thread"]]
    assert thread["ephemeral"] is False and thread["approvalPolicy"] == "never"
    assert thread["sandbox"] == "read-only" and thread["model"] == "gpt-6.1-sol"
    names = {tool["name"] for tool in thread["dynamicTools"]}
    assert {"remember", "set_volume", "run_command", "web_search", "run_agent"} <= names
    assert all(set(tool) == {"type", "name", "description", "inputSchema"} for tool in thread["dynamicTools"])
    assert "Ты работаешь внутри Jarvis" in thread["developerInstructions"]
    assert thread["config"] == {"web_search": "disabled"}
    # «Скорость ответа: Быстро» (по умолчанию) — модель думает меньше
    assert turn["params"]["model"] == "gpt-6.1-sol" and turn["params"]["effort"] == "low"
    context = turn["params"]["additionalContext"]
    assert set(context) == {"jarvis"} and context["jarvis"]["kind"] == "application"
    assert "Обращайся к пользователю «сэр»" in context["jarvis"]["value"]
    assert "Сейчас режим «Всё вместе»" in context["jarvis"]["value"]
    assert context["jarvis"]["value"].rstrip().endswith(")."), "время — последним"
    assert turn["items"] == [{"type": "text", "text": "Запомни, что я люблю кофе, и сделай громкость 30"}]

    # в базе — как у провайдеров по ключу: вызовы инструментов и их результаты (скрытые), итог
    rows = ctx.ai.chats.rows(result.chat_id)  # type: ignore[attr-defined]
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert rows[1]["tool_calls"][0]["function"]["name"] == "remember" and rows[2]["meta"]["hidden"]
    usage = ctx.db.query("SELECT provider, model, prompt_tokens, completion_tokens, cost FROM usage")
    assert [tuple(r.values()) for r in usage] == [("chatgpt", "gpt-6.1-sol", 200, 40, 0.0)]


async def test_chat_mode_refuses_pc_actions(ctx: AppContext, codex: Codexes) -> None:
    def script(t: FakeTurnScript) -> None:
        refused = t.call("set_volume", {"level": 5})
        assert text_of(refused) == backend.CHAT_MODE_DENIED and refused["success"] is False
        assert text_of(t.call("list_memory", {})) == "Память пуста."
        t.message("В режиме «Чат» я не управляю компьютером, сэр.")
        t.complete()

    codex.add(script)
    result = await ctx.ai.ask("Сделай громкость 5", mode="chat")
    assert codex.failures == [] and result.error == ""
    assert ctx.platform.volume == 40  # type: ignore[attr-defined]
    context = codex.turns[0]["params"]["additionalContext"]["jarvis"]["value"]
    assert "Сейчас режим «Чат»" in context


async def test_images_go_to_codex_and_voice_is_fast(ctx: AppContext, codex: Codexes) -> None:
    image = "data:image/png;base64,iVBORw0KGgo="
    codex.add(answer("На картинке кот."), answer("Двадцать градусов, сэр."))
    await ctx.ai.ask("", images=[image])
    assert codex.turns[0]["items"] == [
        {"type": "text", "text": "Что на изображении?"},
        {"type": "image", "url": image},
    ]
    result = await ctx.ai.ask("Какая погода?", source="voice")
    assert result.text == "Двадцать градусов, сэр."
    params = codex.turns[1]["params"]
    assert params["effort"] == "low"
    assert "Ответ будет произнесён вслух" in params["additionalContext"]["jarvis"]["value"]


async def test_thread_is_reused_resumed_and_rebuilt_with_history(ctx: AppContext, codex: Codexes) -> None:
    gpt = gpt_of(ctx)
    codex.add(answer("Привет, сэр."), answer("Конечно."))
    first = await ctx.ai.ask("Привет")
    await ctx.ai.ask("Спасибо", chat_id=first.chat_id)
    thread = codex.turns[0]["thread"]
    assert codex.turns[1]["thread"] == thread and len(codex.threads) == 1
    second = codex.turns[1]["params"]["additionalContext"]
    assert second["jarvis"]["value"].startswith("Сейчас:"), "неизменный контекст второй раз не повторяется"
    saved = ctx.db.kv_get("chatgpt:thread:" + first.chat_id)
    assert saved["id"] == thread and saved["turns"] == 2

    # обращение изменилось — раздел характера уходит снова, с пометкой
    ctx.settings.patch({"profile": {"address": "босс"}})
    codex.add(answer("Да, босс."))
    await ctx.ai.ask("Ты тут?", chat_id=first.chat_id)
    third = codex.turns[2]["params"]["additionalContext"]["jarvis"]["value"]
    assert "Обращайся к пользователю «босс»." in third and backend.prompt.CHANGED in third

    # Codex перезапустился (процесс умер) — тот же тред продолжается через thread/resume
    codex.runtime.closed = True
    codex.add(answer("Помню."))
    await ctx.ai.ask("Что я говорил?", chat_id=first.chat_id)
    assert len(codex.instances) == 2 and ("thread_resume", thread) in codex.calls
    assert codex.turns[3]["thread"] == thread
    assert (
        "Обращайся к пользователю «босс»" in codex.turns[3]["params"]["additionalContext"]["jarvis"]["value"]
    )

    # тред потерян — новый тред, прошлая переписка уходит пересказом, старый тред удаляется
    codex.lost.add(thread)
    codex.runtime.closed = True
    codex.add(answer("Продолжаю."))
    await ctx.ai.ask("И ещё", chat_id=first.chat_id)
    fresh = codex.turns[4]["thread"]
    assert fresh != thread
    history = codex.turns[4]["params"]["additionalContext"]["history"]
    assert history["kind"] == "untrusted"
    assert "Пользователь: Привет\nДжарвис: Привет, сэр." in history["value"]
    assert "И ещё" not in history["value"]
    assert ctx.db.kv_get("chatgpt:thread:" + first.chat_id)["id"] == fresh
    await until(lambda: ("thread_delete", thread) in codex.calls)

    # набор инструментов изменился (поиск выключили) — тоже новый тред
    ctx.settings.patch({"ai": {"web_search": False}})
    codex.add(answer("Хорошо."))
    await ctx.ai.ask("Ладно", chat_id=first.chat_id)
    assert codex.turns[5]["thread"] not in (thread, fresh)
    assert "web_search" not in {t["name"] for t in codex.threads[codex.turns[5]["thread"]]["dynamicTools"]}

    # чат удалён — тред Codex тоже
    last = codex.turns[5]["thread"]
    ctx.ai.delete_chat(first.chat_id)  # type: ignore[attr-defined]
    assert ctx.db.kv_get("chatgpt:thread:" + first.chat_id) is None
    await until(lambda: ("thread_delete", last) in codex.calls)
    assert gpt._threads == {}


async def test_voice_chat_starts_fresh_thread_after_three_hours(ctx: AppContext, codex: Codexes) -> None:
    codex.add(answer("Слушаю."), answer("Снова слушаю."))
    first = await ctx.ai.ask("Джарвис, привет", source="voice")
    # прошло четыре часа
    gpt = gpt_of(ctx)
    gpt._threads[first.chat_id].last_used -= 4 * 3600
    key = "chatgpt:thread:" + first.chat_id
    saved = ctx.db.kv_get(key)
    ctx.db.kv_set(key, {**saved, "updated": saved["updated"] - 4 * 3600})
    ctx.db.execute("UPDATE messages SET created = created - 14400 WHERE chat_id=?", (first.chat_id,))
    second = await ctx.ai.ask("Ещё раз", chat_id=first.chat_id, source="voice")
    assert second.text == "Снова слушаю."
    assert codex.turns[0]["thread"] != codex.turns[1]["thread"]
    assert "history" not in codex.turns[1]["params"]["additionalContext"]  # окно голосового чата — 3 часа


# --- ошибки и стоп


def failing(message: str, info: Any = None) -> Callable[[FakeTurnScript], None]:
    def script(t: FakeTurnScript) -> None:
        t.error(message, info)
        t.complete("failed", {"message": message, "codexErrorInfo": info})

    return script


async def test_turn_errors_become_russian_messages(ctx: AppContext, codex: Codexes, events: EventLog) -> None:
    codex.add(
        failing(
            "You’ve hit your usage limit. Try again at Oct 19th, 2026 8:53 AM.",
            "usageLimitExceeded",
        )
    )
    result = await ctx.ai.ask("Привет")
    assert result.error.startswith("Лимит подписки ChatGPT исчерпан: You’ve hit your usage limit")
    assert events.of("chat.error")[-1]["error"] == result.error

    codex.add(failing("unexpected status 403 Forbidden: unsupported_country_region_territory"))
    assert (await ctx.ai.ask("Привет")).error == errors.REGION

    # 401: вход протух — Jarvis перечитывает аккаунт, «настроено» гаснет
    codex.account = None
    codex.add(
        failing("unexpected status 401 Unauthorized", {"httpConnectionFailed": {"httpStatusCode": 401}})
    )
    assert (await ctx.ai.ask("Привет")).error == errors.NOT_LOGGED_IN
    await until(lambda: not ctx.ai.configured)
    assert (await ctx.ai.ask("Привет")).error == errors.NOT_LOGGED_IN  # без входа ход даже не начинается
    assert len(codex.turns) == 3


async def test_codex_death_mid_turn_and_restart(ctx: AppContext, codex: Codexes) -> None:
    def dies(t: FakeTurnScript) -> None:
        t.message("Начинаю", phase="commentary")
        t.turn.events.put(RuntimeGone("Codex process closed stdout"))

    codex.add(dies, answer("Я снова здесь."))
    result = await ctx.ai.ask("Привет")
    assert result.error == errors.DEAD and result.text == "Начинаю"
    codex.runtime.closed = True
    again = await ctx.ai.ask("Ты тут?", chat_id=result.chat_id)
    assert again.text == "Я снова здесь." and len(codex.instances) == 2


async def test_endless_reconnects_are_cut_off(
    ctx: AppContext, codex: Codexes, events: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend, "RETRY_NETWORK_S", 0.2)
    monkeypatch.setattr(backend, "RETRY_REGION_S", 0.1)

    def reconnecting(message: str) -> Callable[[FakeTurnScript], None]:
        def script(t: FakeTurnScript) -> None:
            t.error(message, {"responseStreamDisconnected": {"httpStatusCode": None}}, will_retry=True)
            assert t.wait_interrupt(), "Jarvis должен прервать ход"

        return script

    codex.add(reconnecting("Reconnecting... 2/5 (stream disconnected before completion)"))
    result = await ctx.ai.ask("Привет")
    assert result.error.startswith("Нет связи с ChatGPT (Reconnecting... 2/5")
    assert ("thinking", "Нет ответа от ChatGPT, повторяю…") in [
        (s["status"], s["text"]) for s in events.of("chat.status")
    ]
    codex.add(reconnecting("unexpected status 403 Forbidden"))
    assert (await ctx.ai.ask("Привет", chat_id=result.chat_id)).error == errors.REGION
    assert codex.failures == []


async def test_stop_interrupts_turn_and_cancels_running_tool(
    ctx: AppContext, codex: Codexes, events: EventLog
) -> None:
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def run_text(text: str, source: str) -> ExecResult | None:
        loop.call_soon(started.set)
        await asyncio.sleep(60)
        return None

    ctx.commands.run_text = run_text  # type: ignore[method-assign]

    def script(t: FakeTurnScript) -> None:
        t.message("Выполняю.", phase="commentary")
        result = t.call("run_command", {"text": "выключи компьютер через час"})
        assert text_of(result) == "Остановлено пользователем" and result["success"] is False
        assert t.wait_interrupt()

    codex.add(script, answer("Слушаю, сэр."))
    task = asyncio.ensure_future(ctx.ai.ask("Выключи компьютер через час"))
    await asyncio.wait_for(started.wait(), 5)
    chat_id = events.of("chat.message")[0]["chat_id"]
    assert ctx.ai.stop_chat(chat_id) is True
    result = await asyncio.wait_for(task, 5)
    assert result.error == "Остановлено" and result.text == "Выполняю."
    assert codex.turns[0]["turn"].interrupted.is_set()
    assert events.of("chat.done")[-1]["stopped"] is True
    await until(lambda: len(codex.runtime.tool_results) == 1)
    assert codex.failures == []
    # следующий вопрос — в тот же тред, когда прерванный ход закрылся
    again = await ctx.ai.ask("Ты тут?", chat_id=chat_id)
    assert again.text == "Слушаю, сэр." and codex.turns[1]["thread"] == codex.turns[0]["thread"]


async def test_subagent_attempt_aborts_turn_and_stops_the_subagent(ctx: AppContext, codex: Codexes) -> None:
    def script(t: FakeTurnScript) -> None:
        codex.foreign["thr-child"] = "turn-child"  # Codex запустил под-агента в своём треде
        t.emit("item/started", {"item": {"type": "collabAgentToolCall", "id": "c1", "tool": "spawnAgent"}})
        assert t.wait_interrupt()

    codex.add(script)
    result = await ctx.ai.ask("Сделай всё сам")
    assert result.error == errors.SUBAGENT
    await until(lambda: ("interrupt", "thr-child") in codex.calls)
    assert codex.foreign == {}


# --- одиночные запросы, модели, проверка связи


async def test_complete_uses_ephemeral_thread_and_output_schema(ctx: AppContext, codex: Codexes) -> None:
    schema = {
        "type": "object",
        "properties": {"steps": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}},
        "required": ["steps"],
    }
    codex.add(answer('{"steps": ["Открыть почту", "Написать письмо"], "note": null}'))
    parsed = await ctx.ai.complete(
        [{"role": "system", "content": "Ты планировщик."}, {"role": "user", "content": "Напиши письмо"}],
        json_schema=schema,
    )
    assert parsed == {"steps": ["Открыть почту", "Написать письмо"], "note": None}
    turn = codex.turns[0]
    thread = codex.threads[turn["thread"]]
    assert thread["ephemeral"] is True and "dynamicTools" not in thread
    assert thread["developerInstructions"].startswith("Ты планировщик.")
    strict = turn["params"]["outputSchema"]
    assert strict["additionalProperties"] is False and strict["required"] == ["steps", "note"]
    assert strict["properties"]["note"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert turn["params"]["effort"] == "low"
    assert turn["items"] == [{"type": "text", "text": "Напиши письмо"}]
    await until(lambda: ("thread_unsubscribe", turn["thread"]) in codex.calls)

    codex.add(answer("Привет!"), answer("это не JSON"))
    dialog = [
        {"role": "user", "content": "Как дела?"},
        {"role": "assistant", "content": "Хорошо."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "А это?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
    ]
    assert await ctx.ai.complete(dialog) == "Привет!"
    items = codex.turns[1]["items"]
    assert items[0]["text"].startswith("Предыдущие сообщения:\nПользователь: Как дела?\nАссистент: Хорошо.")
    assert items[0]["text"].endswith("А это?") and items[1] == {
        "type": "image",
        "url": "data:image/png;base64,AAA",
    }
    with pytest.raises(LlmFormatError):
        await ctx.ai.complete([{"role": "user", "content": "JSON"}], json_schema=schema)

    codex.account = None
    await gpt_of(ctx)._refresh_account()
    with pytest.raises(LlmError, match="Войдите в ChatGPT"):
        await ctx.ai.complete([{"role": "user", "content": "Привет"}])


async def test_models_providers_and_connection_test(ctx: AppContext, codex: Codexes) -> None:
    gpt = gpt_of(ctx)
    preset = next(p for p in ctx.ai.providers() if p["id"] == "chatgpt")  # type: ignore[attr-defined]
    assert preset["login"] and preset["component"] and not preset["needs_key"]
    assert preset["default_model"] == "gpt-6.1-sol"
    assert gpt.resolve_model() == "gpt-6.1-sol"  # модель от прежнего провайдера (gpt-5.4-mini) не годится
    assert await ctx.ai.list_models() == ["gpt-6.1-sol", "gpt-6-luna"]  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"model": "gpt-6-luna"}})
    assert gpt.resolve_model() == "gpt-6-luna" and gpt.resolve_model("неизвестная") == "gpt-6.1-sol"
    preset = next(p for p in ctx.ai.providers() if p["id"] == "chatgpt")  # type: ignore[attr-defined]
    assert preset["models"] == ["gpt-6.1-sol", "gpt-6-luna"]

    codex.add(answer("Готов."))
    test = await ctx.ai.test_connection()  # type: ignore[attr-defined]
    assert test["ok"] is True and test["model"] == "gpt-6-luna"
    codex.add(failing("unexpected status 403 Forbidden"))
    test = await ctx.ai.test_connection()  # type: ignore[attr-defined]
    assert test == {
        "ok": False,
        "error": errors.REGION,
        "latency_ms": test["latency_ms"],
        "model": "gpt-6-luna",
    }


# --- агент


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    return fake


async def test_agent_works_as_a_codex_turn(
    ctx: AppContext, codex: Codexes, ui: FakeUi, events: EventLog
) -> None:
    ctx.settings.patch({"ai": {"vision": "off"}})

    def work(t: FakeTurnScript) -> None:
        clicked = t.call("ui_click", {"id": "e1", "step": 1})
        assert "«Написать»: нажато" in text_of(clicked) and "[Снимок окна]" in text_of(clicked)
        check = t.call("finish", {"summary": "Открыл новое письмо."})  # сначала — проверка по снимку
        assert text_of(check).startswith("Проверка перед итогом") and "[Снимок окна]" in text_of(check)
        t.call("finish", {"summary": "Открыл новое письмо."})
        t.message("Готово.")
        t.complete()

    codex.add(answer('{"steps": ["Нажать «Написать»"]}'), work)
    run_id = await ctx.agent.run("Открой новое письмо", "text")
    result = await asyncio.wait_for(ctx.agent.wait(run_id), 10)  # type: ignore[attr-defined]
    assert codex.failures == []
    assert (result.status, result.summary, result.error) == ("done", "Открыл новое письмо.", "")
    assert ui.clicks == ["Написать"]
    assert [s["status"] for s in events.of("agent.step")] == ["running", "ok"]
    states = events.of("agent.state")
    assert states[0]["status"] == "planning" and states[-1]["status"] == "done"

    # план составляется параллельно (отдельный ход со схемой): ход агента его не ждёт
    agent_turn = next(t for t in codex.turns if "outputSchema" not in t["params"])
    assert agent_turn["params"]["effort"] == "low"
    thread = codex.threads[agent_turn["thread"]]
    names = {tool["name"] for tool in thread["dynamicTools"]}
    assert {"ui_snapshot", "ui_click", "ui_type", "press_keys", "launch_app", "finish"} <= names
    assert "remember" not in names and "screenshot" not in names  # без зрения — без снимков экрана
    assert thread["ephemeral"] is True and "Ты — агент Jarvis" in thread["developerInstructions"]
    first = agent_turn["items"][0]["text"]
    assert "Задача: Открой новое письмо" in first and "e1 Button «Написать»" in first


async def test_agent_step_limit_and_screenshot(
    ctx: AppContext, codex: Codexes, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.patch({"ai": {"max_agent_steps": 3, "vision": True}})

    ctx.platform.screenshot = lambda window=None: b"png"  # type: ignore[method-assign]
    monkeypatch.setattr(
        "jarvis.ai.vision.render",
        lambda png, rect, **kw: Shot("data:image/jpeg;base64,QUJD", 100, 70, rect, kw.get("hwnd", 0)),
    )

    def work(t: FakeTurnScript) -> None:
        shot = t.call("screenshot", {})
        # картинка окна — в том же ответе инструмента, вместе с подписью и снимком элементов
        assert {"type": "inputImage", "imageUrl": "data:image/jpeg;base64,QUJD"} in shot["contentItems"]
        assert shot["contentItems"][0]["text"].startswith("Снимок окна приложен")
        t.call("ui_snapshot", {})
        t.call("ui_snapshot", {})
        refused = t.call("ui_snapshot", {})
        assert text_of(refused) == "Лимит шагов (3) исчерпан"
        assert t.wait_interrupt()

    codex.add(answer('{"steps": []}'), work)
    run_id = await ctx.agent.run("Посмотри на экран", "text")
    result = await asyncio.wait_for(ctx.agent.wait(run_id), 10)  # type: ignore[attr-defined]
    assert codex.failures == []
    assert result.status == "error" and result.error == "Агент не уложился в 3 шагов"
    thread = codex.threads[codex.turns[1]["thread"]]
    shot = next(t for t in thread["dynamicTools"] if t["name"] == "screenshot")
    assert backend.SCREENSHOT_HINT.strip() in shot["description"]


async def test_agent_needs_login(ctx: AppContext, codex: Codexes) -> None:
    codex.account = None
    assert (await gpt_of(ctx).status())["logged_in"] is False
    with pytest.raises(UserFacingError, match="Войдите в ChatGPT"):
        await ctx.agent.run("Открой почту", "text")


# --- API


def test_chatgpt_api_routes(client: TestClient) -> None:
    ctx: Any = client.ctx  # type: ignore[attr-defined]
    world = Codexes(account={})  # входа нет
    gpt = ctx.ai.chatgpt
    gpt.runtime_factory = world.factory
    state = {"installed": False}
    gpt.find_sdk = lambda: installed_sdk() if state["installed"] else SdkInfo(False)

    async def install() -> None:
        state["installed"] = True

    gpt.install_sdk = install
    assert client.patch("/api/settings", json={"ai": {"provider": "chatgpt"}}).status_code == 200

    status = client.get("/api/ai/chatgpt").json()
    assert status["installed"] is False and status["logged_in"] is False
    assert client.post("/api/ai/chatgpt/login").json()["detail"] == errors.NOT_INSTALLED
    assert client.post("/api/ai/chatgpt/install").json() == {"ok": True, "installing": True}
    for _ in range(500):
        status = client.get("/api/ai/chatgpt").json()
        if status["installed"]:
            break
        time.sleep(0.01)
    assert status["installed"] is True and status["logged_in"] is False

    login = client.post("/api/ai/chatgpt/login").json()
    assert login["ok"] is True and login["auth_url"].startswith("https://auth.openai.com/")
    device = client.post("/api/ai/chatgpt/login", json={"device": True}).json()
    assert device["user_code"] == "ABCD-1234"
    assert client.post("/api/ai/chatgpt/login/cancel").json() == {"ok": True}
    world.account = dict(ACCOUNT)
    status = client.get("/api/ai/chatgpt").json()
    assert status["logged_in"] is True and status["account"] == "tony@stark.com (ChatGPT Plus)"
    assert client.get("/api/status").json()["ai"]["configured"] is True

    providers = {p["id"]: p for p in client.get("/api/ai/providers").json()["items"]}
    assert providers["chatgpt"]["login"] is True and providers["chatgpt"]["component"] is True
    assert client.get("/api/ai/models").json()["items"] == ["gpt-6.1-sol", "gpt-6-luna"]
    world.add(answer("Готов."))
    assert client.post("/api/ai/test").json()["ok"] is True

    assert client.post("/api/ai/chatgpt/logout").json() == {"ok": True}
    assert client.get("/api/ai/chatgpt").json()["logged_in"] is False
    # без токена ядра — 401, как у всех /api/*
    bare = TestClient(client.app)
    assert bare.get("/api/ai/chatgpt").status_code == 401
    assert bare.post("/api/ai/chatgpt/login").status_code == 401
