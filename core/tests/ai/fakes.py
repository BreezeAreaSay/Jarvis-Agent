"""Помощники тестов ИИ: модель по сценарию (httpx.MockTransport) и заглушка элементов окон для агента."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from jarvis.ai.uia import UiElement, UiError, UiSnapshot

Spec = dict[str, Any]


def reply(text: str, *, usage: tuple[int, int] = (100, 20)) -> Spec:
    """Ответ модели текстом."""
    return {"text": text, "calls": [], "usage": usage}


def calls(*items: tuple[str, dict[str, Any]], text: str = "", usage: tuple[int, int] = (120, 30)) -> Spec:
    """Ответ модели вызовами инструментов: calls(("run_command", {"text": "…"}), …)."""
    numbered = [(f"call_{name}_{i}", name, args) for i, (name, args) in enumerate(items)]
    return {"text": text, "calls": numbered, "usage": usage}


def sse(chunks: list[dict[str, Any] | str], *, done: bool = True) -> bytes:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _pieces(text: str, n: int = 3) -> list[str]:
    if not text:
        return []
    size = max(1, len(text) // n)
    return [text[i : i + size] for i in range(0, len(text), size)]


def stream_chunks(spec: Spec) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = [
        {"choices": [{"index": 0, "delta": {"content": piece}}]} for piece in _pieces(spec["text"])
    ]
    for index, (call_id, name, args) in enumerate(spec["calls"]):
        raw = json.dumps(args, ensure_ascii=False)
        half = len(raw) // 2
        first = {
            "index": index,
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": raw[:half]},
        }
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [first]}}]})
        rest = {"index": index, "function": {"arguments": raw[half:]}}
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [rest]}}]})
    finish = "tool_calls" if spec["calls"] else "stop"
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    prompt, completion = spec["usage"]
    chunks.append({"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}})
    return chunks


def completion(spec: Spec, model: str = "gpt-5.4-mini") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": spec["text"] or None}
    if spec["calls"]:
        message["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for cid, name, args in spec["calls"]
        ]
    prompt, done = spec["usage"]
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if spec["calls"] else "stop"}
        ],
        "usage": {"prompt_tokens": prompt, "completion_tokens": done},
    }


Handler = Callable[[httpx.Request], Any]


class ScriptedLlm:
    """Модель по сценарию: каждый POST …/chat/completions берёт следующий ответ из очереди.

    Ответ — Spec (reply/calls), готовый httpx.Response или функция (request, body) → Response (можно async).
    Запросы к другим адресам (поиск, погода) уходят в routes: {"host": handler}.
    """

    def __init__(self, *responses: Any) -> None:
        self.responses: list[Any] = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[str, Handler] = {}
        self.other: list[httpx.Request] = []

    def add(self, *responses: Any) -> None:
        self.responses.extend(responses)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            self.other.append(request)
            route = self.routes.get(request.url.host)
            if route is None:
                return httpx.Response(404, json={"error": {"message": f"нет маршрута {request.url}"}})
            result = route(request)
            return await result if asyncio.iscoroutine(result) else result
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.responses:
            return httpx.Response(500, json={"error": {"message": "сценарий закончился"}})
        item = self.responses.pop(0)
        if callable(item):
            result = item(request, body)
            return await result if asyncio.iscoroutine(result) else result
        if isinstance(item, httpx.Response):
            return item
        if body.get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(stream_chunks(item))
            )
        return httpx.Response(200, json=completion(item, body.get("model", "")))


def chunked(data: bytes, sizes: list[int]) -> AsyncIterator[bytes]:
    """Тело ответа кусками заданной длины (разрезает строки SSE и даже символы UTF-8)."""

    async def gen() -> AsyncIterator[bytes]:
        pos = 0
        for size in sizes:
            if pos >= len(data):
                break
            yield data[pos : pos + size]
            pos += size
        if pos < len(data):
            yield data[pos:]

    return gen()


async def no_sleep(_: float) -> None:
    return None


# --- агент -------------------------------------------------------------------------------------------------


class FakeUi:
    """«Почта»: кнопка «Написать» открывает новое письмо с полями и кнопкой «Отправить»."""

    def __init__(self) -> None:
        self.window = "Входящие — Почта"
        self.elements = [
            UiElement("e1", "Написать", "Button", (10, 10, 110, 40)),
            UiElement("e2", "Входящие", "ListItem", (10, 60, 200, 80)),
        ]
        self.clicks: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.snapshots = 0

    def snapshot(self, limit: int = 150) -> UiSnapshot:
        self.snapshots += 1
        return UiSnapshot(self.window, "HxOutlook.exe", [UiElement(**vars(e)) for e in self.elements[:limit]])

    def _find(self, element_id: str) -> UiElement:
        element = next((e for e in self.elements if e.id == element_id), None)
        if element is None:
            raise UiError(f"Элемента {element_id} нет")
        return element

    def click(self, element_id: str) -> str:
        element = self._find(element_id)
        self.clicks.append(element.name)
        if element.name == "Написать":
            self.window = "Новое письмо — Почта"
            self.elements = [
                UiElement("e1", "Кому", "Edit", (10, 10, 400, 30), focused=True),
                UiElement("e2", "Тема", "Edit", (10, 40, 400, 60)),
                UiElement("e3", "Текст письма", "Document", (10, 70, 400, 300)),
                UiElement("e4", "Отправить", "Button", (10, 310, 110, 340)),
            ]
        elif element.name == "Отправить":
            self.window = "Входящие — Почта"
        return "нажато"

    def set_text(self, element_id: str, text: str) -> str:
        element = self._find(element_id)
        self.typed.append((element.name, text))
        element.value = text
        return "текст вставлен"
