"""Фальшивый Codex для тестов провайдера «ChatGPT (подписка)»: без процесса, сети и входа.

FakeRuntime повторяет интерфейс jarvis.ai.chatgpt.runtime.Runtime. Ход — сценарий: функция
(FakeTurnScript) → None, она выполняется в своём потоке, как настоящий Codex, и шлёт события; вызов
инструмента идёт так же, как у Codex: hooks.on_tool_call(params, respond) из «потока чтения», ответ — позже.
"""

import queue
import threading
from collections.abc import Callable
from typing import Any

from jarvis.ai.chatgpt.component import SdkInfo
from jarvis.ai.chatgpt.runtime import CodexEvent, RuntimeConfig, RuntimeGone, RuntimeHooks

WAIT_S = 10.0

MODELS = [
    {
        "id": "gpt-6.1-sol",
        "model": "gpt-6.1-sol",
        "isDefault": True,
        "hidden": False,
        "defaultReasoningEffort": "low",
    },
    {
        "id": "gpt-6-luna",
        "model": "gpt-6-luna",
        "isDefault": False,
        "hidden": False,
        "defaultReasoningEffort": "medium",
    },
    {"id": "gpt-secret", "model": "gpt-secret", "isDefault": False, "hidden": True},
]
ACCOUNT = {"type": "chatgpt", "email": "tony@stark.com", "planType": "plus"}


def installed_sdk() -> SdkInfo:
    return SdkInfo(True, version="0.163.0", binary="/fake/codex")


class FakeTurnScript:
    """То, что видит сценарий хода: события, вызовы инструментов, ожидание прерывания."""

    def __init__(
        self, runtime: "FakeRuntime", turn: "FakeTurn", items: list[dict[str, Any]], params: dict
    ) -> None:
        self.runtime = runtime
        self.turn = turn
        self.items = items
        self.params = params
        self._n = 0

    def emit(self, method: str, params: dict[str, Any]) -> None:
        params = {"threadId": self.turn.thread_id, "turnId": self.turn.turn_id, **params}
        self.turn.events.put(CodexEvent(method, params))

    def message(
        self, text: str, *, item: str = "", phase: str | None = "final_answer", stream: bool = True
    ) -> None:
        self._n += 1
        item = item or f"msg_{self._n}"
        self.emit("item/started", {"item": {"type": "agentMessage", "id": item, "text": "", "phase": None}})
        if stream:
            for i in range(0, len(text), 5):
                self.emit("item/agentMessage/delta", {"itemId": item, "delta": text[i : i + 5]})
        self.emit(
            "item/completed", {"item": {"type": "agentMessage", "id": item, "text": text, "phase": phase}}
        )

    def call(self, tool: str, arguments: dict[str, Any], *, call_id: str = "") -> dict[str, Any]:
        """Модель вызывает инструмент Jarvis; вернёт ответ Jarvis (как его получил бы Codex)."""
        self._n += 1
        call_id = call_id or f"call_{self._n}"
        params = {
            "threadId": self.turn.thread_id,
            "turnId": self.turn.turn_id,
            "callId": call_id,
            "namespace": None,
            "tool": tool,
            "arguments": arguments,
        }
        answer: queue.Queue[dict[str, Any]] = queue.Queue()
        item = {"type": "dynamicToolCall", "id": call_id, "tool": tool, "arguments": arguments}
        self.emit("item/started", {"item": {**item, "status": "inProgress"}})
        self.runtime.hooks.on_tool_call(params, answer.put)
        result = answer.get(timeout=WAIT_S)
        self.runtime.tool_results.append((tool, result))
        self.emit(
            "item/completed",
            {"item": {**item, "status": "completed", "contentItems": result["contentItems"]}},
        )
        return result

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        last = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        }
        self.emit("thread/tokenUsage/updated", {"tokenUsage": {"last": last, "total": last}})

    def error(self, message: str, info: Any = None, *, will_retry: bool = False) -> None:
        self.emit("error", {"error": {"message": message, "codexErrorInfo": info}, "willRetry": will_retry})

    def complete(self, status: str = "completed", error: dict[str, Any] | None = None) -> None:
        self.emit("turn/completed", {"turn": {"id": self.turn.turn_id, "status": status, "error": error}})

    def wait_interrupt(self, timeout: float = WAIT_S) -> bool:
        return self.turn.interrupted.wait(timeout)


Script = Callable[[FakeTurnScript], None]


class FakeTurn:
    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.events: queue.Queue[CodexEvent | BaseException] = queue.Queue()
        self.interrupted = threading.Event()

    def next_event(self) -> CodexEvent:
        try:
            item = self.events.get(timeout=WAIT_S)
        except queue.Empty:
            raise RuntimeGone("тест: событий нет") from None
        if isinstance(item, BaseException):
            raise item
        return item

    def interrupt(self) -> None:
        if not self.interrupted.is_set():
            self.interrupted.set()
            self.events.put(
                CodexEvent(
                    "turn/completed", {"turn": {"id": self.turn_id, "status": "interrupted", "error": None}}
                )
            )

    def close(self) -> None:
        return None


class FakeRuntime:
    """Codex по сценарию. Все экземпляры — в Codexes.instances (видно перезапуски)."""

    def __init__(self, config: RuntimeConfig, hooks: RuntimeHooks, world: "Codexes") -> None:
        self.config = config
        self.hooks = hooks
        self.world = world
        self.pid = 4242
        self.started = False
        self.closed = False
        self.tool_results: list[tuple[str, dict[str, Any]]] = []

    @property
    def alive(self) -> bool:
        return self.started and not self.closed

    def start(self) -> None:
        if self.world.start_error is not None:
            raise self.world.start_error
        self.started = True

    def close(self) -> None:
        self.closed = True

    def account(self) -> dict[str, Any] | None:
        return dict(self.world.account) if self.world.account else None

    def login_start(self, device: bool) -> dict[str, Any]:
        self.world.calls.append(("login_start", device))
        if device:
            return {
                "type": "chatgptDeviceCode",
                "loginId": "login-1",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "ABCD-1234",
            }
        return {
            "type": "chatgpt",
            "loginId": "login-1",
            "authUrl": "https://auth.openai.com/oauth/authorize?x=1",
        }

    def login_wait(self, login_id: str) -> dict[str, Any]:
        result = self.world.login_result.get(timeout=WAIT_S)
        if result.get("success"):
            self.world.account = dict(ACCOUNT)
        return result

    def login_cancel(self, login_id: str) -> None:
        self.world.calls.append(("login_cancel", login_id))

    def logout(self) -> None:
        self.world.calls.append(("logout", None))
        self.world.account = None

    def models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in MODELS]

    def thread_start(self, params: dict[str, Any]) -> dict[str, Any]:
        self.world.thread_n += 1
        thread_id = f"thr-{self.world.thread_n}"
        self.world.threads[thread_id] = params
        self.world.calls.append(("thread_start", thread_id))
        return {"id": thread_id, "path": None, "ephemeral": params.get("ephemeral")}

    def thread_resume(self, thread_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.world.calls.append(("thread_resume", thread_id))
        if thread_id not in self.world.threads or thread_id in self.world.lost:
            raise RuntimeError(f"no rollout found for thread id {thread_id}")
        return {"id": thread_id, "path": None}

    def thread_delete(self, thread_id: str) -> None:
        self.world.calls.append(("thread_delete", thread_id))
        self.world.threads.pop(thread_id, None)

    def thread_unsubscribe(self, thread_id: str) -> None:
        self.world.calls.append(("thread_unsubscribe", thread_id))

    def running_turns(self) -> dict[str, str]:
        running = {
            t["thread"]: t["turn"].turn_id for t in self.world.turns if not t["turn"].interrupted.is_set()
        }
        return {**running, **self.world.foreign}

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.world.calls.append(("interrupt", thread_id))
        self.world.foreign.pop(thread_id, None)

    def turn_start(self, thread_id: str, items: list[dict[str, Any]], params: dict[str, Any]) -> FakeTurn:
        if not self.world.scripts:
            raise AssertionError("сценарий ходов закончился")
        self.world.turn_n += 1
        turn = FakeTurn(thread_id, f"turn-{self.world.turn_n}")
        script = self.world.scripts.pop(_pick(self.world.scripts, params))
        self.world.turns.append({"thread": thread_id, "items": items, "params": params, "turn": turn})
        ctx = FakeTurnScript(self, turn, items, params)

        def run() -> None:
            try:
                script(ctx)
            except Exception as e:  # сценарий упал — пусть тест увидит
                self.world.failures.append(e)
                turn.events.put(RuntimeGone(f"сценарий упал: {e!r}"))

        threading.Thread(target=run, daemon=True).start()
        return turn


class Codexes:
    """«Мир» фальшивого Codex: сценарии ходов, вход, треды; фабрика для ChatGpt.runtime_factory."""

    def __init__(self, *scripts: Script, account: dict[str, Any] | None = None) -> None:
        self.scripts: list[Script] = list(scripts)
        self.account: dict[str, Any] | None = dict(ACCOUNT) if account is None else account
        self.instances: list[FakeRuntime] = []
        self.threads: dict[str, dict[str, Any]] = {}
        self.lost: set[str] = set()  # треды, которые thread/resume не найдёт
        self.foreign: dict[str, str] = {}  # идущие ходы чужих тредов (под-агенты Codex): тред → ход
        self.turns: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.failures: list[BaseException] = []
        self.login_result: queue.Queue[dict[str, Any]] = queue.Queue()
        self.start_error: BaseException | None = None
        self.thread_n = 0
        self.turn_n = 0

    def add(self, *scripts: Script) -> None:
        self.scripts.extend(scripts)

    def factory(self, config: RuntimeConfig, hooks: RuntimeHooks) -> FakeRuntime:
        runtime = FakeRuntime(config, hooks, self)
        self.instances.append(runtime)
        return runtime

    @property
    def runtime(self) -> FakeRuntime:
        return self.instances[-1]


def answer(text: str, *, tokens: tuple[int, int] = (120, 30)) -> Script:
    """Ход: модель отвечает текстом потоком."""

    def script(t: FakeTurnScript) -> None:
        t.message(text)
        t.usage(*tokens)
        t.complete()

    script.json_answer = text.lstrip().startswith("{")  # type: ignore[attr-defined]
    return script


def _pick(scripts: list[Script], params: dict[str, Any]) -> int:
    """Ходу со схемой (план агента) — первый ответ-JSON, остальным — первый другой сценарий: план агента
    составляется параллельно с его первым ходом, порядок ходов не задан."""
    wants_json = "outputSchema" in params
    for i, script in enumerate(scripts):
        if bool(getattr(script, "json_answer", False)) == wants_json:
            return i
    return 0


def text_of(result: dict[str, Any]) -> str:
    return "\n".join(
        i.get("text", "") for i in result.get("contentItems", []) if i.get("type") == "inputText"
    )
