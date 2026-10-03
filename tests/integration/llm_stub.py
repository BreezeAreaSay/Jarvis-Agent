"""Заглушка OpenAI-совместимого сервера модели (как llama-server) для интеграционных тестов CLI.

Настоящий HTTP на 127.0.0.1: адаптер, httpx и разбор ответа работают как с llama-server. Реплики
чата отдаются по очереди; реплика — текст или функция тела запроса (для проб `jarvis model check`).
"""

import json
import threading
from collections import deque
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

Reply = str | Callable[[dict[str, Any]], str]


class LlmStub:
    def __init__(self, replies: Sequence[Reply] = (), *, n_ctx: int = 16384) -> None:
        self.replies: deque[Reply] = deque(replies)
        self.requests: list[dict[str, Any]] = []
        self.n_ctx = n_ctx
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._send(
                        200, {"object": "list", "data": [{"id": "stub", "meta": {"n_ctx": stub.n_ctx}}]}
                    )
                elif self.path == "/props":
                    self._send(
                        200, {"default_generation_settings": {"n_ctx": stub.n_ctx}, "build_info": "stub"}
                    )
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                if self.path != "/v1/chat/completions" or not stub.replies:
                    self._send(500, {"error": {"message": "заглушке нечего ответить"}})
                    return
                reply = stub.replies.popleft()
                content = reply(body) if callable(reply) else reply
                self._send(
                    200,
                    {
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                        "timings": {"prompt_ms": 5.0, "predicted_ms": 10.0},
                    },
                )

            def _send(self, status: int, data: dict[str, Any]) -> None:
                encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def decision(action: dict[str, Any], text: str = "действую") -> str:
    return json.dumps({"decision": text, "action": action}, ensure_ascii=False)


def call(tool: str, **arguments: Any) -> str:
    return decision({"type": "tool", "tool": tool, "arguments": arguments})


def finish(answer: str, *evidence: str) -> str:
    return decision({"type": "finish", "answer": answer, "evidence": list(evidence)})


def config_toml(base_url: str, *, context_window: int = 16384, structured: bool = True) -> str:
    return (
        "schema_version = 1\n"
        "[models.endpoints.main]\n"
        f'base_url = "{base_url}"\n'
        'model = "stub"\n'
        "[models.endpoints.main.capabilities]\n"
        f"structured_output = {'true' if structured else 'false'}\n"
        f"context_window = {context_window}\n"
        "[models.roles]\n"
        'executor = "main"\n'
    )
