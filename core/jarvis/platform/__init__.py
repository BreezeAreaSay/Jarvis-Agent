"""Выбор платформы: настоящая Windows или заглушка (Linux, тесты, демо)."""

import logging
import sys

from jarvis.platform.base import Platform, PlatformError

log = logging.getLogger(__name__)

__all__ = ["Platform", "PlatformError", "create_platform"]


def create_platform(name: str = "auto") -> Platform:
    if name == "auto":
        name = "windows" if sys.platform == "win32" else "fake"
    if name == "windows":
        from jarvis.platform.windows import WindowsPlatform

        return WindowsPlatform()
    from jarvis.platform.fake import FakePlatform

    if sys.platform == "win32":
        log.warning("платформа-заглушка на Windows: действия с системой выполняться не будут")
    return FakePlatform()
