from datetime import timedelta

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.core.leases import Holder, Leases
from jarvis.domain.ids import TaskId
from jarvis.domain.lease import Lease

TASK = TaskId("task_1")


def leases(clock: ManualClock, owner: str = "A") -> Leases:
    return Leases(uow=InMemoryStorage().unit_of_work, clock=clock, owner=owner, ttl_s=30)


def test_holder_classification() -> None:
    clock = ManualClock()
    mine = leases(clock)
    expires = clock.now() + timedelta(seconds=10)
    assert mine.holder(None) is Holder.FREE
    assert mine.holder(Lease(task_id=TASK, owner="A", expires_at=expires)) is Holder.MINE
    assert mine.holder(Lease(task_id=TASK, owner="B", expires_at=expires)) is Holder.BUSY
    clock.advance(10)
    assert mine.holder(Lease(task_id=TASK, owner="B", expires_at=expires)) is Holder.STALE
    assert mine.holder(Lease(task_id=TASK, owner="A", expires_at=expires)) is Holder.MINE


def test_fresh_lease_and_heartbeat_interval() -> None:
    clock = ManualClock()
    service = leases(clock)
    assert service.fresh(TASK).expires_at == clock.now() + timedelta(seconds=30)
    assert service.heartbeat_s == 10
    with pytest.raises(ValueError, match="положительным"):
        Leases(uow=InMemoryStorage().unit_of_work, clock=clock, owner="A", ttl_s=0)
