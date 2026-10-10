"""Контекст запроса: окно на момент хоткея, последний объект действия, результаты find.

«Последнее» (объект и результаты find) забывается через TTL_S секунд. Цель "@cur" — свежий последний объект
(hwnd или имя), иначе окно на момент хоткея. CLI хранит контекст между вызовами в <data>\\cli_context.json.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from pc import settings
from pc.windows import WindowInfo

log = logging.getLogger("jarvis")

TTL_S = 300.0
CUR = "@cur"


@dataclass
class Context:
    active_window: WindowInfo | None = None
    last_object: str | None = None
    last_kind: str | None = None
    last_hwnd: int | None = None
    found: list[str] = field(default_factory=list)
    last_ts: float = 0.0

    def fresh(self, now: float | None = None) -> bool:
        """«Последнее» ещё помнится."""
        t = time.time() if now is None else now
        return self.last_ts > 0 and t - self.last_ts <= TTL_S

    def cur_target(self, now: float | None = None) -> str | int | None:
        """Цель "@cur": свежий последний объект (hwnd или имя), иначе окно на момент хоткея."""
        if self.fresh(now):
            if self.last_hwnd:
                return self.last_hwnd
            if self.last_object:
                return self.last_object
        if self.active_window is not None:
            return self.active_window.hwnd
        return None

    def last(self, now: float | None = None) -> str | None:
        """Имя последнего объекта для подсказки моделям, если он свежий."""
        return self.last_object if self.fresh(now) else None

    def found_items(self, now: float | None = None) -> list[str]:
        """Свежие результаты find (для «открой второй»)."""
        return list(self.found) if self.fresh(now) else []

    def remember(self, obj: str, kind: str, hwnd: int | None = None, now: float | None = None) -> None:
        self.last_object, self.last_kind, self.last_hwnd = obj, kind, hwnd
        self.last_ts = time.time() if now is None else now

    def set_found(self, paths: list[str], now: float | None = None) -> None:
        self.found = list(paths)
        self.last_ts = time.time() if now is None else now

    # --- CLI: контекст между вызовами ----------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "last_object": self.last_object,
            "last_kind": self.last_kind,
            "last_hwnd": self.last_hwnd,
            "found": self.found,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Context":
        ctx = cls()
        if isinstance(data.get("last_object"), str):
            ctx.last_object = data["last_object"]
        if isinstance(data.get("last_kind"), str):
            ctx.last_kind = data["last_kind"]
        if isinstance(data.get("last_hwnd"), int):
            ctx.last_hwnd = data["last_hwnd"]
        if isinstance(data.get("found"), list):
            ctx.found = [p for p in data["found"] if isinstance(p, str)]
        if isinstance(data.get("last_ts"), int | float):
            ctx.last_ts = float(data["last_ts"])
        return ctx


def cli_path() -> str:
    return str(settings.data_dir() / "cli_context.json")


def load_cli() -> Context:
    """Контекст CLI из прошлого вызова; протухший или битый — пустой."""
    try:
        with open(cli_path(), encoding="utf-8") as f:
            ctx = Context.from_json(json.load(f))
    except (OSError, ValueError):
        return Context()
    return ctx if ctx.fresh() else Context()


def save_cli(ctx: Context) -> None:
    """Атомарно: tmp + os.replace. Ошибка записи — только в лог."""
    path = settings.data_file("cli_context.json")
    tmp = path.with_name(f"cli_context.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(ctx.to_json(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("контекст CLI не сохранён: %s", e)
        tmp.unlink(missing_ok=True)
