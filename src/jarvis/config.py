"""Конфиг Jarvis: mode, [ui], [hands], [brain], [journal]. Пути и кэш файла — из pc.settings."""

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from pc import settings

Mode = Literal["normal", "local"]


@dataclass(frozen=True)
class UiConfig:
    hotkey: str = "ctrl+alt+space"
    autohide_s: float = 1.2
    width: int = 720
    backdrop: Literal["solid", "acrylic"] = "solid"
    animations: bool = True


def _default_server_cmd() -> list[str]:
    return [str(settings.app_root() / "scripts" / "start_hands.cmd")]


@dataclass(frozen=True)
class HandsConfig:
    url: str = "http://127.0.0.1:8081"
    model: str = "4b"
    server_cmd: list[str] = field(default_factory=_default_server_cmd)
    max_tokens: int = 128
    timeout_s: float = 4.0
    min_tokens_per_s: dict[str, float] = field(default_factory=lambda: {"4b": 60.0, "8b": 35.0})


@dataclass(frozen=True)
class BrainConfig:
    model_quick: str = "gpt-6-luna"
    model_deep: str = "gpt-6.1-sol"
    service_tier: str = ""
    proxy: str = ""
    fallback_provider: str = ""
    idle_new_thread_min: float = 20.0


@dataclass(frozen=True)
class JournalConfig:
    store_text: bool = True


@dataclass(frozen=True)
class Config:
    mode: Mode = "normal"
    ui: UiConfig = field(default_factory=UiConfig)
    hands: HandsConfig = field(default_factory=HandsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _pick(cls: type, raw: dict[str, Any]) -> Any:
    """Взять из секции только известные поля с подходящим типом; остальное — по умолчанию."""
    defaults = cls()
    kwargs: dict[str, Any] = {}
    for name in cls.__dataclass_fields__:
        if name not in raw:
            continue
        value, default = raw[name], getattr(defaults, name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        elif isinstance(default, dict):
            ok = isinstance(value, dict)
            if ok and name == "min_tokens_per_s":
                value = {str(k): float(v) for k, v in value.items() if isinstance(v, int | float)}
        else:
            ok = isinstance(value, str)
        if ok:
            kwargs[name] = value
    return cls(**kwargs)


_lock = threading.Lock()
_cache: dict[str, Any] = {"src": None, "cfg": None}


def load() -> Config:
    """Текущий конфиг. Пересобирается, только если pc.settings перечитал файл."""
    data = settings.load_toml()
    with _lock:
        if _cache["src"] is data and _cache["cfg"] is not None:
            return _cache["cfg"]
        mode = data.get("mode", "normal")
        ui = _pick(UiConfig, _section(data, "ui"))
        if ui.backdrop not in ("solid", "acrylic"):
            ui = UiConfig(**{**ui.__dict__, "backdrop": "solid"})
        cfg = Config(
            mode=mode if mode in ("normal", "local") else "normal",
            ui=ui,
            hands=_pick(HandsConfig, _section(data, "hands")),
            brain=_pick(BrainConfig, _section(data, "brain")),
            journal=_pick(JournalConfig, _section(data, "journal")),
        )
        _cache.update(src=data, cfg=cfg)
        return cfg


_MODE_LINE = re.compile(r"""^(\s*mode\s*=\s*)("[^"]*"|'[^']*'|[^\s#]+)(.*)$""")


def save_mode(mode: Mode) -> None:
    """Поменять в jarvis.toml только строку верхнего уровня `mode = "…"`, остальное не трогать.

    Строки нет — вставить первой; файла нет — создать копией jarvis.example.toml. Запись — tmp + os.replace.
    """
    if mode not in ("normal", "local"):
        raise ValueError(f"неизвестный режим: {mode!r}")
    path = settings.config_path()
    if path.exists():
        text = path.read_bytes().decode("utf-8-sig")
    else:
        example = settings.app_root() / "jarvis.example.toml"
        text = example.read_bytes().decode("utf-8-sig") if example.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            break
        body = line.rstrip("\r\n")
        m = _MODE_LINE.match(body)
        if m:
            lines[i] = f'{m.group(1)}"{mode}"{m.group(3)}{line[len(body) :] or newline}'
            replaced = True
            break
    if not replaced:
        lines.insert(0, f'mode = "{mode}"{newline}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
