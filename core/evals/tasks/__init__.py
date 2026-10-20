"""Задачи для агента: обычные дела, трудные места, безопасность (evals.tasks.everyday, hard, safety).

Добавить задачу — описать Task в одном из модулей и добавить в его TASKS (как — docs/EVALS.md)."""

from evals.tasks import everyday, hard, safety
from evals.tasks.base import CATEGORIES, Task

TASKS: list[Task] = [*everyday.TASKS, *hard.TASKS, *safety.TASKS]
BY_ID: dict[str, Task] = {t.id: t for t in TASKS}

__all__ = ["BY_ID", "CATEGORIES", "TASKS", "Task"]
