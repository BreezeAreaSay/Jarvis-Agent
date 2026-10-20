"""Задача для агента: текст (как сказал бы человек), сцена, проверка по итоговому миру и эталонное решение.

Проверка смотрит только на мир и на след попытки (что спросили, что разрешили), а не на слова модели:
«отправлено» в итоге агента ничего не значит, если в чате «Мама» сообщения нет. check возвращает причины
провала (пусто — успех). Общие правила безопасности (рискованное действие без разрешения, ввод в чужое
окно) проверяет evals.scoring для всех задач; задача добавляет свои (инструкция с экрана, обход отказа).

solution — эталонное решение для детерминированного режима, mistakes — нарочно ошибочные (имя → решение и
кусок ожидаемой причины провала): так проверяются сами проверки.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evals.sim.desktop import Desktop

CATEGORIES = {"everyday": "Обычные дела", "hard": "Трудные места", "safety": "Безопасность"}


@dataclass
class Approval:
    title: str  # «Нажать «Отправить» в «Новое письмо — Почта»?»
    detail: str
    allowed: bool
    step: int  # номер действия агента
    seq: int  # номер записи журнала мира в этот момент
    kind: str = "agent"


@dataclass
class Question:
    text: str
    answer: str
    step: int
    seq: int


@dataclass
class Trace:
    """След попытки: что вернул агент и что было между ним и человеком."""

    status: str  # done | stopped | error
    outcome: str  # done | partial | failed — как оценил итог сам агент
    summary: str
    error: str = ""
    approvals: list[Approval] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    steps: int = 0
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    actions: list[str] = field(default_factory=list)
    timed_out: bool = False

    def denied(self) -> list[Approval]:
        return [a for a in self.approvals if not a.allowed]


Steps = Callable[[Desktop], list[Any]]


def allow_all(approval: Approval, desktop: Desktop) -> bool:
    return True


def plain_answer(question: str, desktop: Desktop) -> str:
    return "Сделай, как я сказал в задаче."


@dataclass
class Task:
    id: str
    title: str  # коротко — для таблиц отчёта
    category: str  # everyday | hard | safety
    checks: str  # что проверяет — одной фразой
    text: str  # задача агенту
    setup: Callable[[Desktop], None]
    check: Callable[[Desktop, Trace], list[str]]
    solution: Steps
    apps: frozenset[str] = frozenset()  # exe, куда задача разрешает вводить текст
    mistakes: dict[str, tuple[Steps, str]] = field(default_factory=dict)
    approve: Callable[[Approval, Desktop], bool] = allow_all
    answer: Callable[[str, Desktop], str] = plain_answer
    expects_question: bool = False  # правильно — спросить (иначе вопрос — «лишний»)
    vision: bool = False  # без картинок экрана не решить
    plan: list[str] = field(default_factory=lambda: ["Выполнить задачу", "Проверить результат"])
    extra_safety: Callable[[Desktop, Trace], list[str]] | None = None  # свои нарушения безопасности

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "checks": self.checks,
            "text": self.text,
            "vision": self.vision,
        }


def expect(problems: list[str], ok: bool, problem: str) -> None:
    """Не выполнено условие — причина провала в список."""
    if not ok:
        problems.append(problem)


def claimed_done(trace: Trace) -> bool:
    return trace.status == "done" and trace.outcome == "done"
