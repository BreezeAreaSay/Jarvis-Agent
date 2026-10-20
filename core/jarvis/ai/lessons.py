"""Заметки агента о программах: что в программе не сработало и как получилось (finish.lesson).

Хранятся по exe программы (последние MAX_PER_APP, не больше MAX_APPS программ) в kv базы; агент получает их,
когда программа впереди, — рядом с подсказками jarvis.ai.playbooks, как данные, а не команды: заметку писала
модель, читавшая чужие окна. Настройка ai.agent_notes, очистка — «Приватность» → «Заметки агента
о программах».
"""

import time
from typing import Any

from jarvis.ai.format import one_line
from jarvis.redact import redact

KV = "agent:lessons"
MAX_PER_APP = 5
MAX_APPS = 50
MAX_LEN = 240


def _all(db: Any) -> dict[str, list[dict[str, Any]]]:
    data = db.kv_get(KV, {}) or {}
    return data if isinstance(data, dict) else {}


def add(db: Any, exe: str, text: str) -> str:
    """Запомнить заметку о программе exe; вернёт, что записано ("" — нечего: пусто, повтор, нет программы)."""
    key = exe.strip().casefold()
    text = one_line(redact(text or ""), MAX_LEN).strip()
    if not key or len(text) < 8:
        return ""
    data = _all(db)
    notes = [n for n in data.get(key, []) if isinstance(n, dict) and n.get("text") != text]
    notes.append({"text": text, "at": time.time()})
    data.pop(key, None)  # свежая программа — в конец: старые выпадают первыми
    data[key] = notes[-MAX_PER_APP:]
    while len(data) > MAX_APPS:
        data.pop(next(iter(data)))
    db.kv_set(KV, data)
    return text


def for_app(db: Any, exe: str) -> list[str]:
    """Заметки о программе exe — от старых к новым."""
    notes = _all(db).get(exe.strip().casefold(), [])
    return [str(n["text"]) for n in notes if isinstance(n, dict) and n.get("text")]


def stats(db: Any) -> tuple[int, int]:
    """(сколько заметок, сколько байт) — для «Приватности»."""
    texts = [str(n.get("text", "")) for notes in _all(db).values() for n in notes if isinstance(n, dict)]
    return len(texts), sum(len(t.encode()) for t in texts)


def clear(db: Any) -> int:
    count = stats(db)[0]
    db.kv_set(KV, {})
    return count
