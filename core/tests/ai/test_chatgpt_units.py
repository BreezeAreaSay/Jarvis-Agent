"""«ChatGPT (подписка)»: ошибки по-русски, строгая схема, разбор событий хода, контекст хода, ответы Codex,
поиск компонента. Тексты ошибок Codex — как их присылает codex 0.163.0 (сверено с подставным сервером модели).
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from jarvis import extras
from jarvis.ai.chatgpt import component, errors, prompt
from jarvis.ai.chatgpt.backend import strict_schema
from jarvis.ai.chatgpt.runtime import CodexEvent, RuntimeGone, server_reply, to_event, tool_failure
from jarvis.ai.chatgpt.turns import GONE, TurnTranslator, markdown_links
from jarvis.settings import AiSettings, Settings

# --- ошибки -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "info", "reason"),
    [
        (
            "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) or try again at "
            "Oct 19th, 2026 8:53 AM.",
            "usageLimitExceeded",
            "quota",
        ),
        (
            "unexpected status 403 Forbidden: unsupported_country_region_territory, url: https://x",
            None,
            "region",
        ),
        ("Forbidden", {"httpConnectionFailed": {"httpStatusCode": 403}}, "region"),
        (
            "unexpected status 401 Unauthorized: Your authentication token has expired. "
            "Please try signing in again.",
            {"httpConnectionFailed": {"httpStatusCode": 401}},
            "auth",
        ),
        ("Not logged in", None, "auth"),
        ("Too many requests", "rateLimitExceeded", "rate"),
        ("unexpected status 429 Too Many Requests", None, "rate"),
        ("stream disconnected before completion: error sending request", None, "network"),
        ("Connection failed", {"httpConnectionFailed": {"httpStatusCode": None}}, "network"),
        ("Internal server error", "internalServerError", "server"),
        ("unexpected status 503 Service Unavailable", None, "server"),
        ("Your input exceeds the context window of this model", "contextWindowExceeded", "too_long"),
        (
            '{"error": {"message": "The \'gpt-9\' model is not supported when using Codex with a ChatGPT '
            'account.", "type": "invalid_request_error"}}',
            "other",
            "model",
        ),
        ("flagged", "cyberPolicy", "policy"),
        ("Что-то странное", None, "error"),
    ],
)
def test_classify(message: str, info: Any, reason: str) -> None:
    got, text = errors.classify(message, info, model="gpt-9")
    assert got == reason
    assert text and "{" not in text.split(":", 1)[0]


def test_error_texts_are_readable() -> None:
    _, text = errors.classify(
        "You’ve hit your usage limit. Try again at Oct 19th, 2026 8:53 AM.", "usageLimitExceeded"
    )
    assert (
        text
        == "Лимит подписки ChatGPT исчерпан: You’ve hit your usage limit. Try again at Oct 19th, 2026 8:53 AM"
    )
    _, text = errors.classify(
        '{"error": {"message": "The model is not supported"}}', "badRequest", model="gpt-x"
    )
    assert text == errors.MODEL.format(model="gpt-x")
    _, text = errors.classify("unexpected status 418: teapot, url: http://127.0.0.1:1/v1/responses")
    assert text == "Ошибка ChatGPT: unexpected status 418: teapot"
    assert errors.classify_turn_error(None) == ("error", "Ошибка ChatGPT: без подробностей")
    assert errors.classify_turn_error({"message": "Not logged in"})[1] == errors.NOT_LOGGED_IN
    assert errors.error_info({"httpConnectionFailed": {"httpStatusCode": 401}}) == (
        "httpConnectionFailed",
        401,
    )
    assert errors.error_info("usageLimitExceeded") == ("usageLimitExceeded", None)


class TransportClosedError(Exception):
    """Как в SDK (openai_codex.errors) — проверяется по имени класса."""


def test_explain_exception() -> None:
    assert errors.explain_exception(TransportClosedError("closed")) == ("dead", errors.DEAD)
    assert errors.explain_exception(RuntimeGone("closed")) == ("dead", errors.DEAD)
    assert errors.explain_exception(FileNotFoundError("codex"))[0] == "not_installed"
    reason, text = errors.explain_exception(
        TransportClosedError(
            "Codex process closed stdout. stderr_tail=WARNING: x\n"
            'Error: invalid type: string "x", expected u64\nin `thread_unload_delay_secs`\n\n'
            "Stack backtrace:\n 0: ?"
        ),
        starting=True,
    )
    assert reason == "config"
    assert (
        text == 'Ошибка настроек Codex: invalid type: string "x", expected u64 in `thread_unload_delay_secs`'
    )
    reason, text = errors.explain_exception(OSError("Permission denied"), starting=True)
    assert (reason, text) == ("start", "Компонент ChatGPT (Codex) не запустился: Permission denied")
    reason, text = errors.explain_exception(
        RuntimeError("failed to request device code: error sending request for url (https://auth.openai.com)")
    )
    assert reason == "network" and text.startswith("Нет связи с ChatGPT (failed to request device code")


# --- строгая схема для outputSchema -------------------------------------------------------------------------


def test_strict_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Название"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["a", "b"]},
                        "value": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["name", "actions"],
    }
    strict = strict_schema(schema)
    assert strict == {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Название"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["a", "b"]},
                        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["type", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["name", "actions"],
        "additionalProperties": False,
    }
    assert strict_schema({"name": "answer", "schema": {"type": "object", "properties": {}}}) == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    # сложные конструкции — без outputSchema (просьба словами)
    assert strict_schema({"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}) is None
    assert strict_schema({"$ref": "#/defs/x"}) is None


# --- разбор событий хода ------------------------------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []

    def message_started(self, item_id: str) -> None:
        self.log.append(("start", item_id))

    def delta(self, item_id: str, text: str) -> None:
        self.log.append(("delta", item_id, text))

    def message_done(self, item_id: str, text: str, phase: str | None) -> None:
        self.log.append(("done", item_id, text, phase))

    def search(self, item_id: str, query: str, done: bool, urls: list[str]) -> None:
        self.log.append(("search", item_id, query, done, urls))

    def retrying(self, error: dict[str, Any]) -> None:
        self.log.append(("retry", error["message"]))


def ev(method: str, **params: Any) -> CodexEvent:
    return CodexEvent(method, params)


def test_turn_translator() -> None:
    sink = Sink()
    tr = TurnTranslator(sink)
    events = [
        ev("turn/started", turn={"id": "t1"}),
        ev("item/started", item={"type": "userMessage", "id": "u1"}),
        ev("item/started", item={"type": "agentMessage", "id": "m1", "text": ""}),
        ev("item/agentMessage/delta", itemId="m1", delta="Ищу"),
        ev("item/agentMessage/delta", itemId="m1", delta="…"),
        ev(
            "item/completed", item={"type": "agentMessage", "id": "m1", "text": "Ищу…", "phase": "commentary"}
        ),
        ev("item/started", item={"type": "webSearch", "id": "w1", "query": "погода"}),
        ev(
            "item/completed",
            item={
                "type": "webSearch",
                "id": "w1",
                "query": "",
                "action": {"type": "openPage", "url": "https://a.ru/x"},
            },
        ),
        ev("error", error={"message": "Reconnecting... 1/5"}, willRetry=True),
        ev("item/started", item={"type": "dynamicToolCall", "id": "c1", "tool": "remember"}),
        ev("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 100, "outputTokens": 7}}),
        # сообщение пришло целиком, без кусков
        ev("item/started", item={"type": "agentMessage", "id": "m2", "text": ""}),
        ev(
            "item/completed",
            item={
                "type": "agentMessage",
                "id": "m2",
                "text": "Тепло, [источник](https://a.ru/x).",
                "phase": "final_answer",
            },
        ),
        ev("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 50, "outputTokens": 3}}),
    ]
    assert not any(tr.feed(e) for e in events)
    assert tr.feed(ev("turn/completed", turn={"id": "t1", "status": "completed", "error": None}))
    out = tr.outcome
    assert out.status == "completed" and out.error is None
    assert (out.input_tokens, out.output_tokens) == (150, 10)
    assert out.text == "Ищу…\n\nТепло, [источник](https://a.ru/x)."
    assert out.final == "Тепло, [источник](https://a.ru/x)."
    assert out.searched and out.urls == ["https://a.ru/x"]
    assert out.retry_error == {"message": "Reconnecting... 1/5"}
    assert sink.log == [
        ("start", "m1"),
        ("delta", "m1", "Ищу"),
        ("delta", "m1", "…"),
        ("done", "m1", "Ищу…", "commentary"),
        ("search", "w1", "погода", False, []),
        ("search", "w1", "https://a.ru/x", True, ["https://a.ru/x"]),
        ("retry", "Reconnecting... 1/5"),
        ("start", "m2"),
        ("delta", "m2", "Тепло, [источник](https://a.ru/x)."),
        ("done", "m2", "Тепло, [источник](https://a.ru/x).", "final_answer"),
    ]
    assert markdown_links(out.final) == [("источник", "https://a.ru/x")]


def test_turn_translator_failures() -> None:
    tr = TurnTranslator()
    tr.feed(ev("error", error={"message": "boom", "codexErrorInfo": "other"}, willRetry=False))
    assert tr.feed(ev("turn/completed", turn={"status": "failed", "error": {"message": "лимит"}}))
    assert tr.outcome.status == "failed" and tr.outcome.error == {"message": "лимит"}
    assert tr.outcome.final == ""

    tr = TurnTranslator()
    tr.feed(ev("item/started", item={"type": "collabAgentToolCall", "id": "x"}))
    assert tr.outcome.abort_reason == "subagent"
    assert tr.feed(CodexEvent(GONE, {"error": "closed"}))
    assert tr.outcome.status == "gone" and tr.outcome.error == {"message": "closed"}

    tr = TurnTranslator()
    tr.feed(ev("item/completed", item={"type": "agentMessage", "id": "a", "text": "Первое", "phase": None}))
    tr.feed(ev("item/completed", item={"type": "agentMessage", "id": "b", "text": "Второе", "phase": None}))
    assert tr.outcome.final == "Второе"


# --- контекст хода -----------------------------------------------------------------------------------------


def test_context_sections_are_sent_once_and_on_change() -> None:
    settings = Settings()
    settings.profile.name = "Тони"
    sections = prompt.context_sections(
        settings, source="voice", mode="all", memory="Любит кофе.", has_agent_tool=True
    )
    assert "Обращайся к пользователю «сэр»." in sections["persona"]
    assert sections["mode"].endswith("запусти агента инструментом run_agent.")
    assert sections["source"] == prompt.SOURCE_VOICE and sections["custom"] == ""
    assert sections["profile"] == "Пользователь: имя — Тони, город — Москва."
    sent: dict[str, str] = {}
    first = prompt.context_text(sections, sent)
    assert first.startswith(sections["persona"]) and first.endswith(sections["time"])
    assert "Что ты знаешь о пользователе: Любит кофе." in first

    second = prompt.context_text(dict(sections), sent)
    assert second == sections["time"]

    settings.profile.address = "босс"
    settings.ai.custom_instructions = "Шути чаще."
    changed = prompt.context_sections(settings, source="text", mode="chat", memory="", has_agent_tool=False)
    third = prompt.context_text(changed, sent)
    assert changed["persona"] + " " + prompt.CHANGED in third and "«босс»" in changed["persona"]
    assert prompt.MODE_CHAT + " " + prompt.CHANGED in third
    assert "Память о пользователе теперь пуста." in third
    assert "Особые пожелания пользователя" in third and "Шути чаще." in third
    assert prompt.context_text(changed, sent) == changed["time"]
    assert "Ты работаешь внутри Jarvis" in prompt.developer_instructions()


def test_history_text() -> None:
    messages = [
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "assistant", "content": "Здравствуйте,\n сэр."},
        {"role": "user", "content": [{"type": "text", "text": "Картинка"}]},
    ]
    text = prompt.history_text(messages, "Джарвис")
    assert text == (
        "Ранее в этом чате (пересказ прошлой переписки):\n"
        "Пользователь: Привет\nДжарвис: Здравствуйте, сэр.\nПользователь: Картинка"
    )
    assert prompt.history_text([], "Джарвис") == ""
    long = prompt.history_text([{"role": "user", "content": "x" * 50}], "Д", limit=10)
    assert long.endswith("…" + "x" * 10)


# --- ответы Codex и события SDK ---------------------------------------------------------------------------


def test_server_requests_are_denied() -> None:
    assert server_reply("item/commandExecution/requestApproval", {}) == ({"decision": "decline"}, None)
    assert server_reply("item/fileChange/requestApproval", {}) == ({"decision": "decline"}, None)
    assert server_reply("execCommandApproval", {}) == ({"decision": "denied"}, None)
    assert server_reply("mcpServer/elicitation/request", {}) == ({"action": "decline"}, None)
    assert server_reply("item/tool/requestUserInput", {}) == ({"answers": {}}, None)
    result, error = server_reply("currentTime/read", None)
    assert error is None and result is not None and result["currentTimeAt"] > 1_700_000_000
    result, error = server_reply("attestation/generate", {})
    assert result is None and error is not None and error["code"] == -32601
    assert tool_failure("нет") == {"contentItems": [{"type": "inputText", "text": "нет"}], "success": False}


class _Item(BaseModel):
    item_id: str = Field(alias="itemId")
    delta: str


class _Note:
    def __init__(self, method: str, payload: Any) -> None:
        self.method = method
        self.payload = payload


class _Unknown:
    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params


def test_sdk_notifications_become_protocol_dicts() -> None:
    event = to_event(_Note("item/agentMessage/delta", _Item(itemId="m1", delta="При")))
    assert event == CodexEvent("item/agentMessage/delta", {"itemId": "m1", "delta": "При"})
    raw = to_event(_Note("thread/new/thing", _Unknown({"threadId": "t", "x": 1})))
    assert raw == CodexEvent("thread/new/thing", {"threadId": "t", "x": 1})


# --- компонент ----------------------------------------------------------------------------------------------


@pytest.fixture
def pylib(data_dir: Path) -> Any:
    folder = extras.extras_dir()
    yield folder
    while str(folder) in sys.path:
        sys.path.remove(str(folder))


def test_find_sdk(pylib: Path) -> None:
    assert component.find_sdk() == component.SdkInfo(False)
    (pylib / "openai_codex").mkdir()
    (pylib / "openai_codex" / "__init__.py").write_text("", encoding="utf-8")
    info = component.find_sdk()
    assert info.installed is False and "openai-codex-cli-bin" in info.error
    bin_dir = pylib / "codex_cli_bin" / "bin"
    bin_dir.mkdir(parents=True)
    (pylib / "codex_cli_bin" / "__init__.py").write_text("", encoding="utf-8")
    info = component.find_sdk()
    assert info.installed is False and "скачайте компонент заново" in info.error
    exe = "codex.exe" if sys.platform == "win32" else "codex"
    (bin_dir / exe).write_bytes(b"")
    info = component.find_sdk()
    assert info.installed is True and info.binary == str((bin_dir / exe).resolve())
    assert str(pylib) in sys.path


def test_codex_env_and_paths(data_dir: Path) -> None:
    env = component.codex_env(AiSettings())
    assert env == {"CODEX_HOME": str(data_dir / "codex-home"), "NO_COLOR": "1"}
    env = component.codex_env(AiSettings(proxy="socks5://127.0.0.1:1080"))
    assert env["HTTPS_PROXY"] == env["ALL_PROXY"] == "socks5://127.0.0.1:1080"
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert component.work_dir() == data_dir / "chatgpt" and component.work_dir().is_dir()
    assert component.has_auth_file() is False
    component.auth_file().write_text("{}", encoding="utf-8")
    assert component.has_auth_file() is True
    assert component.SDK_SPEC == "openai-codex==0.163.0"
    assert component.thread_config(web_search=True) == {"web_search": "live"}
    assert component.toml_str('C:\\Users\\Тони "x"') == '"C:\\\\Users\\\\Тони \\"x\\""'
    overrides = component.build_overrides()
    assert "include_environment_context=false" in overrides and "features.multi_agent=false" in overrides
    assert all("=" in o for o in overrides)
