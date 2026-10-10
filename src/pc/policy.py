"""Политика риска из AGENTS.md как данные.

Каждое действие pc — строка таблицы; неизвестное действие — исключение, а не «разрешить».
Мозгу — не больше BRAIN_BUDGET изменяющих действий без подтверждения за BRAIN_WINDOW_S секунд;
дальше каждое изменяющее действие — «спросить». Чтение в бюджет не входит.
"""

import threading
import time
from collections import deque
from typing import Literal

from pc.result import Caller, Result, fail

Decision = Literal["allow", "confirm", "deny"]

# действие → (user, brain)
TABLE: dict[str, tuple[Decision, Decision]] = {
    # поиск файлов, списки окон/приложений/процессов, чтение громкости
    "find_files": ("allow", "allow"),
    "list_windows": ("allow", "allow"),
    "list_apps": ("allow", "allow"),
    "list_processes": ("allow", "allow"),
    "volume_get": ("allow", "allow"),
    # открыть приложение из инвентаря или папку, переключиться, свернуть/развернуть, громкость, медиа
    "open_app": ("allow", "allow"),
    "open_folder": ("allow", "allow"),
    "focus": ("allow", "allow"),
    "window": ("allow", "allow"),
    "volume": ("allow", "allow"),
    "media": ("allow", "allow"),
    # открыть файл не исполняемого типа
    "open_file": ("allow", "allow"),
    # исполняемый или «активный» файл
    "open_executable": ("confirm", "confirm"),
    # URL http/https: у user — только если URL есть в тексте команды (проверяет jarvis.execute)
    "open_url": ("allow", "confirm"),
    "close_window": ("allow", "confirm"),
    "clipboard_get": ("allow", "confirm"),
    "clipboard_set": ("allow", "allow"),
    "lock": ("allow", "allow"),
    # «—» в таблице для user: у человека нет такой команды — отказ
    "read_text": ("deny", "confirm"),
    "type_text": ("deny", "confirm"),
    "kill_process": ("confirm", "confirm"),
    "trash": ("confirm", "confirm"),
    "power": ("confirm", "confirm"),
    # никогда
    "shell": ("deny", "deny"),
    "write_file": ("deny", "deny"),
    "delete_permanent": ("deny", "deny"),
    "jarvis_self": ("deny", "deny"),
}

READ_ONLY = frozenset({"find_files", "list_windows", "list_apps", "list_processes", "volume_get"})

BRAIN_BUDGET = 5
BRAIN_WINDOW_S = 60.0

DENY_TEXT = "Это действие Jarvis не выполняет."


class UnknownAction(KeyError):
    """Действия нет в таблице политики."""


_lock = threading.Lock()
_brain_allowed: deque[float] = deque()


def check(action: str, caller: Caller) -> Decision:
    """Чистая таблица, без бюджета."""
    try:
        user, brain = TABLE[action]
    except KeyError:
        raise UnknownAction(action) from None
    if caller == "user":
        return user
    if caller == "brain":
        return brain
    raise ValueError(f"неизвестный вызывающий: {caller!r}")


def decide(action: str, caller: Caller, now: float | None = None) -> Decision:
    """Таблица плюс бюджет мозга. «allow» изменяющего действия мозга сразу засчитывается в бюджет."""
    decision = check(action, caller)
    if caller != "brain" or decision != "allow" or action in READ_ONLY:
        return decision
    t = time.monotonic() if now is None else now
    with _lock:
        while _brain_allowed and t - _brain_allowed[0] >= BRAIN_WINDOW_S:
            _brain_allowed.popleft()
        if len(_brain_allowed) >= BRAIN_BUDGET:
            return "confirm"
        _brain_allowed.append(t)
    return "allow"


def reset_budget() -> None:
    """Для тестов."""
    with _lock:
        _brain_allowed.clear()


def require(action: str, caller: Caller, summary: str, details: str = "") -> Result | None:
    """None — можно выполнять; иначе Result отказа. «Спросить» — через confirm_client.confirm()."""
    decision = decide(action, caller)
    if decision == "allow":
        return None
    if decision == "deny":
        return fail(DENY_TEXT)
    from pc import confirm_client

    if confirm_client.confirm(summary, details, caller):
        return None
    return fail("Отменено.")
