"""Контракт бэкенда модели: scripted-модель и OpenAI-совместимый адаптер на записанных ответах
llama.cpp server (tests/fixtures/llama_server) ведут себя одинаково для ядра."""

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.adapters.models import OpenAICompatibleBackend
from jarvis.domain.errors import ModelContextExceeded, ModelRequestRejected, ModelTimeout, ModelUnavailable
from jarvis.domain.models import BackendRequest, ChatMessage, ModelCapabilities
from jarvis.domain.settings import EndpointSettings
from jarvis.evals.models import ModelReply, ScriptedModel
from jarvis.ports.models import ModelBackend

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llama_server"
SCHEMA = {
    "type": "object",
    "properties": {"color": {"enum": ["red", "green", "blue"]}},
    "required": ["color"],
    "additionalProperties": False,
}
REQUEST = BackendRequest(
    messages=[
        ChatMessage(role="system", content="Ответь JSON."),
        ChatMessage(role="user", content="Выбери цвет."),
    ],
    json_schema=SCHEMA,
    max_tokens=64,
)
Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def settings(**changes: Any) -> EndpointSettings:
    data: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "qwen-local",
        "capabilities": {"structured_output": True, "context_window": 8192},
    }
    return EndpointSettings.model_validate({**data, **changes})


class Server:
    """Записанный llama-server: отвечает фикстурами и запоминает запросы."""

    def __init__(self, routes: dict[tuple[str, str], Handler] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Handler] = {
            ("POST", "/v1/chat/completions"): lambda _: httpx.Response(
                200, json=fixture("chat_json_schema.json")
            ),
            ("GET", "/v1/models"): lambda _: httpx.Response(200, json=fixture("models.json")),
            ("GET", "/props"): lambda _: httpx.Response(200, json=fixture("props_trimmed.json")),
            **(routes or {}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        return handler(request) if handler else httpx.Response(404, json={"error": {"message": "not found"}})

    def backend(self, **changes: Any) -> OpenAICompatibleBackend:
        return OpenAICompatibleBackend("main", settings(**changes), transport=httpx.MockTransport(self))

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


@pytest.fixture(params=["scripted", "llama_server"])
def backend(request: pytest.FixtureRequest) -> ModelBackend:
    if request.param == "scripted":
        reply = ModelReply.model_validate({"json": {"color": "green"}})
        return ScriptedModel(
            [reply], capabilities=ModelCapabilities(structured_output=True, context_window=8192)
        )
    return Server().backend()


# --- общий контракт


async def test_completion_returns_text_tokens_and_timing(backend: ModelBackend) -> None:
    response = await backend.complete(REQUEST)
    assert json.loads(response.text) == {"color": "green"}
    assert response.finish_reason == "stop"
    assert response.prompt_tokens is not None
    assert response.completion_tokens is not None
    assert response.latency_ms >= 0


async def test_info_and_description(backend: ModelBackend) -> None:
    assert backend.info.capabilities.context_window == 8192
    status = await backend.describe()
    assert status.models
    assert status.context_window == 8192


# --- адаптер llama.cpp / OpenAI-совместимый


async def test_request_carries_messages_sampling_and_schema() -> None:
    server = Server()
    backend = server.backend(
        sampling={"temperature": 0.2, "seed": 7, "stop": ["</s>"]}, extra_body={"cache_prompt": True}
    )
    await backend.complete(REQUEST)
    sent = server.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "http://127.0.0.1:8080/v1/chat/completions"
    body = server.body()
    assert body["model"] == "qwen-local"
    assert body["messages"] == [message.model_dump() for message in REQUEST.messages]
    assert body["max_tokens"] == 64
    assert body["stream"] is False
    assert (body["temperature"], body["seed"], body["stop"]) == (0.2, 7, ["</s>"])
    assert "top_p" not in body  # не задано — умолчание сервера
    assert body["cache_prompt"] is True
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }


async def test_no_schema_means_no_response_format_and_extra_body_cannot_override_the_prompt() -> None:
    server = Server(
        {("POST", "/v1/chat/completions"): lambda _: httpx.Response(200, json=fixture("chat_plain.json"))}
    )
    backend = server.backend(
        extra_body={"messages": [], "max_tokens": 99999, "chat_template_kwargs": {"x": 1}}
    )
    response = await backend.complete(REQUEST.model_copy(update={"json_schema": None}))
    body = server.body()
    assert "response_format" not in body
    assert body["messages"] == [message.model_dump() for message in REQUEST.messages]
    assert body["max_tokens"] == 64
    assert body["chat_template_kwargs"] == {"x": 1}
    assert response.finish_reason == "length"
    assert response.prompt_ms is not None


async def test_thinking_block_is_dropped() -> None:
    answer = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '<think>\nскрытые рассуждения\n</think>\n{"color": "red"}',
                },
                "finish_reason": "stop",
            }
        ]
    }
    server = Server({("POST", "/v1/chat/completions"): lambda _: httpx.Response(200, json=answer)})
    response = await server.backend().complete(REQUEST)
    assert response.text == '{"color": "red"}'
    assert response.prompt_tokens is None  # сервер не сообщил usage


async def test_context_overflow_is_a_rejected_request_with_the_server_message() -> None:
    server = Server(
        {("POST", "/v1/chat/completions"): lambda _: httpx.Response(400, json=fixture("error_context.json"))}
    )
    with pytest.raises(ModelRequestRejected, match="exceeds the available context size"):
        await server.backend().complete(REQUEST)


@pytest.mark.parametrize(
    ("respond", "error"),
    [
        (lambda _: httpx.Response(503, json={"error": {"message": "Loading model"}}), ModelUnavailable),
        (lambda _: httpx.Response(200, text="<html>"), ModelRequestRejected),
        (lambda _: httpx.Response(200, json={"choices": []}), ModelRequestRejected),
    ],
)
async def test_server_failures_map_to_model_errors(respond: Handler, error: type[Exception]) -> None:
    server = Server({("POST", "/v1/chat/completions"): respond})
    with pytest.raises(error):
        await server.backend().complete(REQUEST)


@pytest.mark.parametrize(
    ("exception", "error"),
    [(httpx.ConnectError("refused"), ModelUnavailable), (httpx.ReadTimeout("slow"), ModelTimeout)],
)
async def test_transport_failures_map_to_model_errors(exception: Exception, error: type[Exception]) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise exception

    server = Server({("POST", "/v1/chat/completions"): fail})
    with pytest.raises(error):
        await server.backend().complete(REQUEST)


async def test_nothing_listening_is_unavailable() -> None:
    with socket.socket() as probe:  # свободный порт: сервера на нём нет
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    backend = OpenAICompatibleBackend("main", settings(base_url=f"http://127.0.0.1:{port}/v1"))
    with pytest.raises(ModelUnavailable, match="недоступен"):
        await backend.complete(REQUEST)


async def test_environment_proxies_are_ignored() -> None:
    client = OpenAICompatibleBackend("main", settings())._client(1.0)  # pyright: ignore[reportPrivateUsage]
    async with client:
        assert client.trust_env is False


async def test_describe_reads_llama_cpp_extras_when_present() -> None:
    status = await Server().backend().describe()
    assert status.models == ["tiny-qwen2.gguf"]
    assert status.context_window == 8192
    assert status.server == "b1-b92761a"


async def test_describe_works_without_llama_cpp_extras() -> None:
    plain = {"object": "list", "data": [{"id": "some-model", "object": "model"}]}
    server = Server(
        {
            ("GET", "/v1/models"): lambda _: httpx.Response(200, json=plain),
            ("GET", "/props"): lambda _: httpx.Response(404),
        }
    )
    status = await server.backend().describe()
    assert status.models == ["some-model"]
    assert status.context_window is None
    assert status.server is None


async def test_context_overflow_is_recognised() -> None:
    server = Server(
        {("POST", "/v1/chat/completions"): lambda _: httpx.Response(400, json=fixture("error_context.json"))}
    )
    with pytest.raises(ModelContextExceeded):
        await server.backend().complete(REQUEST)


async def test_redirects_are_not_followed() -> None:
    def redirect(_: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://example.com/v1/chat/completions"})

    server = Server({("POST", "/v1/chat/completions"): redirect})
    with pytest.raises(ModelRequestRejected, match="307"):
        await server.backend().complete(REQUEST)
    assert len(server.requests) == 1


@pytest.mark.parametrize("usage", ['"n/a"', '{"prompt_tokens": 1e999, "completion_tokens": -5}', "null"])
async def test_odd_usage_fields_are_ignored(usage: str) -> None:
    body = (
        '{"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}], '
        f'"usage": {usage}, "timings": []}}'
    )
    server = Server({("POST", "/v1/chat/completions"): lambda _: httpx.Response(200, content=body.encode())})
    response = await server.backend().complete(REQUEST)
    assert response.prompt_tokens is None
    assert response.completion_tokens is None
    assert response.truncated
