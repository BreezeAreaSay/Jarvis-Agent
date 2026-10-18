"""Процесс Codex app-server через SDK openai-codex (0.163.0): запуск, запросы, ходы, события.

Один процесс на ядро; все вызовы — блокирующие (их зовут из потоков, см. jarvis.ai.chatgpt.backend).

Что здесь сверх SDK (поэтому версия SDK закреплена):
- окно консоли: SDK запускает codex.exe без CREATE_NO_WINDOW — имя subprocess в openai_codex.client
  подменяется прокладкой (как в прежнем Jarvis);
- инструменты Jarvis отдаются Codex как dynamicTools треда: модель вызывает их, Codex присылает клиенту запрос
  item/tool/call {threadId, turnId, callId, tool, arguments}. SDK отвечает на запросы сервера прямо в потоке
  чтения stdout — долгий инструмент (подтверждение человеком) остановил бы все ходы. Поток чтения заменён: на
  item/tool/call ответ уходит позже, из любого потока (respond); остальные запросы — сразу: разрешений Codex
  не получает (команды, правки файлов, ввод пользователя — отказ).
- события SDK (pydantic) переводятся в CodexEvent {method, params} — словарь в формате протокола (camelCase),
  как его описывает `codex app-server generate-json-schema`.

Если codex умирает (stdin/stdout закрыты), все ожидающие получают RuntimeGone. Если умирает ядро, codex
завершается сам: stdin закрыт — app-server выходит (проверено).
"""

import contextlib
import logging
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis import extras

log = logging.getLogger(__name__)


class RuntimeGone(Exception):
    """Процесс Codex завершился или ещё не запущен."""


@dataclass(frozen=True)
class CodexEvent:
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class RuntimeConfig:
    cwd: str
    env: dict[str, str]
    overrides: tuple[str, ...]
    codex_bin: str | None = None  # None — бинарь из пакета codex_cli_bin (SDK найдёт сам)


Respond = Callable[[dict[str, Any]], None]
ToolCallHandler = Callable[[dict[str, Any], Respond], None]
NotifyHandler = Callable[[CodexEvent], None]


@dataclass
class RuntimeHooks:
    on_tool_call: ToolCallHandler  # из потока чтения; ответить respond(result) можно позже из любого потока
    on_notification: NotifyHandler = field(default=lambda event: None)  # глобальные события (не ходов)


class TurnRun(Protocol):
    thread_id: str
    turn_id: str

    def next_event(self) -> CodexEvent:
        """Следующее событие хода (блокирует); после turn/completed — RuntimeGone."""
        ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


class Runtime(Protocol):
    pid: int | None

    @property
    def alive(self) -> bool: ...

    def start(self) -> None: ...

    def close(self) -> None: ...

    def account(self) -> dict[str, Any] | None:
        """{type: chatgpt, email, planType} | {type: apiKey} | None (входа нет)."""
        ...

    def login_start(self, device: bool) -> dict[str, Any]:
        """{type, loginId, authUrl} или {type: chatgptDeviceCode, loginId, verificationUrl, userCode}."""
        ...

    def login_wait(self, login_id: str) -> dict[str, Any]:
        """Блокирует до account/login/completed: {success, error?}."""
        ...

    def login_cancel(self, login_id: str) -> None: ...

    def logout(self) -> None: ...

    def models(self) -> list[dict[str, Any]]: ...

    def thread_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Новый тред; вернёт Thread {id, path, …}."""
        ...

    def thread_resume(self, thread_id: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def thread_delete(self, thread_id: str) -> None: ...

    def thread_unsubscribe(self, thread_id: str) -> None: ...

    def turn_start(self, thread_id: str, items: list[dict[str, Any]], params: dict[str, Any]) -> TurnRun: ...

    def running_turns(self) -> dict[str, str]:
        """Идущие ходы всех тредов процесса (и под-агентов, которых Jarvis не запускал): тред → ход."""
        ...

    def interrupt(self, thread_id: str, turn_id: str) -> None: ...


RuntimeFactory = Callable[[RuntimeConfig, RuntimeHooks], Runtime]


# --- ответы на запросы Codex к клиенту ---------------------------------------------------------------------

# схемы ответов — `codex app-server generate-json-schema` (0.163.0); всё — отказ
SERVER_DECISIONS: dict[str, dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
}


def server_reply(
    method: str, params: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Запрос Codex (кроме item/tool/call) → (result, error) JSON-RPC. Разрешений Codex не получает."""
    if method in SERVER_DECISIONS:
        log.warning("Codex попросил %s — отказ", method)
        return dict(SERVER_DECISIONS[method]), None
    if method == "currentTime/read":
        return {"currentTimeAt": int(time.time())}, None
    log.warning("Codex: неизвестный запрос %s — отказ", method)
    return None, {"code": -32601, "message": f"Jarvis не поддерживает {method}"}


def tool_failure(text: str) -> dict[str, Any]:
    return {"contentItems": [{"type": "inputText", "text": text}], "success": False}


# --- прокладка subprocess для openai_codex.client --------------------------------------------------------

_WINDOWS = sys.platform == "win32"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_shim_lock = threading.Lock()
_shim_hook: Callable[[subprocess.Popen[Any]], None] | None = None


class _ShimPopen(subprocess.Popen):  # type: ignore[type-arg]
    """Popen для openai_codex.client: на Windows без окна консоли; процесс отдаётся хуку (pid)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if _WINDOWS:
            kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | _CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)
        hook = _shim_hook
        if hook is not None:
            try:
                hook(self)
            except Exception:
                log.exception("хук процесса codex упал")


def _make_shim() -> types.ModuleType:
    shim = types.ModuleType("subprocess", "Прокладка subprocess для openai_codex.client (Jarvis)")

    def __getattr__(name: str) -> Any:  # PEP 562: всё прочее — из настоящего subprocess
        return getattr(subprocess, name)

    shim.__getattr__ = __getattr__  # type: ignore[method-assign]
    shim.Popen = _ShimPopen  # type: ignore[attr-defined]
    shim._jarvis_shim = True  # type: ignore[attr-defined]
    return shim


def install_subprocess_shim(hook: Callable[[subprocess.Popen[Any]], None] | None = None) -> None:
    """Подменить subprocess в openai_codex.client прокладкой (до запуска клиента); повтор — только хук."""
    global _shim_hook
    from openai_codex import client

    with _shim_lock:
        _shim_hook = hook
        if not getattr(client.subprocess, "_jarvis_shim", False):
            client.subprocess = _make_shim()  # type: ignore[attr-defined]


# --- клиент SDK с асинхронными ответами на item/tool/call ------------------------------------------------

_client_class: Any = None


def jarvis_client_class() -> Any:
    """Подкласс CodexClient (создаётся лениво: SDK импортируется только при запуске Codex)."""
    global _client_class
    if _client_class is not None:
        return _client_class
    from openai_codex.client import CodexClient

    class JarvisCodexClient(CodexClient):  # type: ignore[misc]
        def __init__(self, config: Any, on_request: Callable[[Any, dict[str, Any]], None]) -> None:
            super().__init__(config=config)
            self._jarvis_on_request = on_request
            self.running: dict[str, str] = {}  # тред → идущий ход (turn/started … turn/completed)

        def _track(self, method: str, params: Any) -> None:
            if not isinstance(params, dict) or method not in ("turn/started", "turn/completed"):
                return
            thread_id = params.get("threadId")
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            if not isinstance(thread_id, str):
                return
            if method == "turn/started" and isinstance(turn.get("id"), str):
                self.running[thread_id] = turn["id"]
            elif self.running.get(thread_id) == turn.get("id"):
                self.running.pop(thread_id, None)

        def respond(self, request_id: Any, result: Any = None, error: Any = None) -> None:
            payload: dict[str, Any] = {"id": request_id}
            if error is not None:
                payload["error"] = error
            else:
                payload["result"] = result if result is not None else {}
            with contextlib.suppress(Exception):
                self._write_message(payload)

        def _reader_loop(self) -> None:
            # как CodexClient._reader_loop (0.163.0), но запросы сервера — в _jarvis_on_request
            try:
                while True:
                    msg = self._read_message()
                    if "method" in msg and "id" in msg:
                        try:
                            self._jarvis_on_request(self, msg)
                        except Exception:
                            log.exception("запрос Codex %s не обработан", msg.get("method"))
                            self.respond(msg["id"], error={"code": -32603, "message": "внутренняя ошибка"})
                        continue
                    if "method" in msg and "id" not in msg:
                        method = msg["method"]
                        if isinstance(method, str):
                            self._track(method, msg.get("params"))
                            self._router.route_notification(
                                self._coerce_notification(method, msg.get("params"))
                            )
                        continue
                    self._router.route_response(msg)
            except BaseException as exc:
                self._router.fail_all(exc)

    _client_class = JarvisCodexClient
    return _client_class


def to_event(note: Any) -> CodexEvent:
    """Уведомление SDK → CodexEvent: params в формате протокола (camelCase)."""
    payload = note.payload
    if hasattr(payload, "model_dump"):
        params = payload.model_dump(by_alias=True, mode="json")
    else:
        params = dict(getattr(payload, "params", None) or {})
    return CodexEvent(str(note.method), params if isinstance(params, dict) else {})


def _gone(exc: BaseException) -> bool:
    return type(exc).__name__ in ("TransportClosedError",) or isinstance(exc, (BrokenPipeError, EOFError))


class _SdkTurn:
    def __init__(self, client: Any, thread_id: str, turn_id: str, subscription: Any) -> None:
        self._client = client
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._sub = subscription

    def next_event(self) -> CodexEvent:
        try:
            note = self._sub.next()
        except Exception as e:
            if _gone(e):
                raise RuntimeGone(str(e)) from e
            raise
        return to_event(note)

    def interrupt(self) -> None:
        self._client.turn_interrupt(self.thread_id, self.turn_id)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sub.close()


class CodexRuntime:
    """Настоящий Codex app-server (SDK openai-codex)."""

    def __init__(self, config: RuntimeConfig, hooks: RuntimeHooks) -> None:
        self.config = config
        self.hooks = hooks
        self.pid: int | None = None
        self._client: Any = None
        self._closed = False

    @property
    def alive(self) -> bool:
        client = self._client
        if client is None or self._closed:
            return False
        proc = getattr(client, "_proc", None)
        return proc is not None and proc.poll() is None

    def _require(self) -> Any:
        if self._client is None or self._closed:
            raise RuntimeGone("Codex не запущен")
        return self._client

    def start(self) -> None:
        extras.ensure_on_path()
        from openai_codex.client import CodexConfig

        def on_process(proc: subprocess.Popen[Any]) -> None:
            self.pid = proc.pid

        install_subprocess_shim(on_process)
        client = jarvis_client_class()(
            CodexConfig(
                codex_bin=self.config.codex_bin,
                cwd=self.config.cwd,
                env=dict(self.config.env),
                config_overrides=tuple(self.config.overrides),
            ),
            self._on_request,
        )
        try:
            client.start()
            client.initialize()
        except BaseException as e:
            tail = ""
            with contextlib.suppress(Exception):
                tail = client._stderr_tail()
            with contextlib.suppress(Exception):
                client.close()
            if tail and tail not in str(e):
                raise RuntimeGone(f"{e}\n{tail}") from e
            raise
        self._client = client
        threading.Thread(target=self._pump, args=(client,), name="chatgpt-events", daemon=True).start()

    def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    # --- запросы Codex к клиенту (поток чтения) ---
    def _on_request(self, client: Any, msg: dict[str, Any]) -> None:
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        request_id = msg.get("id")

        def respond(result: dict[str, Any]) -> None:
            client.respond(request_id, result=result)

        if method == "item/tool/call":
            try:
                self.hooks.on_tool_call(params, respond)
            except Exception:
                log.exception("инструмент для Codex не запустился")
                respond(tool_failure("Ошибка: инструмент не запустился"))
            return
        result, error = server_reply(method, params)
        client.respond(request_id, result=result, error=error)

    def _pump(self, client: Any) -> None:
        """Глобальные уведомления (не ходов): входы, предупреждения конфигурации, смерть процесса."""
        while True:
            try:
                note = client.next_notification()
            except Exception as e:
                if not self._closed:
                    log.warning("поток событий Codex закрыт: %s", type(e).__name__)
                    with contextlib.suppress(Exception):
                        self.hooks.on_notification(CodexEvent("jarvis/closed", {"error": str(e)[:500]}))
                return
            try:
                self.hooks.on_notification(to_event(note))
            except Exception:
                log.exception("событие Codex не разобрано")

    # --- запросы клиента ---
    def account(self) -> dict[str, Any] | None:
        response = self._require().account_read({"refreshToken": False})
        account = response.account
        if account is None:
            return None
        root = getattr(account, "root", account)
        return root.model_dump(by_alias=True, mode="json") if hasattr(root, "model_dump") else None

    def login_start(self, device: bool) -> dict[str, Any]:
        kind = "chatgptDeviceCode" if device else "chatgpt"
        response = self._require().account_login_start({"type": kind})
        root = getattr(response, "root", response)
        return root.model_dump(by_alias=True, mode="json")

    def login_wait(self, login_id: str) -> dict[str, Any]:
        try:
            done = self._require().wait_for_login_completed(login_id)
        except Exception as e:
            if _gone(e):
                raise RuntimeGone(str(e)) from e
            raise
        return done.model_dump(by_alias=True, mode="json")

    def login_cancel(self, login_id: str) -> None:
        self._require().account_login_cancel(login_id)

    def logout(self) -> None:
        self._require().account_logout()

    def models(self) -> list[dict[str, Any]]:
        response = self._require().model_list(include_hidden=False)
        return [m.model_dump(by_alias=True, mode="json") for m in response.data]

    def thread_start(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._require().thread_start(params)
        return response.thread.model_dump(by_alias=True, mode="json", exclude={"turns"})

    def thread_resume(self, thread_id: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._require().thread_resume(thread_id, params)
        return response.thread.model_dump(by_alias=True, mode="json", exclude={"turns"})

    def thread_delete(self, thread_id: str) -> None:
        self._require()._request_raw("thread/delete", {"threadId": thread_id})

    def thread_unsubscribe(self, thread_id: str) -> None:
        self._require()._request_raw("thread/unsubscribe", {"threadId": thread_id})

    def running_turns(self) -> dict[str, str]:
        client = self._client
        return dict(getattr(client, "running", {}) or {}) if client is not None else {}

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self._require().turn_interrupt(thread_id, turn_id)

    def turn_start(self, thread_id: str, items: list[dict[str, Any]], params: dict[str, Any]) -> TurnRun:
        client = self._require()
        try:
            started, subscription = client._start_turn(thread_id, items, params, for_handle=True)
        except Exception as e:
            if _gone(e):
                raise RuntimeGone(str(e)) from e
            raise
        return _SdkTurn(client, thread_id, started.turn.id, subscription)
