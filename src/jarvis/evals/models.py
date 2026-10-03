"""Scripted-модель: бэкенд, который отвечает заранее записанными репликами (eval и тесты).

Через неё проходит тот же конвейер, что и с настоящей моделью: Gateway, разбор, проверка, ремонт,
Tool Runtime. Реплика — текст, JSON-объект (сериализуется как есть), сбой сервера или зависание.
Запросы запоминаются: по ним проверяют, что модель видела (например, данные только в блоке DATA).
"""

import asyncio
import json
from collections import deque
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, Field, JsonValue, model_validator

from jarvis.domain.errors import JarvisError, ModelRequestRejected, ModelTimeout, ModelUnavailable
from jarvis.domain.models import BackendRequest, BackendResponse, BackendStatus, ModelCapabilities, ModelInfo

SCRIPTED_CAPABILITIES = ModelCapabilities(structured_output=True, context_window=16384)


class ModelScriptExhausted(JarvisError):
    category = "script_mismatch"


class ModelReply(BaseModel, frozen=True, extra="forbid", populate_by_name=True):
    text: str | None = None
    json_: JsonValue = Field(default=None, alias="json")  # объект ответа; сериализуется как есть
    error: Literal["unavailable", "timeout", "rejected"] | None = None
    hang: bool = False  # ответ не приходит: запрос прерывают отмена или лимит времени
    finish_reason: str = "stop"

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        kinds = [self.text is not None, self.json_ is not None, self.error is not None, self.hang]
        if sum(kinds) != 1:
            raise ValueError("реплика модели — ровно одно из: text, json, error, hang")
        return self

    def body(self) -> str:
        if self.text is not None:
            return self.text
        return json.dumps(self.json_, ensure_ascii=False)


class ScriptedModel:
    def __init__(
        self,
        replies: Sequence[ModelReply],
        *,
        capabilities: ModelCapabilities = SCRIPTED_CAPABILITIES,
        endpoint: str = "scripted",
        hung: asyncio.Event | None = None,
    ) -> None:
        self._replies = deque(replies)
        self._info = ModelInfo(endpoint=endpoint, model="scripted", capabilities=capabilities)
        self._hung = hung if hung is not None else asyncio.Event()
        self.requests: list[BackendRequest] = []

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def remaining(self) -> int:
        return len(self._replies)

    async def complete(self, request: BackendRequest) -> BackendResponse:
        self.requests.append(request)
        if not self._replies:
            raise ModelScriptExhausted("реплики scripted-модели закончились")
        reply = self._replies.popleft()
        if reply.hang:
            self._hung.set()
            await asyncio.Event().wait()
        match reply.error:
            case "unavailable":
                raise ModelUnavailable("scripted: сервер недоступен")
            case "timeout":
                raise ModelTimeout("scripted: сервер не ответил вовремя")
            case "rejected":
                raise ModelRequestRejected("scripted: сервер отклонил запрос")
            case None:
                pass
        text = reply.body()
        prompt = sum(len(message.content) for message in request.messages) // 4
        return BackendResponse(
            text=text,
            finish_reason=reply.finish_reason,
            prompt_tokens=prompt,
            completion_tokens=max(1, len(text) // 4),
            latency_ms=1,
        )

    async def describe(self) -> BackendStatus:
        return BackendStatus(models=["scripted"], context_window=self._info.capabilities.context_window)
