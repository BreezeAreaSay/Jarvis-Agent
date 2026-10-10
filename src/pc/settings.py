"""Пути Jarvis и секции [pc]/[aliases] конфига.

jarvis.config читает остальные секции через тот же load_toml(): кэш по mtime общий.
pc не импортирует jarvis.
"""

import logging
import os
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_CONFIG = Path(r"C:\Jarvis\jarvis.toml")

_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "data": {}, "error": ""}


def is_frozen() -> bool:
    """Запущены из собранного PyInstaller-бандла."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Где лежат scripts/, bench/, jarvis.example.toml: корень репозитория или распакованный бандл."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if is_frozen() and meipass else REPO_ROOT


def install_dir() -> Path:
    """Папка установки: рядом с Jarvis.exe; в разработке — корень репозитория."""
    return Path(sys.executable).resolve().parent if is_frozen() else REPO_ROOT


def data_dir() -> Path:
    """Каталог данных: JARVIS_DATA_DIR, иначе %LOCALAPPDATA%\\Jarvis. Не создаётся здесь."""
    env = os.environ.get("JARVIS_DATA_DIR", "").strip()
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Jarvis"
    return Path.home() / "AppData" / "Local" / "Jarvis"


def data_file(*parts: str) -> Path:
    """Путь внутри каталога данных; родительская папка создаётся."""
    path = data_dir().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """JARVIS_CONFIG, иначе C:\\Jarvis\\jarvis.toml; у exe без папки C:\\Jarvis — <data>\\jarvis.toml."""
    env = os.environ.get("JARVIS_CONFIG", "").strip()
    if env:
        return Path(env)
    if is_frozen() and not DEV_CONFIG.parent.is_dir():
        return data_dir() / "jarvis.toml"
    return DEV_CONFIG


def load_toml() -> dict[str, Any]:
    """Конфиг целиком; перечитывается только при смене пути, mtime или размера. Нет файла — {}.

    Ошибка разбора — {} и текст ошибки в config_error() (doctor покажет), без падения.
    """
    path = config_path()
    try:
        st = path.stat()
        key: tuple[Any, ...] = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(path), None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
        data: dict[str, Any] = {}
        error = ""
        if key[1] is not None:
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                error = f"{path}: {e}"
                log.warning("конфиг не прочитан: %s", error)
        _cache.update(key=key, data=data, error=error)
        return data


def config_error() -> str:
    """Текст последней ошибки чтения конфига или пустая строка."""
    load_toml()
    return str(_cache["error"])


@dataclass(frozen=True)
class PcSettings:
    es_path: str = ""
    private_paths: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)


def load_pc_settings() -> PcSettings:
    data = load_toml()
    pc = data.get("pc") if isinstance(data.get("pc"), dict) else {}
    raw_aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    private = pc.get("private_paths", [])
    return PcSettings(
        es_path=str(pc.get("es_path", "") or ""),
        private_paths=tuple(str(p) for p in private if isinstance(p, str) and p.strip())
        if isinstance(private, list)
        else (),
        aliases={str(k).casefold(): str(v) for k, v in raw_aliases.items() if isinstance(v, str)},
    )


def es_path() -> Path:
    """es.exe: из [pc] es_path, иначе bin\\es.exe рядом с приложением, иначе C:\\Jarvis\\bin\\es.exe."""
    configured = load_pc_settings().es_path
    if configured:
        return Path(configured)
    bundled = install_dir() / "bin" / "es.exe"
    if bundled.exists():
        return bundled
    return Path(r"C:\Jarvis\bin\es.exe")
