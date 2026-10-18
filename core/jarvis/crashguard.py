"""Защита от нативных вылетов при загрузке моделей.

sherpa-onnx и espeak-ng при некоторых ошибках (файл не открылся, повреждённая модель) завершают весь процесс
(exit), а не бросают исключение, — ядро падает и после перезапуска падает снова на том же месте. Поэтому перед
такой загрузкой ядро оставляет метку <data>/guard/<этап>.json и убирает её после (удачно или с исключением —
неважно: процесс жив). Метка, оставшаяся от прошлого запуска, значит, что процесс умер на этом этапе. После
LIMIT таких смертей подряд этап пропускается — с понятной ошибкой вместо вылета, — пока человек не попросит
попробовать снова (reset) или не выйдет новая версия Jarvis.
"""

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jarvis import __version__, paths

log = logging.getLogger(__name__)

LIMIT = 2  # смертей подряд на одном этапе, после которых он пропускается
STATE = "state.json"

_lock = threading.Lock()
_state: dict[str, Any] | None = None  # {"version", "crashes": {этап: число}}
_state_dir: Path | None = None  # для какого каталога данных прочитано


class StageBlocked(RuntimeError):
    """Этап пропущен: на нём ядро уже падало."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _dir() -> Path:
    folder = paths.data_dir() / "guard"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _marker(stage: str) -> Path:
    return _dir() / (re.sub(r"[^a-zA-Z0-9_.-]+", "_", stage)[:80] + ".json")


def _save(state: dict[str, Any]) -> None:
    path = _dir() / STATE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _loaded() -> dict[str, Any]:
    """Состояние; при первом обращении — разбор меток, оставшихся от прошлых запусков."""
    global _state, _state_dir
    current = paths.data_dir()
    if _state is not None and _state_dir == current:
        return _state
    state: dict[str, Any] = {"version": __version__, "crashes": {}}
    try:
        raw = json.loads((_dir() / STATE).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == __version__:  # новая версия — с чистого листа
            state["crashes"] = {str(k): int(v) for k, v in dict(raw.get("crashes") or {}).items()}
    except (OSError, ValueError, TypeError):
        pass
    for marker in _dir().glob("*.json"):
        if marker.name == STATE:
            continue
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            stage = str(info["stage"])
            if int(info.get("pid", 0)) != os.getpid():
                state["crashes"][stage] = state["crashes"].get(stage, 0) + 1
                count = state["crashes"][stage]
                log.error("ядро аварийно завершилось на этапе «%s» (раз подряд: %s)", stage, count)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with contextlib.suppress(OSError):
            marker.unlink()
    _state, _state_dir = state, current
    with contextlib.suppress(OSError):
        _save(state)
    return state


def blocked(stage: str) -> bool:
    with _lock:
        return _loaded()["crashes"].get(stage, 0) >= LIMIT


def crashed() -> dict[str, int]:
    """Этапы, на которых ядро падало, и сколько раз подряд (для интерфейса и журнала)."""
    with _lock:
        return dict(_loaded()["crashes"])


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Опасная нативная загрузка. Этап уже валил ядро LIMIT раз — StageBlocked без попытки."""
    with _lock:
        state = _loaded()
        if state["crashes"].get(name, 0) >= LIMIT:
            raise StageBlocked(name)
        marker = _marker(name)
        info = {"stage": name, "pid": os.getpid(), "ts": time.time()}
        marker.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    ok = False
    try:
        yield
        ok = True
    finally:
        with _lock:
            with contextlib.suppress(OSError):
                marker.unlink()
            if ok and state["crashes"].pop(name, None) is not None:
                with contextlib.suppress(OSError):
                    _save(state)


def reset(name: str | None = None) -> None:
    """Попробовать снова: забыть сбои этапа (или всех)."""
    with _lock:
        state = _loaded()
        if name is None:
            state["crashes"].clear()
        else:
            state["crashes"].pop(name, None)
        with contextlib.suppress(OSError):
            _save(state)


def forget_process() -> None:
    """Ядро завершается штатно — его метки не считаются падением (загрузка оборвалась выходом)."""
    with _lock, contextlib.suppress(OSError):
        for marker in _dir().glob("*.json"):
            if marker.name == STATE:
                continue
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                if int(json.loads(marker.read_text(encoding="utf-8")).get("pid", 0)) == os.getpid():
                    marker.unlink()


def _reload_for_tests() -> None:
    """Тесты: следующий вызов заново прочитает метки, как при новом запуске."""
    global _state
    with _lock:
        _state = None
