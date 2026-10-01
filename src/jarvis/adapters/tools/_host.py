"""Общее для инструментов HOST: цель, канонические пути, проверка открытого файла, работа в потоке.

Путь, о котором решает политика, — канонический: абсолютный, с раскрытыми ссылками, существующий.
Исполнение работает с этим же путём и перед делом убеждается, что он всё ещё указывает туда же:
подмена папки ссылкой между preview и исполнением обнаруживается, а не исполняется.
"""

import asyncio
import ctypes
import os
import stat
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jarvis.domain.errors import ToolExecutionFailed, ToolPreviewFailed
from jarvis.domain.paths import OsFamily, is_within, unsupported_form
from jarvis.domain.tools import ExecutionTarget, TargetKind
from jarvis.ports.tools import ToolContext

OS_FAMILY: OsFamily = "windows" if os.name == "nt" else "posix"
HOST = ExecutionTarget(kind=TargetKind.HOST, os_family=OS_FAMILY, name="local")

Kind = Literal["file", "dir", "symlink", "other"]
Expect = Literal["any", "file", "dir"]


async def canonical(raw: str, context: ToolContext, *, expect: Expect = "any") -> Path:
    """Существующий объект по пути из аргументов: относительный путь считается от рабочей папки
    задачи, `~` раскрывается, ссылки — тоже. Ошибка — ToolPreviewFailed с понятной причиной.
    Разрешение пути — ввод-вывод (сетевой диск может отвечать долго): оно идёт в потоке, и таймаут
    и отмена вызова продолжают работать."""
    return await in_thread(lambda stop: _canonical(raw, context, expect))


def _canonical(raw: str, context: ToolContext, expect: Expect) -> Path:
    if "\x00" in raw:
        raise ToolPreviewFailed("путь содержит нулевой символ")
    _check_form(raw)  # до обращения к диску: UNC-путь не должен ждать недоступный сервер
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = context.working_directory or os.getcwd()
        path = Path(base) / path
    _check_form(str(path))
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ToolPreviewFailed(f"нет такого пути: {raw}") from None
    except PermissionError:
        raise ToolPreviewFailed(f"нет доступа к пути: {raw}") from None
    except (OSError, RuntimeError) as exc:  # петля ссылок, слишком длинный путь, сбой устройства
        raise ToolPreviewFailed(f"путь не разрешается: {raw}: {exc}") from None
    _check_form(str(resolved))  # ссылка или подключённый диск могли привести на сетевой путь
    _check_kind(resolved, raw, expect)
    return resolved


def _check_form(path: str) -> None:
    if unsupported_form(path, OS_FAMILY):
        raise ToolPreviewFailed(
            f"форма пути не поддерживается (\\\\?\\…, \\\\.\\…, сетевой путь или поток NTFS): {path}"
        )


def _check_kind(path: Path, raw: str, expect: Expect) -> None:
    if expect == "dir" and not path.is_dir():
        raise ToolPreviewFailed(f"ожидалась папка, а это файл: {raw}")
    if expect == "file" and not path.is_file():
        what = "папка" if path.is_dir() else "не обычный файл"
        raise ToolPreviewFailed(f"ожидался файл, а это {what}: {raw}")


def unchanged(path: str, *, expect: Expect = "any") -> Path:
    """Перед исполнением: канонический путь из preview всё ещё разрешается сам в себя."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ToolExecutionFailed(f"путь исчез или стал недоступен после проверки: {path}") from None
    if not same_path(str(resolved), path) or unsupported_form(str(resolved), OS_FAMILY):
        raise ToolExecutionFailed(f"путь изменился после проверки (подмена ссылкой?): {path}")
    try:
        _check_kind(resolved, path, expect)
    except ToolPreviewFailed as exc:
        raise ToolExecutionFailed(f"тип объекта изменился после проверки: {exc.message}") from None
    return resolved


def same_path(left: str, right: str) -> bool:
    if OS_FAMILY == "windows":
        return os.path.normcase(left) == os.path.normcase(right)
    return left == right


def opened_path(fd: int) -> str | None:
    """Где на самом деле лежит открытый файл — по дескриптору, а не по имени. None — ОС не говорит."""
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return None
    if sys.platform == "win32":
        return _windows_final_path(fd)
    return None


def _windows_final_path(fd: int) -> str | None:  # pragma: no cover - только Windows
    if sys.platform != "win32":
        return None
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        return None
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        return "\\\\" + final[8:]
    return final.removeprefix("\\\\?\\")


def check_opened(fd: int, expected: str) -> None:
    """Открыт именно проверенный файл: иначе между проверкой и открытием путь подменили."""
    actual = opened_path(fd)
    if actual is not None:
        if not same_path(actual, expected):
            raise ToolExecutionFailed(f"открылся не тот файл, что проверялся: {expected}")
        return
    # ОС не сообщает путь по дескриптору: сверить, что имя и дескриптор — один и тот же объект.
    opened, named = os.fstat(fd), os.stat(expected, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise ToolExecutionFailed(f"открылся не тот файл, что проверялся: {expected}")


def is_protected(path: str, context: ToolContext) -> bool:
    return any(is_within(path, root, context.target.os_family) for root in context.protected_roots)


def kind_of(entry: os.DirEntry[str]) -> Kind:
    """Вид записи без перехода по ссылкам; точка соединения Windows (junction) — тоже ссылка."""
    if entry.is_symlink() or entry.is_junction():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def kind_of_stat(mode: int) -> Kind:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def size_of(entry: os.DirEntry[str], kind: Kind) -> int | None:
    if kind != "file":
        return None
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def sort_key(name: str) -> tuple[str, str]:
    """Один порядок на всех ОС: без учёта регистра, при равенстве — по точному имени."""
    return name.casefold(), name


def sorted_entries(entries: Sequence[os.DirEntry[str]]) -> list[os.DirEntry[str]]:
    return sorted(entries, key=lambda entry: sort_key(entry.name))


async def in_thread[T](work: Callable[[threading.Event], T]) -> T:
    """Блокирующая работа в потоке. Отмена (и таймаут runtime) выставляет флаг `stop`: работа
    проверяет его на каждом шаге и выходит, а вызывающий получает CancelledError сразу."""
    stop = threading.Event()
    try:
        return await asyncio.to_thread(work, stop)
    except asyncio.CancelledError:
        stop.set()
        raise


class Stopped(Exception):
    """Работа в потоке остановлена флагом: вызов отменён или вышел таймаут."""
