"""Журнал ядра: файл с ротацией в <data>/logs/core.log и stderr."""

import faulthandler
import logging
import logging.handlers
import sys
from typing import IO, Any

from jarvis import paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_crash_file: IO[Any] | None = None  # держим открытым: faulthandler пишет в него при вылете


def setup(debug: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_jarvis_configured", False):
        return
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "core.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(stream)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "websockets", "comtypes", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._jarvis_configured = True  # type: ignore[attr-defined]
    global _crash_file
    try:
        _crash_file = open(paths.logs_dir() / "crash.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        faulthandler.enable(all_threads=True)
