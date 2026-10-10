"""Технический лог: логгер "jarvis" в <data>\\logs\\<имя>.log (RotatingFileHandler, UTF-8) и в stderr.

Файл не открылся — только stderr, без падения. Под pythonw stderr нет — тогда только файл.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from pc import settings

FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(message)s"
_configured: set[str] = set()


def setup(name: str = "jarvis", level: int = logging.INFO, stderr: bool = True) -> logging.Logger:
    """Настроить логгер "jarvis" один раз на процесс; name — имя файла лога."""
    logger = logging.getLogger("jarvis")
    if name in _configured:
        return logger
    _configured.add(name)
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter(FORMAT)
    try:
        path = settings.data_file("logs", f"{name}.log")
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as e:
        if sys.stderr is not None:
            print(f"лог-файл не открыт: {e}", file=sys.stderr)
    if stderr and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.WARNING)
        logger.addHandler(sh)
    return logger
