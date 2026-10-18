"""Дополнительные модули, которые не входят в установщик и скачиваются по кнопке (например OpenCV для камеры).

Ставятся pip'ом встроенного Python в <data>/pylib; каталог добавляется в sys.path при запуске ядра.
"""

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from jarvis import paths
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

INSTALL_TIMEOUT_S = 900.0
_lock = asyncio.Lock()


def extras_dir() -> Path:
    return paths.sub_dir("pylib")


def ensure_on_path() -> None:
    folder = str(extras_dir())
    if folder not in sys.path:
        sys.path.append(folder)


def _pip_command(spec: str, target: Path) -> list[str]:
    base = [
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--upgrade",
        "--target",
        str(target),
        spec,
    ]
    try:
        import pip  # noqa: F401 — во встроенном Python из NuGet pip есть

        return [sys.executable, "-m", "pip", "install", *base]
    except ImportError:
        uv = shutil.which("uv")
        if uv:
            return [uv, "pip", "install", "--python", sys.executable, "--target", str(target), spec]
    raise UserFacingError("Не могу скачать модуль: в этой сборке нет pip")


async def install(spec: str) -> None:
    """Скачать и поставить пакет в <data>/pylib. Ошибка — UserFacingError с понятным текстом."""
    async with _lock:
        target = extras_dir()
        cmd = _pip_command(spec, target)
        log.info("ставлю дополнительный модуль: %s", spec)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), INSTALL_TIMEOUT_S)
        except TimeoutError as e:
            proc.kill()
            raise UserFacingError("Модуль скачивается слишком долго — проверьте интернет") from e
        if proc.returncode != 0:
            tail = out.decode("utf-8", "replace").strip().splitlines()[-3:]
            log.error("модуль %s не поставился: %s", spec, " | ".join(tail))
            raise UserFacingError("Не удалось скачать модуль — проверьте интернет и попробуйте ещё раз")
        ensure_on_path()
        log.info("модуль %s поставлен", spec)
