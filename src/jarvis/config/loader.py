"""Слои конфигурации (06-errors-and-config.md §2, ADR 0018).

defaults → пользовательский config.toml (`load_config`) → параметры запуска (`with_overrides`). Слияние
глубокое; итог проверяется одной схемой `JarvisConfig`; неизвестный ключ — ошибка. Из окружения
читаются только `JARVIS_HOME` и `JARVIS_CONFIG`. Проектный слой появится вместе с реестром проектов (M5).
"""

import copy
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import JsonValue, ValidationError

from jarvis.domain.errors import ConfigError
from jarvis.domain.settings import JarvisConfig

ENV_HOME = "JARVIS_HOME"
ENV_CONFIG = "JARVIS_CONFIG"
DEFAULT_LAYER = "default"
RUNTIME_LAYER = "runtime"


@dataclass(frozen=True)
class LoadedConfig:
    config: JarvisConfig
    sources: Mapping[str, str]  # "budgets.agent.max_steps" → "default" | "user:<путь>" | "runtime"
    home: Path
    config_path: Path
    config_file_exists: bool


def default_home(platform: str = sys.platform) -> Path:
    if platform == "win32":
        return Path.home() / "AppData" / "Local" / "Jarvis"
    return Path.home() / ".local" / "share" / "jarvis"


def load_config(*, env: Mapping[str, str] | None = None) -> LoadedConfig:
    environ = os.environ if env is None else env
    home = Path(environ[ENV_HOME]) if environ.get(ENV_HOME) else default_home()
    explicit = environ.get(ENV_CONFIG)
    config_path = Path(explicit) if explicit else home / "config" / "config.toml"
    exists = config_path.is_file()
    if explicit and not exists:
        raise ConfigError(f"файл конфига из {ENV_CONFIG} не найден: {config_path}")

    layers: list[tuple[str, Mapping[str, Any]]] = [
        (DEFAULT_LAYER, JarvisConfig().model_dump(mode="json")),
        (f"user:{config_path}", _read_toml(config_path) if exists else {}),
    ]
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, layer in layers:
        _merge(merged, layer, name, sources)
    return LoadedConfig(
        config=_validate(merged, sources),
        sources=sources,
        home=home,
        config_path=config_path,
        config_file_exists=exists,
    )


def with_overrides(config: JarvisConfig, overrides: Mapping[str, Any]) -> JarvisConfig:
    """Слой параметров запуска поверх готового конфига (например, бюджет сценария eval)."""
    merged = config.model_dump(mode="json")
    sources: dict[str, str] = {}
    _merge(merged, overrides, RUNTIME_LAYER, sources)
    return _validate(merged, sources)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig: Блокнот и PowerShell 5.1 пишут UTF-8 с BOM.
        return tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: неверный TOML: {exc}") from None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: файл не в UTF-8: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: не удалось прочитать: {exc}") from None


def _merge(
    target: dict[str, Any], layer: Mapping[str, Any], name: str, sources: dict[str, str], prefix: str = ""
) -> None:
    for key, value in layer.items():
        path = f"{prefix}{key}"
        current = target.get(key)
        if isinstance(value, Mapping) and isinstance(current, dict):
            _merge(current, value, name, sources, f"{path}.")  # pyright: ignore[reportUnknownArgumentType]
        else:
            target[key] = copy.deepcopy(value)
            _record(sources, path, value, name)


def _record(sources: dict[str, str], path: str, value: Any, name: str) -> None:
    if isinstance(value, Mapping) and value:
        for key, nested in value.items():  # pyright: ignore[reportUnknownVariableType]
            _record(sources, f"{path}.{key}", nested, name)
    else:
        sources[path] = name


def _validate(merged: dict[str, Any], sources: Mapping[str, str]) -> JarvisConfig:
    try:
        return JarvisConfig.model_validate(merged)
    except ValidationError as exc:
        problems: list[JsonValue] = []
        for error in exc.errors():
            key = ".".join(str(part) for part in error["loc"])
            problems.append(f"{key}: {error['msg']} [{_source_of(key, sources)}]")
        message = "ошибка конфигурации:\n  " + "\n  ".join(str(problem) for problem in problems)
        raise ConfigError(message, problems=problems) from None


def _source_of(key: str, sources: Mapping[str, str]) -> str:
    """Слой ключа; для неизвестной таблицы — слой, задавший что-то внутри неё."""
    if key in sources:
        return sources[key]
    nested = [layer for path, layer in sources.items() if path.startswith(f"{key}.")]
    return nested[-1] if nested else DEFAULT_LAYER
