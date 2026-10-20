"""Журнал работы агента: что он видел и делал — чтобы человек мог разобраться, как шла работа («Как он это
делал» у итога в чате).

Каждый шаг — действие (аргументы без секретов), чем кончилось, что модель написала перед ним (если написала)
и картинки экрана после него; шаг 0 — экран в начале, отдельный шаг — проверка перед итогом. Папка
data/agent_runs/<run_id>/: journal.json и NNN-K.jpg (K — монитор). Хранятся последние MAX_RUNS прогонов; чат
удалили — его журналы тоже. Настройка ai.agent_journal; очистка — «Приватность» → «Журнал работы агента».

Пишется из пула ввода-вывода (ctx.run_io) и никогда не роняет работу агента.
"""

import base64
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from jarvis import paths
from jarvis.ai.format import one_line
from jarvis.redact import redact

log = logging.getLogger(__name__)

FILE = "journal.json"
MAX_RUNS = 30
MAX_IMAGES = 4  # картинок на шаг: главный монитор и другие
MAX_TEXT = 4000  # ответа действия, мысли модели
MAX_ARG = 300
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE = re.compile(r"^\d{3}-\d\.jpg$")
# аргументы инструментов MCP вроде password, api_key, otp_code — в журнал не пишем
SECRET_ARGS = re.compile(
    r"(?:^|_)(?:pass(?:word)?|pwd|пароль|token|secret|api_?key|pin|cvv|cvc|otp|code)(?:$|_)", re.I
)


def root() -> Path:
    return paths.data_dir() / "agent_runs"


def safe_args(args: dict[str, Any], *, secret: bool = False) -> dict[str, str]:
    """Аргументы действия для журнала: длинное — коротко, номера карт и токены — замазаны; secret (ввод в поле
    пароля) и аргументы с именами вроде password — целиком."""
    out: dict[str, str] = {}
    for key, value in list(args.items())[:12]:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        hidden = (secret and key == "text") or bool(SECRET_ARGS.search(str(key)))
        out[str(key)] = "••••" if hidden and text else one_line(redact(text), MAX_ARG)
    return out


class Journal:
    """Журнал одного прогона."""

    def __init__(self, run_id: str, task: str, chat_id: str, folder: Path | None = None) -> None:
        if not RUN_ID.match(run_id):
            raise ValueError(f"плохой id прогона: {run_id!r}")
        self.dir = (folder or root()) / run_id
        self.data: dict[str, Any] = {
            "run_id": run_id,
            "task": one_line(redact(task), 500),
            "chat_id": chat_id,
            "started": time.time(),
            "finished": None,
            "status": "running",
            "steps": [],
        }

    def step(
        self,
        title: str,
        *,
        name: str = "",
        args: dict[str, str] | None = None,
        ok: bool = True,
        result: str = "",
        thought: str = "",
        images: list[str] | None = None,
    ) -> None:
        index = len(self.data["steps"])
        files: list[str] = []
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for k, url in enumerate((images or [])[:MAX_IMAGES]):
                head, _, encoded = url.partition(",")
                if not head.startswith("data:image/jpeg;base64"):
                    continue
                file = f"{index:03d}-{k}.jpg"
                (self.dir / file).write_bytes(base64.b64decode(encoded))
                files.append(file)
        except Exception:
            log.exception("картинка для журнала агента не сохранена")
        self.data["steps"].append(
            {
                "index": index,
                "ts": time.time(),
                "title": one_line(title, 200),
                "name": name,
                "args": args or {},
                "ok": ok,
                "result": redact(result[:MAX_TEXT]),
                "thought": redact(thought[:MAX_TEXT]),
                "images": files,
            }
        )
        self._save()

    def finish(
        self, status: str, outcome: str, summary: str, error: str = "", plan: list[str] | None = None
    ) -> None:
        self.data.update(
            {
                "finished": time.time(),
                "status": status,
                "outcome": outcome,
                "summary": redact(summary[:MAX_TEXT]),
                "error": redact(error[:MAX_TEXT]),
                "plan": list(plan or []),
            }
        )
        self._save()
        prune(self.dir.parent)

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / (FILE + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.dir / FILE)
        except Exception:
            log.exception("журнал агента не сохранён")


def _runs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    runs = [p for p in folder.iterdir() if p.is_dir() and RUN_ID.match(p.name)]
    return sorted(runs, key=lambda p: p.stat().st_mtime)


def prune(folder: Path | None = None, keep: int = MAX_RUNS) -> int:
    """Оставить последние keep прогонов; вернёт, сколько удалено."""
    runs = _runs(folder or root())
    old = runs[: max(0, len(runs) - keep)]
    for run in old:
        shutil.rmtree(run, ignore_errors=True)
    return len(old)


def load(run_id: str, folder: Path | None = None) -> dict[str, Any] | None:
    if not RUN_ID.match(run_id):
        return None
    file = (folder or root()) / run_id / FILE
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def image(run_id: str, name: str, folder: Path | None = None) -> bytes | None:
    if not RUN_ID.match(run_id) or not IMAGE.match(name):
        return None
    try:
        return ((folder or root()) / run_id / name).read_bytes()
    except OSError:
        return None


def stats(folder: Path | None = None) -> tuple[int, int]:
    """(сколько прогонов, сколько байт) — для «Приватности»."""
    runs = _runs(folder or root())
    size = sum(f.stat().st_size for run in runs for f in run.iterdir() if f.is_file())
    return len(runs), size


def clear(folder: Path | None = None) -> int:
    runs = _runs(folder or root())
    for run in runs:
        shutil.rmtree(run, ignore_errors=True)
    return len(runs)


def forget_chat(chat_id: str, folder: Path | None = None) -> int:
    """Чат удалили — его журналы тоже."""
    removed = 0
    for run in _runs(folder or root()):
        data = load(run.name, run.parent)
        if data is not None and data.get("chat_id") == chat_id:
            shutil.rmtree(run, ignore_errors=True)
            removed += 1
    return removed
