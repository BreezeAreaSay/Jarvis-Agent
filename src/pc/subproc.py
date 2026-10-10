"""Единственная точка запуска внешних процессов.

Всегда CREATE_NO_WINDOW (никогда DETACHED_PROCESS: с ним PowerShell 5.1 падает на смене кодировки),
stdin=DEVNULL, вывод — байты; декодирует вызывающий явно.
"""

import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


def run(
    argv: Sequence[str | Path],
    timeout: float,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Completed:
    """Запустить и дождаться. TimeoutExpired и OSError (нет файла) пробрасываются вызывающему."""
    r = subprocess.run(
        [str(a) for a in argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        creationflags=NO_WINDOW,
        check=False,
    )
    return Completed(r.returncode, r.stdout, r.stderr)


def spawn(
    argv: Sequence[str | Path],
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout: int | None = subprocess.DEVNULL,
    stderr: int | None = subprocess.DEVNULL,
) -> subprocess.Popen[bytes]:
    """Запустить в фоне. По умолчанию вывод отбрасывается: непрочитанный PIPE подвешивает дочерний процесс."""
    return subprocess.Popen(
        [str(a) for a in argv],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        creationflags=NO_WINDOW,
    )
