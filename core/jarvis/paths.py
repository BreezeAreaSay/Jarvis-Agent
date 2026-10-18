"""Где ядро хранит данные: настройки, базу, модели, журналы.

Каталог данных — JARVIS_DATA_DIR, иначе %LOCALAPPDATA%\\Jarvis (Windows) или ~/.local/share/jarvis.
Ресурсы, которые едут вместе с программой (паки команд, звуки), лежат в пакете: resource_dir().
"""

import os
import sys
from pathlib import Path

_override: Path | None = None


def set_data_dir(path: str | Path | None) -> None:
    """Подменить каталог данных (аргумент --data-dir, тесты)."""
    global _override
    _override = Path(path) if path else None


def data_dir() -> Path:
    if _override is not None:
        root = _override
    elif os.environ.get("JARVIS_DATA_DIR"):
        root = Path(os.environ["JARVIS_DATA_DIR"])
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "Jarvis"
    else:
        root = Path.home() / ".local" / "share" / "jarvis"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sub_dir(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    return sub_dir("models")


def logs_dir() -> Path:
    return sub_dir("logs")


def cache_dir() -> Path:
    return sub_dir("cache")


def resource_dir() -> Path:
    """Каталог ресурсов пакета (jarvis/resources)."""
    return Path(__file__).resolve().parent / "resources"
