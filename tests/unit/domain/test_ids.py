import pytest

from jarvis.domain.ids import CHILD_KINDS, child_id, child_number, task_id


def test_task_and_child_formats() -> None:
    assert task_id(42) == "task_42"
    assert child_id(task_id(42), "ev", 31) == "task_42.ev_31"
    assert child_number("task_42.ev_31") == 31
    assert CHILD_KINDS == ("plan", "step", "mc", "call", "art", "appr", "ev")


@pytest.mark.parametrize("value", ["task_1", "task_1.foo_2", "task_1.ev_0", "task_1.ev_x"])
def test_invalid_child_ids_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="дочерней"):
        child_number(value)


def test_numbers_must_be_positive() -> None:
    with pytest.raises(ValueError, match="положительным"):
        task_id(0)
    with pytest.raises(ValueError, match="положительным"):
        child_id(task_id(1), "ev", 0)
