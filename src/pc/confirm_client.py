"""Подтверждения действий pc.

В процессе Jarvis/CLI — обработчик set_confirm_handler() (окно или консоль). В MCP-процессе мозга — клиент
named pipe по адресу из JARVIS_CONFIRM_PIPE (сервер — ConfirmServer в процессе, который запустил Codex).
Нет обработчика, переменной, сервера или ответа за 60 с — отказ.
"""

import json
import logging
import os
import secrets
import sys
import threading
from collections.abc import Callable
from multiprocessing import connection
from pathlib import Path

from pc import settings
from pc.result import Caller

log = logging.getLogger("jarvis")

ConfirmHandler = Callable[[str, str, Caller], bool]

TIMEOUT_S = 60.0
MAX_MESSAGE = 64 * 1024
KEY_BYTES = 32
PIPE_ENV = "JARVIS_CONFIRM_PIPE"

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
    return _ask_pipe(address, summary, details, caller)


# --- канал --------------------------------------------------------------------------------------


def key_path() -> Path:
    return settings.data_dir() / "pipe.key"


def load_key(create: bool = False) -> bytes | None:
    """Ключ канала: создаётся один раз (tmp + os.replace), потом только читается, не перезаписывается."""
    path = key_path()
    try:
        data = path.read_bytes()
        return data if len(data) == KEY_BYTES else None
    except FileNotFoundError:
        if not create:
            return None
    except OSError:
        log.warning("ключ канала не прочитан: %s", path)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"pipe.key.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(os.urandom(KEY_BYTES))
    try:
        # не перезаписывать ключ, если другой процесс успел создать его первым
        if path.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
    return load_key(create=False)


def _family() -> str:
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def pipe_address(pid: int | None = None) -> str:
    """\\\\.\\pipe\\jarvis-confirm-<пользователь>-<PID>; на Linux (тесты) — сокет во временной папке."""
    pid = os.getpid() if pid is None else pid
    user = "".join(ch for ch in os.environ.get("USERNAME", "user") if ch.isalnum() or ch in "-_") or "user"
    if sys.platform == "win32":
        return rf"\\.\pipe\jarvis-confirm-{user}-{pid}"
    import tempfile

    return str(Path(tempfile.gettempdir()) / f"jarvis-confirm-{user}-{pid}-{secrets.token_hex(3)}.sock")


def _ask_pipe(address: str, summary: str, details: str, caller: Caller) -> bool:
    key = load_key(create=False)
    if key is None:
        log.warning("нет ключа канала подтверждений")
        return False
    result: dict[str, bool] = {}

    def worker() -> None:
        try:
            with connection.Client(address, family=_family(), authkey=key) as conn:
                payload = {"summary": summary[:4000], "details": details[:20000], "caller": caller}
                conn.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                if not conn.poll(TIMEOUT_S):
                    return
                answer = json.loads(conn.recv_bytes(MAX_MESSAGE).decode("utf-8"))
                result["ok"] = isinstance(answer, dict) and answer.get("approved") is True
        except Exception as e:
            log.warning("канал подтверждений: %s", e)

    # Client() и рукопожатие идут без таймаута — ждём в отдельном потоке не дольше TIMEOUT_S
    t = threading.Thread(target=worker, name="jarvis-confirm-client", daemon=True)
    t.start()
    t.join(TIMEOUT_S + 1)
    return result.get("ok", False)
