"""Процесс программы-провайдера: запуск без окна консоли, ввод через stdin, построчное чтение stdout, хвост
stderr для текста ошибки, остановка вместе с дочерними процессами (psutil)."""

import asyncio
import contextlib
import logging
import sys
from typing import Any

import psutil

from jarvis.ai.cli.locate import Launch, shell_command, shell_safe

log = logging.getLogger(__name__)

LINE_LIMIT = 32 * 1024 * 1024  # одна строка stream-json бывает большой (результат инструмента, картинка)
STDERR_TAIL = 8192
_CREATE_NO_WINDOW = 0x08000000


class CliStartError(OSError):
    """Программа не запустилась (нет файла, нет прав, плохие аргументы для cmd.exe)."""


class CliProcess:
    def __init__(self, launch: Launch, args: list[str], *, cwd: str, env: dict[str, str]) -> None:
        self.launch = launch
        self.args = args
        self.cwd = cwd
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr: bytearray = bytearray()
        self._tasks: list[asyncio.Task[Any]] = []

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    @property
    def stderr_tail(self) -> str:
        return bytes(self._stderr[-STDERR_TAIL:]).decode("utf-8", "replace")

    async def start(self, stdin: bytes | None = None) -> None:
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": self.cwd,
            "env": self.env,
            "limit": LINE_LIMIT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            if self.launch.shell:
                if not all(shell_safe(a) for a in self.args) or not shell_safe(self.launch.path):
                    raise CliStartError("в пути или параметрах программы есть недопустимые символы")
                self.proc = await asyncio.create_subprocess_shell(
                    shell_command(self.launch, self.args), **kwargs
                )
            else:
                self.proc = await asyncio.create_subprocess_exec(*self.launch.argv, *self.args, **kwargs)
        except CliStartError:
            raise
        except OSError as e:
            raise CliStartError(str(e) or type(e).__name__) from e
        self._tasks.append(asyncio.ensure_future(self._read_stderr()))
        self._tasks.append(asyncio.ensure_future(self._write_stdin(stdin)))

    async def _write_stdin(self, data: bytes | None) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            if data:
                proc.stdin.write(data)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            log.info("программа не приняла ввод: %s", e)
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    async def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr += chunk
                if len(self._stderr) > STDERR_TAIL * 4:
                    del self._stderr[: len(self._stderr) - STDERR_TAIL]

    async def readline(self) -> bytes:
        """Следующая строка stdout ("" — программа закрыла вывод)."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return await proc.stdout.readline()
        except ValueError:
            # строка длиннее LINE_LIMIT: пропускаем её целиком
            with contextlib.suppress(Exception):
                await proc.stdout.readuntil(b"\n")
            return b"{}\n"

    async def wait(self, timeout: float | None = None) -> int | None:
        proc = self.proc
        if proc is None:
            return None
        try:
            return await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            return None

    def kill(self) -> None:
        """Остановить программу и все её дочерние процессы."""
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            with contextlib.suppress(psutil.Error):
                child.kill()
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    async def close(self) -> None:
        """Убить, если ещё жива, и дождаться; фоновые чтения — отменить."""
        if self.proc is not None and self.proc.returncode is None:
            self.kill()
            await self.wait(5.0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
