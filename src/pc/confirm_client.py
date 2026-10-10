"""Подтверждения действий pc.

В процессе Jarvis/CLI — обработчик set_confirm_handler() (окно или консоль). В MCP-процессе мозга — клиент
named pipe по адресу из JARVIS_CONFIRM_PIPE; сервер — ConfirmServer в процессе, который запустил Codex.
Нет обработчика, переменной, ключа, сервера или ответа за TIMEOUT_S — отказ.

Канал — multiprocessing.connection: взаимное рукопожатие HMAC по ключу <data>\\pipe.key, дальше только
send_bytes/recv_bytes с JSON (send/recv распаковывают pickle — запрещены). Без ключа нельзя ни отправить
запрос, ни подделать ответ «да».
"""

import contextlib
import itertools
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from multiprocessing import connection
from pathlib import Path
from typing import Any

from pc import settings
from pc.result import Caller

log = logging.getLogger("jarvis")

ConfirmHandler = Callable[[str, str, Caller], bool]

TIMEOUT_S = 60.0  # клиент ждёт ответа человека
HANDSHAKE_TIMEOUT_S = 10.0  # сервер ждёт рукопожатия и запроса от клиента
MAX_SUMMARY = 1000  # символов
MAX_DETAILS = 16000  # символов: текст для ввода, URL целиком, путь
MAX_MESSAGE = 128 * 1024  # байт JSON
KEY_BYTES = 32
PIPE_ENV = "JARVIS_CONFIRM_PIPE"

_PIPE_RX = re.compile(r"\\\\\.\\pipe\\jarvis-confirm-[\w-]{1,80}")

_handler: ConfirmHandler | None = None


def set_confirm_handler(handler: ConfirmHandler | None) -> None:
    """Обработчик подтверждений в этом процессе: (summary, details, caller) → да/нет."""
    global _handler
    _handler = handler


def confirm(summary: str, details: str, caller: Caller) -> bool:
    """Спросить человека. Любая ошибка — «нет»."""
    handler = _handler
    if handler is not None:
        try:
            return bool(handler(summary, details, caller))
        except Exception:
            log.exception("обработчик подтверждения упал")
            return False
    address = os.environ.get(PIPE_ENV, "").strip()
    if not address:
        return False
    return _ask_pipe(address, summary, details)


# --- ключ и адрес -------------------------------------------------------------------------------


def key_path() -> Path:
    return settings.data_dir() / "pipe.key"


def load_key(create: bool = False) -> bytes | None:
    """Ключ канала (KEY_BYTES байт). create=True — создать, если файла нет; существующий не перезаписать."""
    path = key_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return _create_key(path) if create else None
    except OSError as e:
        log.warning("ключ канала не прочитан: %s", e)
        return None
    if len(data) != KEY_BYTES:
        log.warning("ключ канала повреждён (%d байт): %s", len(data), path)
        return None
    return data


def _private_opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _create_key(path: Path) -> bytes | None:
    """Новый ключ: tmp, затем атомарно на место — только если файла ещё нет (гонку выигрывает первый)."""
    tmp = path.with_name(f"pipe.key.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "xb", opener=_private_opener) as f:
            f.write(os.urandom(KEY_BYTES))
        try:
            if sys.platform == "win32":
                os.rename(tmp, path)  # MoveFileEx без REPLACE_EXISTING: существующий ключ не трогает
            else:
                os.link(tmp, path)  # link не заменяет существующий файл, в отличие от os.replace
        except FileExistsError:
            pass  # другой процесс успел первым — читаем его ключ
    except OSError as e:
        log.warning("ключ канала не создан: %s", e)
        return None
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return load_key(create=False)


def _family() -> str:
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def _user() -> str:
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:40] or "user"


def pipe_address(pid: int | None = None) -> str:
    """\\\\.\\pipe\\jarvis-confirm-<пользователь>-<PID>; на Linux (тесты) — сокет AF_UNIX во временной папке.

    Пользователь — из USERNAME (USER), только буквы, цифры, «-» и «_».
    """
    pid = os.getpid() if pid is None else pid
    if sys.platform == "win32":
        return rf"\\.\pipe\jarvis-confirm-{_user()}-{pid}"
    return str(Path(tempfile.gettempdir()) / f"jarvis-confirm-{_user()}-{pid}-{secrets.token_hex(3)}.sock")


def _valid_address(address: str) -> bool:
    """Адрес из окружения — только наш канал: иначе Client() откроет произвольный путь (CreateFile)."""
    if sys.platform == "win32":
        return _PIPE_RX.fullmatch(address) is not None
    return os.path.isabs(address) and "\0" not in address


# --- клиент (MCP-процесс мозга) ---------------------------------------------------------------------


def _parse_answer(raw: bytes) -> bool:
    answer = json.loads(raw.decode("utf-8"))
    return isinstance(answer, dict) and answer.get("approved") is True


def _ask_pipe(address: str, summary: str, details: str) -> bool:
    if not _valid_address(address):
        log.warning("канал подтверждений: адрес в %s не похож на канал Jarvis", PIPE_ENV)
        return False
    if len(summary) > MAX_SUMMARY or len(details) > MAX_DETAILS:
        # обрезать нельзя: человек одобрил бы не то, что будет выполнено
        log.warning("канал подтверждений: запрос длиннее допустимого — отказ")
        return False
    key = load_key(create=False)
    if key is None:
        log.warning("канал подтверждений: нет ключа %s", key_path())
        return False
    payload = json.dumps({"summary": summary, "details": details}, ensure_ascii=False).encode("utf-8")
    timeout = TIMEOUT_S
    deadline = time.monotonic() + timeout
    answer: list[bool] = []
    done = threading.Event()

    def worker() -> None:
        try:
            with connection.Client(address, family=_family(), authkey=key) as conn:
                conn.send_bytes(payload)
                if conn.poll(max(0.0, deadline - time.monotonic())):
                    answer.append(_parse_answer(conn.recv_bytes(MAX_MESSAGE)))
        except Exception as e:
            log.warning("канал подтверждений: %s: %s", type(e).__name__, e)
        finally:
            done.set()

    # Client() и рукопожатие идут без таймаута — ждём в отдельном потоке не дольше timeout
    threading.Thread(target=worker, name="jarvis-confirm-client", daemon=True).start()
    done.wait(timeout)
    return bool(answer) and answer[0]


# --- сервер (процесс Jarvis/CLI, который запустил Codex) ---------------------------------------------


class _Timed:
    """Соединение с общим сроком на чтение. deliver/answer_challenge зовут только send_bytes и recv_bytes."""

    def __init__(self, conn: Any, timeout: float) -> None:
        self._conn = conn
        self._deadline = time.monotonic() + timeout

    def send_bytes(self, data: bytes) -> None:
        self._conn.send_bytes(data)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        if not self._conn.poll(max(0.0, self._deadline - time.monotonic())):
            raise TimeoutError("клиент молчит")
        return self._conn.recv_bytes(maxlength)


def _parse_request(raw: bytes) -> tuple[str, str] | None:
    """(summary, details) или None — запрос не по форме. Строка из pipe — недоверенные данные."""
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    summary, details = request.get("summary"), request.get("details", "")
    if not isinstance(summary, str) or not isinstance(details, str):
        return None
    if not summary.strip() or len(summary) > MAX_SUMMARY or len(details) > MAX_DETAILS:
        return None
    return summary, details


_server_seq = itertools.count(1)


class ConfirmServer:
    """Сервер подтверждений для MCP-процесса мозга.

    Каждое соединение — в своём daemon-потоке: рукопожатие HMAC (с таймаутом), запрос JSON,
    callback(summary, details, "brain") в этом же потоке, ответ {"approved": bool}. Запросы из канала всегда
    от мозга, что бы ни пришло в JSON. Исключение callback или запрос не по форме — «нет».
    """

    def __init__(self, callback: ConfirmHandler) -> None:
        self._callback = callback
        base = pipe_address()
        seq = next(_server_seq)
        # второй сервер в том же процессе — свой канал, первый не ломается
        self._address = base if seq == 1 or sys.platform != "win32" else f"{base}-{seq}"
        self._key = b""
        self._listener: Any = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._lock = threading.Lock()

    @property
    def address(self) -> str:
        """Адрес канала для JARVIS_CONFIRM_PIPE MCP-процесса."""
        return self._address

    def start(self) -> None:
        """Открыть канал и запустить поток слушателя. Нет ключа — RuntimeError."""
        with self._lock:
            if self._thread is not None or self._closing.is_set():
                return
            key = load_key(create=True)
            if key is None:
                raise RuntimeError(f"Нет ключа канала подтверждений: {key_path()}")
            # без authkey: рукопожатие внутри accept() идёт без таймаута — делаем его в потоке соединения
            listener = connection.Listener(self._address, family=_family(), backlog=16)
            if sys.platform != "win32":
                # не критично: без ключа рукопожатие всё равно не пройдёт
                with contextlib.suppress(OSError):
                    os.chmod(self._address, 0o600)
            self._key, self._listener = key, listener
            self._thread = threading.Thread(
                target=self._serve, args=(listener,), name="jarvis-confirm-server", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        """Остановить слушателя. Соединения, которые уже обрабатываются, доживают в своих потоках."""
        with self._lock:
            if self._closing.is_set():
                return
            self._closing.set()
            thread, listener = self._thread, self._listener
        if thread is None:
            return
        if thread.is_alive():
            # listener.close() из другого потока не будит accept(): будим одним подключением к себе.
            # Подключение — в своём потоке: если слушатель успел выйти до accept(), Client() ждал бы
            # рукопожатия вечно; listener.close() ниже обрывает его.
            threading.Thread(target=self._wake, name="jarvis-confirm-wake", daemon=True).start()
            thread.join(timeout=5)
        listener.close()

    def _wake(self) -> None:
        # сервер сразу закрывает это соединение — рукопожатие и не должно пройти
        with contextlib.suppress(Exception):
            connection.Client(self._address, family=_family(), authkey=self._key).close()

    def _serve(self, listener: Any) -> None:
        while not self._closing.is_set():
            try:
                conn = listener.accept()
            except Exception as e:
                if self._closing.is_set():
                    break
                log.warning("канал подтверждений: accept: %s", e)
                time.sleep(0.1)
                continue
            if self._closing.is_set():
                conn.close()
                break
            threading.Thread(
                target=self._handle, args=(conn,), name="jarvis-confirm-conn", daemon=True
            ).start()

    def _handle(self, conn: Any) -> None:
        try:
            with conn:
                timed = _Timed(conn, HANDSHAKE_TIMEOUT_S)
                # тот же порядок, что в Listener.accept(): сначала проверяем клиента, потом он нас
                connection.deliver_challenge(timed, self._key)
                connection.answer_challenge(timed, self._key)
                request = _parse_request(timed.recv_bytes(MAX_MESSAGE))
                approved = self._decide(request)
                conn.send_bytes(json.dumps({"approved": approved}).encode("utf-8"))
        except Exception as e:
            log.warning("канал подтверждений: соединение закрыто (%s: %s)", type(e).__name__, e)

    def _decide(self, request: tuple[str, str] | None) -> bool:
        if request is None:
            log.warning("канал подтверждений: запрос не по форме — отказ")
            return False
        summary, details = request
        try:
            return bool(self._callback(summary, details, "brain"))
        except Exception:
            log.exception("обработчик подтверждения из канала упал")
            return False
