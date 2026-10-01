from itertools import product

import pytest

from jarvis.domain.errors import InvalidTransition
from jarvis.domain.states import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    TaskStatus,
    check_transition,
    is_allowed,
)

S = TaskStatus
NON_TERMINAL = [status for status in TaskStatus if status not in TERMINAL_STATUSES]

# Таблица из 02-domain.md §3, записанная независимо от реализации.
FORWARD = {
    (S.CREATED, S.ROUTING),
    (S.ROUTING, S.PLANNING),
    (S.ROUTING, S.EXECUTING),
    (S.ROUTING, S.COMPLETED),
    (S.PLANNING, S.EXECUTING),
    (S.EXECUTING, S.WAITING_CONFIRMATION),
    (S.EXECUTING, S.REPLANNING),
    (S.EXECUTING, S.VERIFYING),
    (S.WAITING_CONFIRMATION, S.EXECUTING),
    (S.VERIFYING, S.COMPLETED),
    (S.VERIFYING, S.REPLANNING),
    (S.VERIFYING, S.FAILED),
    (S.REPLANNING, S.EXECUTING),
}
EXITS = {(source, target) for source in NON_TERMINAL for target in (S.FAILED, S.CANCELLED, S.BUDGET_EXCEEDED)}
EXPECTED_ALLOWED = FORWARD | EXITS


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.CREATED, S.ROUTING),
        (S.ROUTING, S.PLANNING),
        (S.PLANNING, S.EXECUTING),
        (S.EXECUTING, S.VERIFYING),
        (S.VERIFYING, S.COMPLETED),
        (S.EXECUTING, S.WAITING_CONFIRMATION),
        (S.EXECUTING, S.REPLANNING),
        (S.VERIFYING, S.REPLANNING),
        (S.REPLANNING, S.EXECUTING),
    ],
)
def test_main_path_transitions_are_allowed(source: TaskStatus, target: TaskStatus) -> None:
    check_transition(source, target)


@pytest.mark.parametrize("source", NON_TERMINAL)
@pytest.mark.parametrize("target", [S.FAILED, S.CANCELLED, S.BUDGET_EXCEEDED])
def test_every_active_state_can_fail_be_cancelled_or_exceed_budget(
    source: TaskStatus, target: TaskStatus
) -> None:
    check_transition(source, target)


@pytest.mark.parametrize(("source", "target"), list(product(TaskStatus, TaskStatus)))
def test_transition_table_matches_specification(source: TaskStatus, target: TaskStatus) -> None:
    if (source, target) in EXPECTED_ALLOWED:
        assert is_allowed(source, target)
        check_transition(source, target)
    else:
        assert not is_allowed(source, target)
        with pytest.raises(InvalidTransition) as raised:
            check_transition(source, target)
        assert raised.value.details == {"source": source.value, "target": target.value}


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_states_have_no_exits(status: TaskStatus) -> None:
    assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.CREATED, S.EXECUTING),
        (S.CREATED, S.COMPLETED),
        (S.PLANNING, S.COMPLETED),
        (S.PLANNING, S.VERIFYING),
        (S.EXECUTING, S.COMPLETED),
        (S.EXECUTING, S.PLANNING),
        (S.WAITING_CONFIRMATION, S.COMPLETED),
        (S.REPLANNING, S.VERIFYING),
        (S.COMPLETED, S.EXECUTING),
        (S.FAILED, S.ROUTING),
        (S.CANCELLED, S.CANCELLED),
    ],
)
def test_forbidden_transitions_raise(source: TaskStatus, target: TaskStatus) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(source, target)
