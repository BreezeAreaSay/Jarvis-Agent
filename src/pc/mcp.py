"""MCP-сервер pc для мозга (GPT через Codex): `python -m pc.mcp`, в exe — `jarvis-cli.exe mcp`.

Каждый инструмент — тонкая обёртка над функцией pc с caller="brain": политика, подтверждения и privacy —
внутри pc. Ответ — Result.text и, если есть data, компактный JSON следующей строкой; ok=False — тот же формат
(это не ошибка протокола). Исключение pc — понятный текст без traceback (traceback — в лог).

stdout — только протокол MCP; логи — stderr и <data>\\logs\\pc-mcp.log. FastMCP и модули pc импортируются
лениво: `import pc.mcp` быстрый и не тянет mcp, PySide6, openai_codex, pycaw.
"""

import dataclasses
import importlib
import inspect
import io
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from pc import settings
from pc.result import Caller, Result

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("jarvis")

CALLER: Caller = "brain"

INSTRUCTIONS = (
    "Инструменты ПК владельца (Windows 11). Окно указывай hwnd из контекста «[окно: … hwnd N]» "
    "или из list_windows, либо названием. Рискованные действия pc сам подтверждает у человека; "
    "ответ «Отменено.» — человек отказал, не повторяй."
)

# инструмент → строки таблицы политики (pc.policy.TABLE)
TOOL_ACTIONS: dict[str, list[str]] = {
    "list_windows": ["list_windows"],
    "list_apps": ["list_apps"],
    "find_files": ["find_files"],
    "open": ["open_app", "open_folder", "open_file", "open_executable", "open_url"],
    "focus": ["focus"],
    "close": ["close_window", "jarvis_self"],
    "window": ["window"],
    "volume": ["volume"],
    "media": ["media"],
    "processes": ["list_processes"],
    "kill_process": ["kill_process", "jarvis_self"],
    "clipboard_get": ["clipboard_get"],
    "clipboard_set": ["clipboard_set"],
    "read_text_file": ["read_text"],
    "move_to_trash": ["trash"],
    "type_text": ["type_text"],
    "lock": ["lock"],
    "power": ["power"],
}

READ_ONLY_TOOLS = frozenset(
    {"list_windows", "list_apps", "find_files", "processes", "clipboard_get", "read_text_file"}
)
DESTRUCTIVE_TOOLS = frozenset({"kill_process", "move_to_trash", "power"})

NO_CUR = "Цель «@cur» мозгу недоступна: укажи hwnd из контекста или list_windows, либо название окна."
NO_TARGET = "Укажи окно: hwnd из контекста или list_windows, либо название окна."

Target = str | int
FindKind = Literal["file", "folder", "any"]
OpenKind = Literal["app", "folder", "file", "url"]
WindowAction = Literal["minimize", "maximize", "restore", "minimize_all"]
MediaAction = Literal["play_pause", "next", "prev"]
PowerAction = Literal["sleep", "shutdown", "restart"]

_action_lock = threading.Lock()  # действия pc — по одному, даже если мозг вызвал инструменты параллельно


# --- общая обёртка ------------------------------------------------------------------------------


def _pc(name: str) -> ModuleType:
    """Модуль pc по имени — лениво, в рабочем потоке (pycaw, win32 и т.п. грузятся при первом вызове)."""
    return importlib.import_module(f"pc.{name}")


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    return str(value)


def _format(result: Result) -> str:
    """Result.text и JSON data следующей строкой. Текст ещё раз — через privacy (страховка)."""
    text = _pc("privacy").redact_text(result.text)
    if result.data is None:
        return text
    data = json.dumps(result.data, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return f"{text}\n{data}"


def _error_text(tool: str, error: Exception) -> str:
    # текст исключения не отдаём: в нём могут быть пути и данные, которые privacy не видел
    if isinstance(error, NotImplementedError):
        return f"«{tool}»: действие пока не реализовано."
    return f"«{tool}»: внутренняя ошибка ({type(error).__name__}), подробности — в pc-mcp.log."


def _run_locked(action: Callable[[], Result]) -> Result:
    with _action_lock:
        return action()


async def _call(tool: str, action: Callable[[], Result]) -> str:
    """Выполнить действие pc в рабочем потоке (цикл событий не блокируется на подтверждении до 60 с)."""
    import anyio.to_thread

    started = time.perf_counter()
    try:
        result = await anyio.to_thread.run_sync(_run_locked, action)
        if not isinstance(result, Result):
            raise TypeError(f"ожидался Result, получен {type(result).__name__}")
        text = _format(result)
    except Exception as e:
        log.exception("инструмент %s упал", tool)
        return _error_text(tool, e)
    log.info("%s: ok=%s, %.0f мс", tool, result.ok, (time.perf_counter() - started) * 1000)
    return text


def _is_cur(target: Target) -> bool:
    return isinstance(target, str) and target.strip().casefold() == "@cur"


def _is_empty(target: Target) -> bool:
    return isinstance(target, str) and not target.strip()


# --- инструменты (докстринг — описание для мозга) ---------------------------------------------------


async def list_windows() -> str:
    """Открытые окна: hwnd, заголовок, exe. hwnd — цель для focus, close, window, type_text."""
    return await _call("list_windows", lambda: _pc("windows").list_windows_result(CALLER))


async def list_apps(query: str = "") -> str:
    """Установленные приложения из меню «Пуск». query — часть имени; пусто — все."""
    return await _call("list_apps", lambda: _pc("apps").list_apps_result(query or None, CALLER))


async def find_files(query: str, kind: FindKind = "any") -> str:
    """Поиск файлов и папок по имени (Everything). kind: file — файлы, folder — папки, any — всё."""
    return await _call("find_files", lambda: _pc("files").find(query, kind=kind, caller=CALLER))


async def open_(target: str, kind: OpenKind | None = None) -> str:
    """Открыть приложение (имя из list_apps), папку или файл (полный путь) или URL (только http/https).
    kind — что это; не указан — определится по target."""
    return await _call("open", lambda: _pc("files").open_target(target, kind, CALLER))


async def focus(target: Target) -> str:
    """Переключиться на окно. target — hwnd из контекста «[окно: … hwnd N]» или list_windows, либо название
    окна; «@cur» недоступна."""
    if _is_cur(target):
        return NO_CUR
    return await _call("focus", lambda: _pc("windows").focus_target(target, CALLER))


async def close(target: Target) -> str:
    """Закрыть окно (программа сама спросит про сохранение). target — hwnd или название окна;
    «@cur» недоступна."""
    if _is_cur(target):
        return NO_CUR
    return await _call("close", lambda: _pc("windows").close_target(target, CALLER))


async def window(action: WindowAction, target: Target = "") -> str:
    """Свернуть (minimize), развернуть (maximize) или восстановить (restore) окно target — hwnd или название;
    minimize_all — свернуть все окна, target не нужен. «@cur» недоступна."""
    if _is_cur(target):
        return NO_CUR
    if action != "minimize_all" and _is_empty(target):
        return NO_TARGET
    tgt = None if action == "minimize_all" else target
    return await _call("window", lambda: _pc("windows").window_action(action, tgt, CALLER))


async def volume(set: int | None = None, delta: int | None = None, mute: bool | None = None) -> str:
    """Громкость — ровно одно из: set — уровень 0–100; delta — изменение (например, -10 или 20);
    mute — true выключить звук, false включить."""
    if sum(v is not None for v in (set, delta, mute)) != 1:
        return "Укажи ровно одно: set, delta или mute."
    return await _call("volume", lambda: _pc("audio").volume(set=set, delta=delta, mute=mute, caller=CALLER))


async def media(action: MediaAction) -> str:
    """Медиаклавиша: play_pause — пауза/воспроизведение, next — следующий трек, prev — предыдущий."""
    return await _call("media", lambda: _pc("media").media(action, CALLER))


async def processes(name: str = "") -> str:
    """Запущенные процессы, сгруппированные по exe, с числом экземпляров. name — часть имени; пусто — все."""
    return await _call("processes", lambda: _pc("procs").processes(name or None, CALLER))


async def kill_process(name: str) -> str:
    """Завершить процесс по имени exe (например, notepad.exe); спрашивает человека."""
    return await _call("kill_process", lambda: _pc("procs").kill(name, CALLER))


async def clipboard_get() -> str:
    """Прочитать текст из буфера обмена; спрашивает человека."""
    return await _call("clipboard_get", lambda: _pc("system").clipboard_get(CALLER))


async def clipboard_set(text: str) -> str:
    """Записать текст в буфер обмена."""
    return await _call("clipboard_set", lambda: _pc("system").clipboard_set(text, CALLER))


async def read_text_file(path: str) -> str:
    """Прочитать текстовый файл (до 64 КБ) по полному пути; спрашивает человека."""
    return await _call("read_text_file", lambda: _pc("files").read_text(path, CALLER))


async def move_to_trash(path: str) -> str:
    """Переместить файл или папку в корзину по полному пути; спрашивает человека."""
    return await _call("move_to_trash", lambda: _pc("files").trash(path, CALLER))


async def type_text(text: str, target: Target) -> str:
    """Ввести текст (до 500 символов) в окно target — hwnd или название окна; «@cur» недоступна.
    Спрашивает человека."""
    if _is_cur(target):
        return NO_CUR
    if _is_empty(target):
        return NO_TARGET
    return await _call("type_text", lambda: _pc("system").type_text(text, target, CALLER))


async def lock() -> str:
    """Заблокировать компьютер."""
    return await _call("lock", lambda: _pc("system").lock(CALLER))


async def power(action: PowerAction) -> str:
    """Сон (sleep), выключение (shutdown) или перезагрузка (restart); спрашивает человека."""
    return await _call("power", lambda: _pc("system").power(action, CALLER))


TOOLS: dict[str, Callable[..., Any]] = {
    "list_windows": list_windows,
    "list_apps": list_apps,
    "find_files": find_files,
    "open": open_,
    "focus": focus,
    "close": close,
    "window": window,
    "volume": volume,
    "media": media,
    "processes": processes,
    "kill_process": kill_process,
    "clipboard_get": clipboard_get,
    "clipboard_set": clipboard_set,
    "read_text_file": read_text_file,
    "move_to_trash": move_to_trash,
    "type_text": type_text,
    "lock": lock,
    "power": power,
}


# --- сервер -------------------------------------------------------------------------------------


def describe(fn: Callable[..., Any]) -> str:
    """Описание инструмента для мозга — докстринг одной строкой."""
    return " ".join(inspect.cleandoc(fn.__doc__ or "").split())


def build_server() -> "FastMCP":
    """FastMCP с 18 инструментами (без запуска). Импорт mcp — здесь, а не при `import pc.mcp`."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("pc", instructions=INSTRUCTIONS, log_level="WARNING")
    for name, fn in TOOLS.items():
        hints = ToolAnnotations(
            readOnlyHint=name in READ_ONLY_TOOLS, destructiveHint=name in DESTRUCTIVE_TOOLS
        )
        server.add_tool(fn, name=name, description=describe(fn), annotations=hints, structured_output=False)
    return server


def _setup_logging() -> None:
    """Логгер "jarvis" и предупреждения mcp → stderr и <data>\\logs\\pc-mcp.log (не открылся — stderr)."""
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        if isinstance(sys.stderr, io.TextIOWrapper) and not sys.stderr.isatty():
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        handlers.append(logging.StreamHandler(sys.stderr))
    try:
        path = settings.data_file("logs", "pc-mcp.log")
        handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8"))
    except OSError as e:
        if sys.stderr is not None:
            sys.stderr.write(f"pc.mcp: лог-файл не открылся: {e}\n")
    root = logging.getLogger()
    fmt = logging.Formatter("%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s")
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    log.setLevel(logging.INFO)


def main() -> None:
    """Запуск сервера по stdio (вызывают `python -m pc.mcp` и `jarvis-cli.exe mcp`)."""
    _setup_logging()
    started = time.perf_counter()
    server = build_server()
    log.info(
        "pc.mcp: pid %d, %d инструментов, старт %.0f мс",
        os.getpid(),
        len(TOOLS),
        (time.perf_counter() - started) * 1000,
    )
    try:
        server.run("stdio")
    except Exception:
        log.exception("pc.mcp упал")
        raise


if __name__ == "__main__":
    main()
