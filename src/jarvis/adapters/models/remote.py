"""Провайдер модели в интернете через OpenAI-совместимый API (ADR 0027).

Один адаптер для всех совместимых провайдеров: поставщик — это `base_url`, имя модели, ссылка на ключ
и особенности в конфиге (`json_object`, `extra_body`, признаки квоты). Ядро не знает брендов.

Граница безопасности:
- только https, перенаправлениям не следуем (ключ не уйдёт на другой адрес), прокси и сертификаты из
  окружения не читаются;
- таймаут запроса ограничен, ответ читается потоком не больше `max_response_bytes`;
- ключ живёт только в заголовке запроса: в сообщениях об ошибках, журналах и трассе его нет, тексты
  ошибок провайдера маскируются поиском секретов и обрезаются;
- ошибки приводятся к таксономии ядра: ModelAuthRequired, ModelRateLimited, ModelLimitExceeded,
  ModelUnavailable, ModelTimeout, ModelMisconfigured, ModelContextExceeded — по ним Model Gateway
  ведёт состояние провайдера и fallback.

Промпт сюда попадает только после решения границы приватности (Model Gateway, ADR 0028).
"""

import asyncio
import json
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import JsonValue

from jarvis.adapters.models.openai_compat import THINKING, count, mapping
from jarvis.domain.errors import (
    ModelAuthRequired,
    ModelContextExceeded,
    ModelError,
    ModelLimitExceeded,
    ModelMisconfigured,
    ModelRateLimited,
    ModelTimeout,
    ModelUnavailable,
)
from jarvis.domain.models import BackendRequest, BackendResponse, BackendStatus, ModelInfo
from jarvis.domain.providers import ProviderKind
from jarvis.domain.secrets import mask_secrets
from jarvis.domain.settings import RemoteEndpointSettings

CONNECT_TIMEOUT_S = 10.0
DESCRIBE_TIMEOUT_S = 15.0
ERROR_CHARS = 300
RETRY_AFTER_MAX_S = 3600
# Общие признаки исчерпанной квоты или баланса в ответе 429 (разные провайдеры кодируют по-разному);
# свои признаки провайдера — `quota_markers` в конфиге.
QUOTA_MARKERS = (
    "insufficient_quota", "quota", "billing", "balance", "credit", "payment", "exceeded your current",
    "余额", "欠费", "额度", "配额",
)  # fmt: skip
_CONTEXT_MARKERS = (
    "context_length",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
)
_MODEL_MARKERS = ("model_not_found", "model not found", "does not exist", "unknown model", "invalid model")


class RemoteOpenAICompatibleBackend:
    def __init__(
        self,
        endpoint: str,
        settings: RemoteEndpointSettings,
        api_key: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._key = api_key  # None — переменной из ссылки нет в окружении: каждый вызов — MISCONFIGURED
        self._transport = transport  # тесты подставляют поддельный сервер
        self._info = ModelInfo(
            endpoint=endpoint,
            model=settings.model,
            capabilities=settings.capabilities,
            kind=ProviderKind.REMOTE_MODEL_API,
        )

    def __repr__(self) -> str:  # ключ не попадает ни в repr, ни в отладочный вывод
        return f"RemoteOpenAICompatibleBackend({self._info.endpoint!r}, {self._settings.base_url!r})"

    @property
    def info(self) -> ModelInfo:
        return self._info

    @property
    def configured(self) -> bool:
        return self._key is not None

    async def complete(self, request: BackendRequest) -> BackendResponse:
        body = self._body(request)
        started = time.perf_counter()
        timeout_s = self._settings.request_timeout_s
        async with self._client(timeout_s) as client:
            data = await self._exchange(client, "POST", "chat/completions", body, timeout_s)
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelUnavailable(
                f"{self._info.endpoint}: ответ провайдера не похож на chat.completion",
                endpoint=self._info.endpoint,
            ) from None
        usage = mapping(data.get("usage"))
        details = mapping(usage.get("completion_tokens_details"))
        reasoning = count(details.get("reasoning_tokens"))
        if reasoning is None:
            reasoning = count(usage.get("reasoning_tokens"))
        return BackendResponse(
            text=THINKING.sub("", str(content), count=1),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            truncated=finish_reason == "length",
            prompt_tokens=count(usage.get("prompt_tokens")),
            completion_tokens=count(usage.get("completion_tokens")),
            reasoning_tokens=reasoning,
            latency_ms=latency_ms,
        )

    async def describe(self) -> BackendStatus:
        async with self._client(DESCRIBE_TIMEOUT_S) as client:
            listed = await self._exchange(client, "GET", "models", None, DESCRIBE_TIMEOUT_S)
        models = [
            str(item["id"])  # pyright: ignore[reportUnknownArgumentType]
            for item in listed.get("data") or []
            if isinstance(item, dict) and "id" in item
        ]
        return BackendStatus(models=models)

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
        elif self._settings.json_object:
            body["response_format"] = {"type": "json_object"}
        return body

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self._settings.base_url}/",
            timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s)),
            trust_env=False,  # ни прокси, ни сертификатов из окружения: только адрес из конфига
            follow_redirects=False,  # ключ не уходит по перенаправлению на другой адрес
            transport=self._transport,
        )

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None,
        deadline_s: float,
    ) -> dict[str, Any]:
        """Один обмен с общим сроком: таймауты httpx — на каждое чтение, и провайдер, отдающий ответ по
        байту, иначе не кончился бы никогда."""
        endpoint = self._info.endpoint
        if self._key is None:
            raise ModelMisconfigured(
                f"{endpoint}: ключа нет — переменная окружения {self._settings.api_key_env} не задана",
                endpoint=endpoint,
            )
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        try:
            async with (
                asyncio.timeout(deadline_s),
                client.stream(method, path, json=body, headers=headers) as response,
            ):
                status = response.status_code
                raw = await self._read(response)
                retry_after = response.headers.get("retry-after")
        except (httpx.TimeoutException, TimeoutError):
            raise ModelTimeout(
                f"{endpoint}: провайдер не ответил за {deadline_s:g} с",
                endpoint=endpoint,
            ) from None
        except httpx.TransportError as exc:
            raise ModelUnavailable(
                f"{endpoint}: провайдер недоступен: {type(exc).__name__}", endpoint=endpoint
            ) from None
        except httpx.HTTPError as exc:
            raise ModelMisconfigured(
                f"{endpoint}: запрос к провайдеру не удался: {type(exc).__name__}", endpoint=endpoint
            ) from None
        if 200 <= status < 300:
            return self._decode(raw)
        raise self._failure(status, raw, retry_after)

    async def _read(self, response: httpx.Response) -> bytes:
        limit = self._settings.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise ModelUnavailable(
                    f"{self._info.endpoint}: ответ провайдера больше {limit} байт — не читается",
                    endpoint=self._info.endpoint,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode(self, raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # не UTF-8, не JSON, вложенность без дна
            raise ModelUnavailable(
                f"{self._info.endpoint}: провайдер ответил не JSON (ответ оборван или искажён)",
                endpoint=self._info.endpoint,
            ) from None
        if not isinstance(data, dict):
            raise ModelUnavailable(
                f"{self._info.endpoint}: провайдер ответил не JSON-объектом", endpoint=self._info.endpoint
            )
        return data  # pyright: ignore[reportUnknownVariableType]

    def _failure(self, status: int, raw: bytes, retry_after: str | None) -> ModelError:
        endpoint = self._info.endpoint
        text = self._error_text(raw)
        lowered = text.casefold()
        said = f"{endpoint}: провайдер ответил {status}: {text}"
        if status in (401, 403):
            return ModelAuthRequired(said, endpoint=endpoint, status=status)
        if status == 402:
            return ModelLimitExceeded(said, endpoint=endpoint, status=status)
        if status == 429:
            markers = (*QUOTA_MARKERS, *(marker.casefold() for marker in self._settings.quota_markers))
            if any(marker in lowered for marker in markers):
                return ModelLimitExceeded(said, endpoint=endpoint, status=status)
            return ModelRateLimited(
                said, endpoint=endpoint, status=status, retry_after_s=_seconds(retry_after)
            )
        if status == 413 or (status == 400 and any(marker in lowered for marker in _CONTEXT_MARKERS)):
            return ModelContextExceeded(said, endpoint=endpoint, status=status)
        if status >= 500 or status == 408:
            return ModelUnavailable(
                said, endpoint=endpoint, status=status, retry_after_s=_seconds(retry_after)
            )
        if 300 <= status < 400:
            return ModelMisconfigured(
                f"{endpoint}: провайдер перенаправляет запрос ({status}) — перенаправлениям Jarvis не "
                "следует, проверьте base_url",
                endpoint=endpoint,
                status=status,
            )
        if status == 404 or any(marker in lowered for marker in _MODEL_MARKERS):
            return ModelMisconfigured(
                f"{said} (проверьте base_url и model)", endpoint=endpoint, status=status
            )
        return ModelMisconfigured(said, endpoint=endpoint, status=status)

    def _error_text(self, raw: bytes) -> str:
        """Текст ошибки провайдера для журнала: из поля error.message, без секретов, коротко."""
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            data = None
        error = mapping(data).get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            parts = [str(error.get(key)) for key in ("code", "type", "message") if error.get(key)]  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            text = " ".join(parts) or text
        elif isinstance(error, str):
            text = error
        masked = mask_secrets(" ".join(text.split()), self._key or "")
        return masked[:ERROR_CHARS] or "(без текста)"


def _seconds(value: str | None) -> int | None:
    """Retry-After: секунды или HTTP-дата; не больше часа."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        seconds = moment.timestamp() - time.time()
    if not math.isfinite(seconds) or seconds < 0:  # inf, NaN или прошлое
        return None
    return min(round(seconds), RETRY_AFTER_MAX_S)
