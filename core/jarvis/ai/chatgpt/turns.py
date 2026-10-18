"""События хода Codex → итог хода и вызовы «приёмника» (поток текста, поиск, повторы).

Что приходит в ходе (codex 0.163.0): item/started и item/completed (agentMessage, dynamicToolCall, webSearch,
reasoning…), item/agentMessage/delta, thread/tokenUsage/updated (расход каждого запроса к модели), error
(willRetry — Codex повторит сам) и в конце turn/completed {turn: {status, error}}.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.ai.chatgpt.runtime import CodexEvent

GONE = "jarvis/gone"  # синтетическое событие: процесс Codex умер посреди хода
SUBAGENT_ITEMS = ("collabAgentToolCall", "subAgentActivity")

_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


class TurnSink(Protocol):
    def message_started(self, item_id: str) -> None: ...

    def delta(self, item_id: str, text: str) -> None: ...

    def message_done(self, item_id: str, text: str, phase: str | None) -> None: ...

    def search(self, item_id: str, query: str, done: bool, urls: list[str]) -> None: ...

    def retrying(self, error: dict[str, Any]) -> None: ...


class NullSink:
    def message_started(self, item_id: str) -> None:
        return None

    def delta(self, item_id: str, text: str) -> None:
        return None

    def message_done(self, item_id: str, text: str, phase: str | None) -> None:
        return None

    def search(self, item_id: str, query: str, done: bool, urls: list[str]) -> None:
        return None

    def retrying(self, error: dict[str, Any]) -> None:
        return None


@dataclass
class TurnOutcome:
    status: str = ""  # completed | interrupted | failed | gone | "" (не закончился)
    error: dict[str, Any] | None = None
    retry_error: dict[str, Any] | None = None
    abort_reason: str = ""  # region | network | subagent | limit — ход прервали мы
    messages: list[tuple[str, str, str | None]] = field(default_factory=list)  # (id, текст, фаза)
    input_tokens: int = 0
    output_tokens: int = 0
    searched: bool = False
    urls: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(t.strip() for _, t, _ in self.messages if t.strip())

    @property
    def final(self) -> str:
        """Итоговый ответ: сообщение с фазой final_answer, иначе последнее сообщение."""
        for _, text, phase in reversed(self.messages):
            if phase == "final_answer" and text.strip():
                return text.strip()
        for _, text, _ in reversed(self.messages):
            if text.strip():
                return text.strip()
        return ""


def markdown_links(text: str) -> list[tuple[str, str]]:
    """[заголовок](https://…) из текста ответа."""
    return [(m.group(1).strip(), m.group(2)) for m in _MD_LINK.finditer(text or "")]


def _item(params: dict[str, Any]) -> dict[str, Any]:
    item = params.get("item")
    return item if isinstance(item, dict) else {}


def _search_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    action = item.get("action")
    if isinstance(action, dict) and isinstance(action.get("url"), str):
        urls.append(action["url"])
    results = item.get("results")
    if isinstance(results, list):
        for result in results:
            if isinstance(result, dict) and isinstance(result.get("url"), str):
                urls.append(result["url"])
    return [u for u in urls if u.startswith(("http://", "https://"))]


def _search_query(item: dict[str, Any]) -> str:
    query = item.get("query")
    if isinstance(query, str) and query.strip():
        return query.strip()
    action = item.get("action")
    if isinstance(action, dict):
        for key in ("query", "url"):
            if isinstance(action.get(key), str) and action[key].strip():
                return action[key].strip()
        queries = action.get("queries")
        if isinstance(queries, list) and queries:
            return str(queries[0])
    return ""


class TurnTranslator:
    """Разбирает события одного хода; feed() вернёт True на turn/completed (или смерти процесса)."""

    def __init__(self, sink: TurnSink | None = None) -> None:
        self.sink: TurnSink = sink or NullSink()
        self.outcome = TurnOutcome()
        self._streamed: dict[str, int] = {}

    def feed(self, event: CodexEvent) -> bool:
        method, params = event.method, event.params
        out = self.outcome
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                item_id = str(params.get("itemId") or "")
                self._streamed[item_id] = self._streamed.get(item_id, 0) + len(delta)
                self.sink.delta(item_id, delta)
        elif method in ("item/started", "item/completed"):
            self._on_item(method == "item/completed", _item(params))
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            last = usage.get("last") if isinstance(usage, dict) else None
            if isinstance(last, dict):
                out.input_tokens += int(last.get("inputTokens") or 0)
                out.output_tokens += int(last.get("outputTokens") or 0)
        elif method == "error":
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            if params.get("willRetry"):
                out.retry_error = error
                self.sink.retrying(error)
            else:
                out.error = error
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            out.status = str(turn.get("status") or "completed")
            if isinstance(turn.get("error"), dict):
                out.error = turn["error"]
            return True
        elif method == GONE:
            out.status = "gone"
            out.error = {"message": str(params.get("error") or "")}
            return True
        return False

    def _on_item(self, completed: bool, item: dict[str, Any]) -> None:
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "agentMessage":
            if not completed:
                self.sink.message_started(item_id)
                return
            text = str(item.get("text") or "")
            phase = item.get("phase") if isinstance(item.get("phase"), str) else None
            if text and not self._streamed.get(item_id):
                self.sink.delta(item_id, text)  # сообщение пришло целиком, без кусков
            self.outcome.messages.append((item_id, text, phase))
            self.sink.message_done(item_id, text, phase)
        elif kind in SUBAGENT_ITEMS and not self.outcome.abort_reason:
            self.outcome.abort_reason = "subagent"  # под-агентов Jarvis не допускает — ход прервут
        elif kind == "webSearch":
            self.outcome.searched = True
            urls = _search_urls(item) if completed else []
            for url in urls:
                if url not in self.outcome.urls:
                    self.outcome.urls.append(url)
            self.sink.search(item_id, _search_query(item), completed, urls)
