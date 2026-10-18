"""Разбор потокового вывода программ (stream-json, по объекту JSON на строку) в общие события хода.

Claude Code и Qwen Code пишут сообщения в формате Agent SDK (сверено с документацией Claude Code и
исходниками Qwen Code 0.25): system/init (session_id, model, mcp_servers), stream_event (message_start,
content_block_delta с text_delta — при --include-partial-messages), assistant (целое сообщение: text и
tool_use; поле error — authentication_failed, rate_limit, billing_error…), user (tool_result),
system/api_retry, rate_limit_event и в конце result (is_error, result, session_id, usage).

Gemini CLI (packages/core/src/output/types.ts) пишет init (session_id, model), message (role, content,
delta), tool_use, tool_result, error (severity) и result (status, error, stats).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

EventKind = Literal[
    "init",
    "round",
    "delta",
    "message",
    "tool_use",
    "tool_result",
    "retry",
    "rate_limit",
    "warning",
    "error",
    "result",
]


@dataclass
class CliEvent:
    kind: EventKind
    text: str = ""
    session_id: str = ""
    model: str = ""
    item: str = ""  # id сообщения или вызова инструмента
    name: str = ""  # имя инструмента
    code: str = ""  # категория ошибки или подтип результата
    ok: bool = True
    usage: tuple[int, int] = (0, 0)  # (вход, выход) токенов
    data: dict[str, Any] = field(default_factory=dict)


def parse_line(line: bytes | str) -> dict[str, Any] | None:
    """Строка вывода → объект JSON (не JSON — None: предупреждения, баннеры)."""
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        log.debug("строка вывода не JSON: %r", text[:200])
        return None
    return value if isinstance(value, dict) else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class ClaudeStream:
    """stream-json Claude Code и Qwen Code (формат Agent SDK)."""

    def __init__(self) -> None:
        self._current = ""
        self._streamed: set[str] = set()

    def feed(self, obj: dict[str, Any]) -> list[CliEvent]:
        kind = obj.get("type")
        if obj.get("parent_tool_use_id"):
            return []  # сообщения под-агентов (у Jarvis их нет: встроенные инструменты выключены)
        session = str(obj.get("session_id") or "")
        if kind == "system":
            return self._system(obj, session)
        if kind == "stream_event":
            return self._partial(obj.get("event") if isinstance(obj.get("event"), dict) else {})
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            out = []
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(
                        CliEvent(
                            "tool_result",
                            text=_content_text(block.get("content")),
                            item=str(block.get("tool_use_id") or ""),
                            ok=not block.get("is_error"),
                        )
                    )
            return out
        if kind == "rate_limit_event":
            info = obj.get("rate_limit_info") if isinstance(obj.get("rate_limit_info"), dict) else {}
            if info.get("status") == "rejected":
                return [CliEvent("rate_limit", code=str(info.get("errorCode") or ""), data=dict(info))]
            return []
        if kind == "auth_status" and obj.get("error"):
            return [CliEvent("error", code="authentication_failed", text=str(obj.get("error")))]
        if kind == "result":
            return [self._result(obj, session)]
        return []

    def _system(self, obj: dict[str, Any], session: str) -> list[CliEvent]:
        sub = obj.get("subtype")
        if sub in ("init", "session_start"):
            data = {"mcp_servers": obj.get("mcp_servers") or [], "tools": obj.get("tools") or []}
            if obj.get("mcp_server_errors"):
                data["mcp_server_errors"] = obj.get("mcp_server_errors")
            return [CliEvent("init", session_id=session, model=str(obj.get("model") or ""), data=data)]
        if sub == "api_retry":
            status = obj.get("error_status")
            return [CliEvent("retry", code=str(obj.get("error") or ""), data={"status": status})]
        if sub == "permission_denied":
            return [
                CliEvent("warning", name=str(obj.get("tool_name") or ""), text=str(obj.get("message") or ""))
            ]
        return []

    def _partial(self, event: dict[str, Any]) -> list[CliEvent]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            self._current = str(message.get("id") or "")
            return [CliEvent("round", item=self._current)]
        if kind == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            text = delta.get("text")
            if delta.get("type") == "text_delta" and isinstance(text, str) and text:
                self._streamed.add(self._current)
                return [CliEvent("delta", text=text, item=self._current)]
        return []

    def _assistant(self, obj: dict[str, Any]) -> list[CliEvent]:
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        message_id = str(message.get("id") or "")
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        text = "".join(
            str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        if obj.get("error"):
            # «Invalid API key · Please run /login», «Claude AI usage limit reached…» — это не ответ, а ошибка
            return [CliEvent("error", code=str(obj["error"]), text=text)]
        out = []
        if text:
            streamed = message_id in self._streamed
            out.append(CliEvent("message", text=text, item=message_id, data={"streamed": streamed}))
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(
                    CliEvent(
                        "tool_use",
                        item=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        data={"input": block.get("input")},
                    )
                )
        return out

    @staticmethod
    def _result(obj: dict[str, Any], session: str) -> CliEvent:
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}

        def num(key: str) -> int:
            value = usage.get(key)
            return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0

        prompt = num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens")
        subtype = str(obj.get("subtype") or "")
        ok = not obj.get("is_error") and subtype in ("success", "")
        text = obj.get("result") if isinstance(obj.get("result"), str) else ""
        if not ok and not text:
            errors = obj.get("errors")
            error = obj.get("error")
            if isinstance(errors, list) and errors:
                text = "; ".join(str(e) for e in errors)
            elif isinstance(error, dict):
                text = str(error.get("message") or "")
            elif isinstance(error, str):
                text = error
        return CliEvent(
            "result",
            ok=ok,
            text=text or "",
            session_id=session,
            code=subtype,
            usage=(prompt, num("output_tokens")),
            data={"structured": obj.get("structured_output"), "status": obj.get("api_error_status")},
        )


class GeminiStream:
    """stream-json Gemini CLI."""

    def __init__(self) -> None:
        self._new_round = True

    def feed(self, obj: dict[str, Any]) -> list[CliEvent]:
        kind = obj.get("type")
        if kind == "init":
            return [
                CliEvent(
                    "init", session_id=str(obj.get("session_id") or ""), model=str(obj.get("model") or "")
                )
            ]
        if kind == "message":
            if obj.get("role") != "assistant":
                return []
            text = obj.get("content") if isinstance(obj.get("content"), str) else ""
            if not text:
                return []
            out = []
            if self._new_round:
                self._new_round = False
                out.append(CliEvent("round"))
            out.append(CliEvent("delta", text=text))
            return out
        if kind == "tool_use":
            self._new_round = True
            return [
                CliEvent(
                    "tool_use",
                    item=str(obj.get("tool_id") or ""),
                    name=str(obj.get("tool_name") or ""),
                    data={"input": obj.get("parameters")},
                )
            ]
        if kind == "tool_result":
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            text = (
                obj.get("output") if isinstance(obj.get("output"), str) else str(error.get("message") or "")
            )
            return [
                CliEvent(
                    "tool_result",
                    text=text,
                    item=str(obj.get("tool_id") or ""),
                    ok=obj.get("status") != "error",
                )
            ]
        if kind == "error":
            severity = "error" if obj.get("severity") == "error" else "warning"
            return [CliEvent(severity, text=str(obj.get("message") or ""))]  # type: ignore[arg-type]
        if kind == "result":
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}

            def num(key: str) -> int:
                value = stats.get(key)
                return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0

            return [
                CliEvent(
                    "result",
                    ok=obj.get("status") != "error",
                    text=str(error.get("message") or ""),
                    code=str(error.get("type") or ""),
                    usage=(num("input_tokens"), num("output_tokens")),
                )
            ]
        return []
