"""Идентификаторы задач и их дочерних сущностей (02-domain.md §1, ADR 0012)."""

import re
from typing import Literal, NewType, get_args

TaskId = NewType("TaskId", str)

ChildKind = Literal["plan", "step", "mc", "call", "art", "appr", "ev"]
CHILD_KINDS: tuple[ChildKind, ...] = get_args(ChildKind)

_TASK_ID = re.compile(r"task_([1-9][0-9]*)")
_CHILD_ID = re.compile(r"(task_[1-9][0-9]*)\.([a-z]+)_([1-9][0-9]*)")


def task_id(number: int) -> TaskId:
    if number < 1:
        raise ValueError(f"номер задачи должен быть положительным: {number}")
    return TaskId(f"task_{number}")


def child_id(task: TaskId, kind: ChildKind, number: int) -> str:
    if number < 1:
        raise ValueError(f"номер дочерней сущности должен быть положительным: {number}")
    return f"{task}.{kind}_{number}"


def parse_task_id(value: str) -> TaskId:
    if _TASK_ID.fullmatch(value) is None:
        raise ValueError(f"не ID задачи: {value!r}")
    return TaskId(value)


def child_number(value: str) -> int:
    """Номер дочерней сущности: `task_42.ev_31` → 31."""
    match = _CHILD_ID.fullmatch(value)
    if match is None or match.group(2) not in CHILD_KINDS:
        raise ValueError(f"не ID дочерней сущности: {value!r}")
    return int(match.group(3))
