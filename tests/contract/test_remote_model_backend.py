"""Контракт удалённого провайдера модели (ADR 0027): поддельный OpenAI-совместимый сервер через
`httpx.MockTransport`. Настоящих провайдеров и ключей в тестах нет.

Проверяется то, на что опирается ядро: ответ и usage, structured output, таксономия ошибок (по ней Model
Gateway ведёт состояние и fallback), отказ следовать перенаправлению, предел ответа, отмена — и что ключ
не попадает ни в сообщения об ошибках, ни в repr.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from jarvis.adapters.models import RemoteOpenAICompatibleBackend
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
from jarvis.domain.models import BackendRequest, ChatMessage
from jarvis.domain.providers import ProviderKind
from jarvis.domain.settings import RemoteEndpointSettings

pytestmark = pytest.mark.anyio

KEY = "sk-test-0000000000000000000000000000"  # поддельный ключ: проверяем, что он нигде не всплывает
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
REQUEST = BackendRequest(
    messages=[ChatMessage(role="system", content="JSON."), ChatMessage(role="user", content="Ок?")],
    json_schema=SCHEMA,
    max_tokens=64,
)
Handler = Callable[[httpx.Request], httpx.Response]


def completion(
    content: str = '{"ok": true}', finish: str = "stop", usage: dict[str, Any] | None = None
) -> Any:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 12, "completion_tokens": 4},
    }


def settings(**changes: Any) -> RemoteEndpointSettings:
    data: dict[str, Any] = {
        "base_url": "https://provider.example/v1",
        "model": "smart-model",
        "api_key": "env:JARVIS_TEST_KEY",
        "capabilities": {"structured_output": True, "context_window": 32768},
    }
    return RemoteEndpointSettings.model_validate({**data, **changes})


class Provider:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def backend(
    handler: Handler, key: str | None = KEY, **changes: Any
) -> tuple[RemoteOpenAICompatibleBackend, Provider]:
    provider = Provider(handler)
    return (
        RemoteOpenAICompatibleBackend(
            "cloud_a", settings(**changes), key, transport=httpx.MockTransport(provider)
        ),
        provider,
    )


def error(status: int, message: str = "", code: str = "", **headers: str) -> Handler:
    body = {"error": {"message": message, "code": code}}
    return lambda _: httpx.Response(status, json=body, headers=headers)


async def test_a_normal_response_with_usage() -> None:
    usage = {
        "prompt_tokens": 30,
        "completion_tokens": 9,
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    model, provider = backend(lambda _: httpx.Response(200, json=completion(usage=usage)))
    response = await model.complete(REQUEST)
    assert response.text == '{"ok": true}'
    assert (response.prompt_tokens, response.completion_tokens, response.reasoning_tokens) == (30, 9, 5)
    assert response.truncated is False
    assert model.info.kind is ProviderKind.REMOTE_MODEL_API
    assert model.info.remote
    (sent,) = provider.requests
    assert sent.url == httpx.URL("https://provider.example/v1/chat/completions")
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["model"] == "smart-model"
    assert body["max_tokens"] == 64
    assert body["stream"] is False


async def test_usage_that_is_not_reported_is_not_invented() -> None:
    model, _ = backend(lambda _: httpx.Response(200, json=completion(usage={})))
    response = await model.complete(REQUEST)
    assert (response.prompt_tokens, response.completion_tokens, response.reasoning_tokens) == (
        None,
        None,
        None,
    )


async def test_structured_output_uses_the_schema_or_a_json_object() -> None:
    model, provider = backend(lambda _: httpx.Response(200, json=completion()))
    await model.complete(REQUEST)
    assert json.loads(provider.requests[0].content)["response_format"]["type"] == "json_schema"

    caps = {"structured_output": False, "context_window": 32768}
    model, provider = backend(
        lambda _: httpx.Response(200, json=completion()), json_object=True, capabilities=caps
    )
    await model.complete(REQUEST.model_copy(update={"json_schema": None}))
    assert json.loads(provider.requests[0].content)["response_format"] == {"type": "json_object"}


async def test_thinking_block_is_dropped_and_a_truncated_answer_is_flagged() -> None:
    model, _ = backend(lambda _: httpx.Response(200, json=completion('<think>долго</think>{"ok":', "length")))
    response = await model.complete(REQUEST)
    assert response.text == '{"ok":'
    assert response.truncated is True


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (error(401, "invalid api key"), ModelAuthRequired),
        (error(403, "forbidden"), ModelAuthRequired),
        (error(402, "payment required"), ModelLimitExceeded),
        (error(429, "Rate limit reached", "rate_limit_exceeded"), ModelRateLimited),
        (error(429, "You exceeded your current quota", "insufficient_quota"), ModelLimitExceeded),
        (error(429, "账户余额不足"), ModelLimitExceeded),
        (error(500, "internal"), ModelUnavailable),
        (error(502, "bad gateway"), ModelUnavailable),
        (error(503, "overloaded"), ModelUnavailable),
        (error(404, "not found"), ModelMisconfigured),
        (error(400, "The model `smart-x` does not exist"), ModelMisconfigured),
        (error(400, "This model's maximum context length is 32768 tokens"), ModelContextExceeded),
        (error(413, "payload too large"), ModelContextExceeded),
        (error(422, "unprocessable"), ModelMisconfigured),
    ],
)
async def test_http_errors_map_to_the_core_taxonomy(handler: Handler, expected: type[ModelError]) -> None:
    model, _ = backend(handler)
    with pytest.raises(expected) as caught:
        await model.complete(REQUEST)
    assert KEY not in caught.value.message
    assert caught.value.details.get("endpoint") == "cloud_a"


async def test_rate_limit_carries_retry_after() -> None:
    model, _ = backend(error(429, "slow down", **{"Retry-After": "120"}))
    with pytest.raises(ModelRateLimited) as caught:
        await model.complete(REQUEST)
    assert caught.value.details["retry_after_s"] == 120


@pytest.mark.parametrize("value", ["inf", "1e999", "nan", "-5", "soon"])
async def test_a_strange_retry_after_is_ignored(value: str) -> None:
    model, _ = backend(error(429, "slow down", **{"Retry-After": value}))
    with pytest.raises(ModelRateLimited) as caught:
        await model.complete(REQUEST)
    assert caught.value.details["retry_after_s"] is None


@pytest.mark.parametrize("status", [200, 500])
async def test_deeply_nested_json_is_a_provider_failure(status: int) -> None:
    model, _ = backend(lambda _: httpx.Response(status, content=b'{"error":' + b"[" * 200_000))
    with pytest.raises(ModelUnavailable):
        await model.complete(REQUEST)


async def test_a_provider_that_trickles_bytes_hits_the_overall_deadline() -> None:
    async def trickle() -> AsyncIterator[bytes]:
        for _ in range(100):
            await asyncio.sleep(0.05)  # каждый кусок — в пределах таймаута чтения, весь ответ — нет
            yield b" "

    model, _ = backend(lambda _: httpx.Response(200, content=trickle()), request_timeout_s=0.3)
    started = asyncio.get_running_loop().time()
    with pytest.raises(ModelTimeout):
        await model.complete(REQUEST)
    assert asyncio.get_running_loop().time() - started < 2


async def test_provider_specific_quota_markers_come_from_config() -> None:
    model, _ = backend(error(429, "plan credits used up: tier exhausted"), quota_markers=["tier exhausted"])
    with pytest.raises(ModelLimitExceeded):
        await model.complete(REQUEST)


async def test_timeout_and_connection_failure() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelTimeout):
        await backend(timeout)[0].complete(REQUEST)
    with pytest.raises(ModelUnavailable):
        await backend(refused)[0].complete(REQUEST)


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b'{"choices": [{"message": {"content": "ok"',
        json.dumps({"no": "choices"}).encode(),
        b"[]",
    ],
)
async def test_invalid_or_truncated_json_is_a_provider_failure(body: bytes) -> None:
    model, _ = backend(lambda _: httpx.Response(200, content=body))
    with pytest.raises(ModelUnavailable):
        await model.complete(REQUEST)


async def test_redirects_are_not_followed() -> None:
    model, provider = backend(
        lambda _: httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    )
    with pytest.raises(ModelMisconfigured, match="перенаправл"):
        await model.complete(REQUEST)
    assert len(provider.requests) == 1  # ключ не ушёл на другой адрес


async def test_a_response_above_the_limit_is_not_read() -> None:
    huge = completion("x" * 20_000)
    model, _ = backend(lambda _: httpx.Response(200, json=huge), max_response_bytes=8192)
    with pytest.raises(ModelUnavailable, match="больше 8192 байт"):
        await model.complete(REQUEST)


async def test_a_missing_key_is_misconfigured_without_network() -> None:
    model, provider = backend(lambda _: httpx.Response(200, json=completion()), key=None)
    with pytest.raises(ModelMisconfigured, match="JARVIS_TEST_KEY не задана"):
        await model.complete(REQUEST)
    assert provider.requests == []
    assert model.configured is False


async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("не должно дойти")

    model = RemoteOpenAICompatibleBackend("cloud_a", settings(), KEY, transport=httpx.MockTransport(hang))
    task = asyncio.create_task(model.complete(REQUEST))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_key_never_appears_in_errors_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    # Провайдер, который в тексте ошибки повторяет ключ и заголовок авторизации.
    leaky = error(401, f"Incorrect API key provided: {KEY}. Authorization: Bearer {KEY}")
    model, _ = backend(leaky)
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelAuthRequired) as caught:
        await model.complete(REQUEST)
    assert KEY not in caught.value.message
    assert KEY not in json.dumps(caught.value.details)
    assert KEY not in repr(model)
    assert KEY not in caplog.text


async def test_describe_lists_models() -> None:
    model, provider = backend(
        lambda _: httpx.Response(200, json={"data": [{"id": "smart-model"}, {"id": "x"}]})
    )
    status = await model.describe()
    assert status.models == ["smart-model", "x"]
    assert provider.requests[0].url.path == "/v1/models"


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/v1",
        "file:///etc/passwd",
        "https://127.0.0.1/v1",
        "https://localhost:8443/v1",
        "ftp://x/v1",
        # этот компьютер в другой записи
        "https://LOCALHOST/v1",
        "https://localhost./v1",
        "https://api.localhost/v1",
        "https://0.0.0.0/v1",
        "https://[::]/v1",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://127.1/v1",
        "https://2130706433/v1",
        "https://0x7f000001/v1",
    ],
)
def test_only_https_to_another_computer(url: str) -> None:
    with pytest.raises(ValueError, match=r"https|этом компьютере"):
        settings(base_url=url)


@pytest.mark.parametrize(
    "url", ["https://api.provider.example/v1", "https://203.0.113.7/v1", "https://[2001:db8::1]:8443/v1"]
)
def test_another_computer_by_name_or_address_is_accepted(url: str) -> None:
    assert settings(base_url=url).base_url == url
