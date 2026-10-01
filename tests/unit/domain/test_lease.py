from datetime import UTC, datetime, timedelta

import pytest

from jarvis.domain.ids import TaskId
from jarvis.domain.lease import Lease
from jarvis.domain.states import ACTIVE_STATUSES, TERMINAL_STATUSES, TaskStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_lease_is_live_until_it_expires() -> None:
    lease = Lease(task_id=TaskId("task_1"), owner="host:1:ab", expires_at=NOW + timedelta(seconds=30))
    assert lease.is_live(NOW)
    assert lease.is_live(NOW + timedelta(seconds=29.999))
    assert not lease.is_live(NOW + timedelta(seconds=30))


def test_lease_needs_an_owner() -> None:
    with pytest.raises(ValueError, match="owner"):
        Lease(task_id=TaskId("task_1"), owner="", expires_at=NOW)


def test_active_statuses_exclude_waiting_and_terminal() -> None:
    assert TaskStatus.WAITING_CONFIRMATION not in ACTIVE_STATUSES
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES
    assert ACTIVE_STATUSES | TERMINAL_STATUSES | {TaskStatus.WAITING_CONFIRMATION} == set(TaskStatus)
