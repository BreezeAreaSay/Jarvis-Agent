"""Бэкенд модели через OpenAI-совместимый HTTP API на этом компьютере (ADR 0008, ADR 0023).

Основной рантайм — llama.cpp `llama-server`: `POST /v1/chat/completions`, structured output —
`response_format` с JSON Schema (сервер строит из неё грамматику). Особенности llama.cpp (тайминги,
окно контекста в `/props` и `/v1/models`) используются, когда сервер их отдаёт, и не нужны, когда нет.

Здесь и только здесь живут имя модели, сэмплирование, формат запроса и разбор ответа. Блок
«размышлений» отбрасывается до того, как ответ попадёт в ядро (ADR 0015). Переменные окружения
(прокси, сертификаты) не читаются: запрос уходит ровно на адрес из конфига.
"""

import math
import re
import time
from typing import Any

import httpx
from pydantic import JsonValue

from jarvis.domain.errors import ModelContextExceeded, ModelRequestRejected, ModelTimeout, ModelUnavailable
from jarvis.domain.models import BackendRequest, BackendResponse, BackendStatus, ModelInfo
from jarvis.domain.settings import EndpointSettings

CONNECT_TIMEOUT_S = 5.0
DESCRIBE_TIMEOUT_S = 10.0
_THINKING = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_ERROR_CHARS = 500


class OpenAICompatibleBackend:
    def __init__(
        self, endpoint: str, settings: EndpointSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport  # тесты подставляют записанные ответы сервера
        self._info = ModelInfo(endpoint=endpoint, model=settings.model, capabilities=settings.capabilities)

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def complete(self, request: BackendRequest) -> BackendResponse:
        body = self._body(request)
        started = time.perf_counter()
        async with self._client(self._settings.request_timeout_s) as client:
            response = await self._send(client, "POST", "chat/completions", json=body)
        latency_ms = round((time.perf_counter() - started) * 1000)
        data = _json(response)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelRequestRejected(
                f"{self._info.endpoint}: ответ сервера не похож на chat.completion",
                endpoint=self._info.endpoint,
            ) from None
        usage = _mapping(data.get("usage"))
        timings = _mapping(data.get("timings"))
        return BackendResponse(
            text=_THINKING.sub("", str(content), count=1),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            truncated=finish_reason == "length",
            prompt_tokens=_count(usage.get("prompt_tokens")),
            completion_tokens=_count(usage.get("completion_tokens")),
            latency_ms=latency_ms,
            prompt_ms=_count(timings.get("prompt_ms")),
        )

    async def describe(self) -> BackendStatus:
        async with self._client(DESCRIBE_TIMEOUT_S) as client:
            listed = _json(await self._send(client, "GET", "models"))
            props = await self._props(client)
        models: list[str] = []
        window: int | None = None
        for item in listed.get("data") or []:
            if not isinstance(item, dict) or "id" not in item:
                continue
            models.append(str(item["id"]))  # pyright: ignore[reportUnknownArgumentType]
            meta = item.get("meta")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if window is None and isinstance(meta, dict):
                window = _count(meta.get("n_ctx"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        settings = props.get("default_generation_settings")
        if isinstance(settings, dict):  # окно одного слота: столько получит один запрос
            window = _count(settings.get("n_ctx")) or window  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        build = props.get("build_info")
        return BackendStatus(
            models=models, context_window=window or None, server=str(build) if build is not None else None
        )

    def _body(self, request: BackendRequest) -> dict[str, JsonValue]:
        sampling = self._settings.sampling
        body: dict[str, JsonValue] = dict(self._settings.extra_body)
        body.update(
            {
                "model": self._settings.model,
                "messages": [message.model_dump() for message in request.messages],
                "max_tokens": request.max_tokens,
                "stream": False,
            }
        )
        for key, value in (
            ("temperature", sampling.temperature),
            ("top_p", sampling.top_p),
            ("seed", sampling.seed),
        ):
            if value is not None:
                body[key] = value
        if sampling.stop:
            body["stop"] = list[JsonValue](sampling.stop)
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": request.json_schema},
            }
        return body

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self._settings.base_url}/",
            timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s)),
            trust_env=False,  # ни прокси, ни сертификатов из окружения: только адрес из конфига
            follow_redirects=False,
            transport=self._transport,
        )

    async def _send(
        self, client: httpx.AsyncClient, method: str, path: str, **options: Any
    ) -> httpx.Response:
        endpoint = self._info.endpoint
        try:
            response = await client.request(method, path, **options)
        except httpx.TimeoutException:
            raise ModelTimeout(
                f"{endpoint}: сервер модели не ответил за {self._settings.request_timeout_s:g} с",
                endpoint=endpoint,
            ) from None
        except httpx.TransportError as exc:
            raise ModelUnavailable(
                f"{endpoint}: сервер модели недоступен ({self._settings.base_url}): {type(exc).__name__}",
                endpoint=endpoint,
            ) from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelRequestRejected(
                f"{endpoint}: запрос к серверу модели не удался: {type(exc).__name__}: {exc}",
                endpoint=endpoint,
            ) from None
        if response.status_code >= 500:
            raise ModelUnavailable(
                f"{endpoint}: сервер модели ответил {response.status_code}: {_error_text(response)}",
                endpoint=endpoint,
                status=response.status_code,
            )
        if response.status_code == 400 and _context_overflow(response):
            raise ModelContextExceeded(
                f"{endpoint}: промпт не помещается в окно контекста сервера: {_error_text(response)}",
                endpoint=endpoint,
            )
        if response.status_code >= 300:  # перенаправлений не бывает у локального сервера: не следуем
            raise ModelRequestRejected(
                f"{endpoint}: сервер отклонил запрос ({response.status_code}): {_error_text(response)}",
                endpoint=endpoint,
                status=response.status_code,
            )
        return response

    async def _props(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """`/props` есть только у llama.cpp; у других серверов — пусто."""
        root = re.sub(r"/v1$", "", self._settings.base_url)
        try:
            response = await client.get(f"{root}/props")
        except httpx.HTTPError:
            return {}
        if response.status_code != 200:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise ModelRequestRejected(f"сервер модели ответил не JSON: {response.text[:200]!r}") from None
    if not isinstance(data, dict):
        raise ModelRequestRejected("сервер модели ответил не JSON-объектом")
    return data  # pyright: ignore[reportUnknownVariableType]


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:_ERROR_CHARS]
    error = data.get("error") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(error, dict) and "message" in error:
        return str(error["message"])[:_ERROR_CHARS]  # pyright: ignore[reportUnknownArgumentType]
    return response.text[:_ERROR_CHARS]


def _context_overflow(response: httpx.Response) -> bool:
    """llama-server: type exceed_context_size_error; другие серверы — по тексту ошибки."""
    try:
        data = response.json()
    except ValueError:
        return False
    error = _mapping(data.get("error") if isinstance(data, dict) else None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    text = f"{error.get('type', '')} {error.get('message', '')}".lower()
    return "exceed_context" in text or "context size" in text or "context length" in text


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return round(value)
