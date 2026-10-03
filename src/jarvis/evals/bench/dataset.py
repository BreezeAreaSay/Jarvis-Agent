"""Набор задач бенчмарка агента: один фиксированный датасет для всех моделей (Session 4.5).

Датасет — YAML: рабочая папка-фикстура (`workspace`, путь → содержимое) и задачи. У задачи — категория,
запрос на естественном языке, ожидания и эталонное решение (`reference`): реплики модели, с которыми
задача проходит. Эталон прогоняется тестами на scripted-модели — так проверяется, что ожидания
выполнимы на фикстуре и оценщик не ошибается, ещё до прогона настоящей модели.
"""

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, JsonValue, model_validator

from jarvis.domain.errors import JarvisError
from jarvis.domain.states import TaskStatus


class DatasetError(JarvisError):
    category = "bench_dataset"


class Category(StrEnum):
    """Категории из задания Session 4.5 (буквы — в отчёте)."""

    SIMPLE = "simple"  # A: выбор одного инструмента
    SEARCH = "search"  # B: поиск
    READ = "read"  # C: чтение и ответ по содержимому
    MIXED = "mixed"  # D: русский и английские технические термины
    AMBIGUOUS = "ambiguous"  # E: нужен уточняющий вопрос
    INVALID = "invalid"  # F: инструментов для этого нет — объяснить ограничение
    INJECTION = "injection"  # G: инструкции в данных
    MULTISTEP = "multistep"  # H: поиск → чтение → ответ


LETTERS: dict[Category, str] = dict(zip(Category, "ABCDEFGH", strict=True))


class Constraint(BaseModel, frozen=True, extra="forbid"):
    """Условие на аргумент вызова. `path` — путь от рабочей папки ("." — она сама); модель может
    передать его относительным, абсолютным, с «./» или «/» на конце — сравниваются канонические пути."""

    equals: JsonValue = None
    contains: str | None = None  # подстрока без учёта регистра
    regex: str | None = None
    path: str | list[str] | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [
            self.equals is not None,
            self.contains is not None,
            self.regex is not None,
            self.path is not None,
        ]
        if sum(given) != 1:
            raise ValueError("у условия ровно один вид: equals, contains, regex или path")
        return self


Phrase = str | list[str]  # строка или любая из списка (без учёта регистра)


class Expect(BaseModel, frozen=True, extra="forbid"):
    status: TaskStatus = TaskStatus.COMPLETED
    tool: list[str] | Literal["none"] | None = None  # первый вызов — один из этих; "none" — без вызовов
    args: dict[str, Constraint] = {}  # условия на аргументы первого вызова
    sequence: list[list[str]] = []  # исполненные вызовы содержат эти шаги по порядку
    answer_all: list[Phrase] = []  # в ответе есть каждая фраза (или одна из вариантов)
    answer_none: list[str] = []  # в ответе нет ни одной из этих (выдуманный результат)
    clarify: bool = False  # ответ — уточняющий вопрос, файл не читался
    limitation: bool = False  # ответ объясняет, что сделать этого нельзя
    forbid_args: list[str] = []  # подстроки, которых нет в аргументах ни одного вызова
    no_escalation: bool = False  # ни отказа политики, ни запроса подтверждения

    @model_validator(mode="after")
    def _args_need_tool(self) -> Self:
        if self.args and not isinstance(self.tool, list):
            raise ValueError("условия на аргументы требуют ожидаемого инструмента (tool)")
        return self


class ReferenceStep(BaseModel, frozen=True, extra="forbid"):
    """Шаг эталонного решения: вызов инструмента или ответ."""

    tool: str | None = None
    arguments: dict[str, JsonValue] = {}
    finish: str | None = None
    evidence: list[str] = []
    decision: str = "эталон"

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.tool is None) == (self.finish is None):
            raise ValueError("шаг эталона — либо tool, либо finish")
        return self

    def reply(self) -> dict[str, JsonValue]:
        if self.tool is not None:
            action: dict[str, JsonValue] = {"type": "tool", "tool": self.tool, "arguments": self.arguments}
        else:
            action = {"type": "finish", "answer": self.finish, "evidence": list[JsonValue](self.evidence)}
        return {"decision": self.decision, "action": action}


class BenchTask(BaseModel, frozen=True, extra="forbid"):
    id: str = Field(pattern=r"^[a-h][0-9]{2}_[a-z0-9_]+$")
    category: Category
    input: str = Field(min_length=1)
    expect: Expect
    reference: list[ReferenceStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _id_letter(self) -> Self:
        if self.id[0] != LETTERS[self.category].lower():
            raise ValueError(
                f"ID задачи категории {self.category} начинается с {LETTERS[self.category].lower()}"
            )
        if self.reference[-1].finish is None:
            raise ValueError("эталон заканчивается ответом (finish)")
        return self


class Dataset(BaseModel, frozen=True, extra="forbid"):
    version: Literal[1] = 1
    workspace: dict[str, str]  # путь → содержимое; путь с «/» на конце — папка
    tasks: list[BenchTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                raise ValueError(f"задача {task.id} определена дважды")
            seen.add(task.id)
        for name in self.workspace:
            parts = name.replace("\\", "/").split("/")
            if name.startswith(("/", "\\")) or ":" in name or ".." in parts:
                raise ValueError(f"файл фикстуры должен быть внутри рабочей папки: {name}")
        return self

    def select(self, ids: Sequence[str]) -> "Dataset":
        unknown = set(ids) - {task.id for task in self.tasks}
        if unknown:
            raise DatasetError(f"нет задач: {', '.join(sorted(unknown))}")
        return self.model_copy(update={"tasks": [task for task in self.tasks if task.id in ids]})


def load_dataset(path: Path) -> Dataset:
    try:
        return Dataset.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise DatasetError(f"{path}: {exc}") from None
