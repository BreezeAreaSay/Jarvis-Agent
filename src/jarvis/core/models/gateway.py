"""Model Gateway — единственный путь ядра к модели (03-contracts.md §3, ADR 0008, ADR 0009, ADR 0023).

Ядро обращается к модели по роли. Gateway знает требования ролей и возможности назначенной модели и
решает по возможностям, а не по имени модели: если сервер ограничивает ответ схемой
(`structured_output`), схема уходит в запрос, иначе — описывается в промпте. Конвейер structured
output:

    отрисовать промпт → бюджет (вызов и токены) → бэкенд → JSON → схема pydantic
    → семантическая проверка вызывающего → ошибка: повтор с объяснением (не больше repair_attempts)
    → всё ещё ошибка: InvalidModelOutput

Каждая попытка — запись `model_calls` (промпт и ответ) и событие `model.called` (без текста).
Модель ничего не исполняет: результат — проверенное значение, которое вызывающий передаёт дальше.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_core import ErrorDetails

from jarvis.core.budget import BudgetMeter
from jarvis.core.models.prompt import estimate_tokens, render
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import (
    ConfigError,
    ErrorInfo,
    InvalidModelOutput,
    ModelError,
    ModelUnavailable,
)
from jarvis.domain.ids import TaskId
from jarvis.domain.models import (
    BackendRequest,
    BackendResponse,
    ChatMessage,
    ModelCallRecord,
    ModelCallStatus,
    ModelCapabilities,
    ModelInfo,
    ModelRole,
    Prompt,
)
from jarvis.domain.trace import EventKind
from jarvis.ports.clock import Clock
from jarvis.ports.models import ModelBackend
from jarvis.ports.storage import UnitOfWorkFactory

RETRY_DELAY_S = 1.0  # пауза перед повтором, когда сервер недоступен (модель ещё грузится)
TRANSPORT_RETRIES = 1


@dataclass(frozen=True)
class RoleRequirements:
    min_context_window: int
    reply_tokens: int  # сколько токенов ответа роли нужно: резерв в окне и max_tokens запроса


# Требования ролей объявлены в ядре, возможности моделей — в конфиге (ADR 0008).
ROLE_REQUIREMENTS: Mapping[ModelRole, RoleRequirements] = {
    ModelRole.EXECUTOR: RoleRequirements(min_context_window=8192, reply_tokens=1024),
}


@dataclass(frozen=True)
class StructuredOutput[T: BaseModel]:
    """Какой ответ нужен: модель pydantic для разбора, JSON Schema для сервера и промпта (может быть
    строже модели — например, перечислять допустимые инструменты) и семантическая проверка."""

    model: type[T]
    schema: dict[str, JsonValue]
    check: Callable[[T], list[str]] | None = None


@dataclass(frozen=True)
class Generation[T: BaseModel]:
    value: T
    call_id: str  # принятая попытка
    attempts: int


class ModelGateway:
    def __init__(
        self,
        *,
        backends: Mapping[ModelRole, ModelBackend],
        uow: UnitOfWorkFactory,
        tracer: Tracer,
        clock: Clock,
        repair_attempts: int,
        retry_delay_s: float = RETRY_DELAY_S,
    ) -> None:
        for role, backend in backends.items():
            check_requirements(role, backend.info)
        self._backends = dict(backends)
        self._uow = uow
        self._tracer = tracer
        self._clock = clock
        self._repair_attempts = repair_attempts
        self._retry_delay_s = retry_delay_s

    def available(self, role: ModelRole) -> bool:
        return role in self._backends

    def capabilities(self, role: ModelRole) -> ModelCapabilities:
        return self._backend(role).info.capabilities

    def prompt_budget(self, role: ModelRole) -> int:
        """Токенов на промпт: окно модели минус ответ роли."""
        return self.capabilities(role).context_window - ROLE_REQUIREMENTS[role].reply_tokens

    async def generate[T: BaseModel](
        self,
        role: ModelRole,
        prompt: Prompt,
        output: StructuredOutput[T],
        *,
        task_id: TaskId,
        budget: BudgetMeter,
    ) -> Generation[T]:
        backend = self._backend(role)
        constrained = backend.info.capabilities.structured_output
        base = render(prompt)
        if not constrained:  # сервер схему не применит: модель должна увидеть её сама
            base = _with_schema(base, output.schema)
        messages = base
        attempt = repairs = retries = 0
        while True:
            attempt += 1
            budget.charge(BudgetLimit.MODEL_CALLS)
            budget.require_tokens()
            request = BackendRequest(
                messages=messages,
                json_schema=output.schema if constrained else None,
                max_tokens=ROLE_REQUIREMENTS[role].reply_tokens,
            )
            call = _Attempt(self._tracer.next_id(task_id, "mc"), task_id, role, backend.info, prompt, attempt)
            try:
                response = await backend.complete(request)
            except ModelUnavailable as exc:
                self._record(call, request, None, "error", error=exc.to_info())
                if retries < TRANSPORT_RETRIES:
                    retries += 1
                    await asyncio.sleep(self._retry_delay_s)
                    continue
                raise
            except ModelError as exc:
                self._record(call, request, None, "error", error=exc.to_info())
                raise
            except asyncio.CancelledError:
                self._record(call, request, None, "cancelled", best_effort=True)
                raise
            budget.add_tokens(_tokens(request, response))
            value, problems = _parse(response.text, output)
            self._record(call, request, response, "invalid" if value is None else "ok", problems=problems)
            if value is not None:
                return Generation(value=value, call_id=call.id, attempts=attempt)
            if repairs >= self._repair_attempts:
                raise InvalidModelOutput(
                    f"ответ модели не прошёл проверку после {repairs + 1} попыток: {'; '.join(problems)}",
                    role=role.value,
                    problems=list[JsonValue](problems),
                )
            repairs += 1
            messages = [*base, ChatMessage(role="assistant", content=response.text), _repair(problems)]

    def _backend(self, role: ModelRole) -> ModelBackend:
        backend = self._backends.get(role)
        if backend is None:
            raise ConfigError(
                f'роли {role} не назначена модель: добавьте [models.roles] {role} = "<эндпоинт>" '
                "(docs/development.md, «Настройка модели»)"
            )
        return backend

    def _record(
        self,
        call: "_Attempt",
        request: BackendRequest,
        response: BackendResponse | None,
        status: ModelCallStatus,
        *,
        problems: list[str] | None = None,
        error: ErrorInfo | None = None,
        best_effort: bool = False,
    ) -> None:
        record = ModelCallRecord(
            id=call.id,
            task_id=call.task_id,
            role=call.role,
            endpoint=call.info.endpoint,
            model=call.info.model,
            template_id=call.prompt.template_id,
            attempt=call.attempt,
            prompt_sha256=prompt_hash(request.messages, call.task_id),
            messages=request.messages,
            json_schema=request.json_schema is not None,
            response_text=response.text if response else None,
            status=status,
            problems=problems or [],
            prompt_tokens=response.prompt_tokens if response else None,
            completion_tokens=response.completion_tokens if response else None,
            latency_ms=response.latency_ms if response else None,
            prompt_ms=response.prompt_ms if response else None,
            finish_reason=response.finish_reason if response else None,
            error=error,
            created_at=self._clock.now(),
        )
        payload: dict[str, JsonValue] = {
            "call_id": call.id,
            "role": call.role.value,
            "endpoint": call.info.endpoint,
            "template": call.prompt.template_id,
            "attempt": call.attempt,
            "status": status,
            "constrained": request.json_schema is not None,
        }
        if response is not None:
            payload.update(
                {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "latency_ms": response.latency_ms,
                    "finish_reason": response.finish_reason,
                }
            )
        if problems:
            payload["problems"] = [shorten(problem, 200) for problem in problems[:5]]
        if error is not None:
            payload["error"] = {"category": error.category, "message": shorten(error.message, 300)}
        event = self._tracer.event(call.task_id, EventKind.MODEL_CALLED, payload)
        try:
            with self._uow() as uow:
                uow.model_calls.add(record)
                uow.trace.append([event])
                uow.commit()
        except Exception:
            if not best_effort:
                raise


@dataclass(frozen=True)
class _Attempt:
    id: str
    task_id: TaskId
    role: ModelRole
    info: ModelInfo
    prompt: Prompt
    attempt: int


def check_requirements(role: ModelRole, info: ModelInfo) -> None:
    """Модель, не подходящая роли, — ошибка конфигурации при старте, а не сбой посреди задачи."""
    required = ROLE_REQUIREMENTS[role]
    window = info.capabilities.context_window
    if window < required.min_context_window:
        raise ConfigError(
            f"эндпоинт {info.endpoint} не подходит роли {role}: окно контекста {window} токенов, "
            f"нужно не меньше {required.min_context_window} (запустите сервер с большим -c)"
        )


def prompt_hash(messages: list[ChatMessage], task_id: TaskId) -> str:
    """Хеш промпта без ID задачи: у повторного прогона того же сценария он тот же (replay)."""
    encoded = json.dumps([message.model_dump() for message in messages], ensure_ascii=False)
    normalized = re.sub(rf"\b{re.escape(task_id)}(?![0-9])", "task_*", encoded)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _with_schema(messages: list[ChatMessage], schema: dict[str, JsonValue]) -> list[ChatMessage]:
    system, *rest = messages
    described = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    rule = "Отвечай одним JSON-объектом по этой JSON Schema, без текста вокруг:"
    text = f"{system.content}\n\n{rule}\n{described}"
    return [ChatMessage(role="system", content=text), *rest]


def _repair(problems: list[str]) -> ChatMessage:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return ChatMessage(
        role="user",
        content=f"Ответ не принят:\n{listed}\nОтветь заново: один JSON-объект по схеме, без текста вокруг.",
    )


def _tokens(request: BackendRequest, response: BackendResponse) -> int:
    """Токены попытки: по данным сервера, а если он не сообщил — оценкой сверху."""
    prompt = response.prompt_tokens
    if prompt is None:
        prompt = sum(estimate_tokens(message.content) for message in request.messages)
    completion = response.completion_tokens
    if completion is None:
        completion = estimate_tokens(response.text)
    return prompt + completion


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


def extract_json(text: str) -> str:
    """JSON-объект из ответа: без обрамления ```json и текста вокруг первой «{» и последней «}»."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped


def _parse[T: BaseModel](text: str, output: StructuredOutput[T]) -> tuple[T | None, list[str]]:
    try:
        data = json.loads(extract_json(text))
    except json.JSONDecodeError as exc:
        reason = "ответ — не JSON" if text.strip() else "пустой ответ"
        return None, [f"{reason}: {exc.msg} (символ {exc.pos})"]
    try:
        value = output.model.model_validate(data)
    except ValidationError as exc:
        return None, [_problem(error) for error in exc.errors()[:10]]
    problems = output.check(value) if output.check is not None else []
    return (None, problems) if problems else (value, [])


def _problem(error: ErrorDetails) -> str:
    location = ".".join(str(part) for part in error["loc"]) or "ответ"
    return f"{location}: {error['msg']}"
