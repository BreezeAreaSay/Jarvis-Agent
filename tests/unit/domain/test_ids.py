import pytest

from jarvis.domain.ids import CHILD_KINDS, child_id, child_number, parse_task_id, task_id


def test_task_and_child_formats() -> None:
    assert task_id(42) == "task_42"
    assert child_id(task_id(42), "ev", 31) == "task_42.ev_31"
    assert child_number("task_42.ev_31") == 31
    assert parse_task_id("task_7") == "task_7"
    assert CHILD_KINDS == ("plan", "step", "mc", "call", "art", "appr", "ev")


@pytest.mark.parametrize("value", ["task_0", "task_", "task_01", "Task_1", "task_1.ev_1", "42"])
def test_invalid_task_ids_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="ID задачи"):
        parse_task_id(value)


@pytest.mark.parametrize("value", ["task_1", "task_1.foo_2", "task_1.ev_0", "task_1.ev_x"])
def test_invalid_child_ids_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="дочерней"):
        child_number(value)


def test_numbers_must_be_positive() -> None:
    with pytest.raises(ValueError, match="положительным"):
        task_id(0)
    with pytest.raises(ValueError, match="положительным"):
        child_id(task_id(1), "ev", 0)
