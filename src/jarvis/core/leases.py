"""Аренда задачи (02-domain.md §3, ADR 0021).

Задачу в активном состоянии ведёт только процесс-владелец её аренды. Аренду берёт `submit`, продлевает
каждая контрольная точка и фоновый heartbeat, снимает контрольная точка, которая выводит задачу из
активных состояний. Чужая живая аренда — `TaskBusy`. Активная задача без живой аренды — прерванная:
процесс, который её вёл, завершился; такую задачу не продолжают, а переводят в FAILED (`interrupted`).

Хранилище только сравнивает и записывает строки аренды; что считать живым, решает этот модуль.
"""

from datetime import timedelta
from enum import StrEnum

from jarvis.domain.errors import ConcurrentModification, LeaseLost
from jarvis.domain.ids import TaskId
from jarvis.domain.lease import Lease
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory


class Holder(StrEnum):
    FREE = "free"  # аренды нет
    MINE = "mine"  # аренда этого процесса (живая или истёкшая — её никто не перехватил)
    BUSY = "busy"  # живая аренда другого процесса
    STALE = "stale"  # истёкшая аренда другого процесса: он завершился, не закончив задачу


class Leases:
    def __init__(self, *, uow: UnitOfWorkFactory, clock: Clock, owner: str, ttl_s: float) -> None:
        if ttl_s <= 0:
            raise ValueError("срок аренды должен быть положительным")
        self._uow = uow
        self._clock = clock
        self.owner = owner
        self.ttl_s = ttl_s

    @property
    def heartbeat_s(self) -> float:
        """Владелец продлевает аренду трижды за срок: одна задержка цикла событий её не теряет."""
        return self.ttl_s / 3

    def fresh(self, task_id: TaskId) -> Lease:
        return Lease(
            task_id=task_id, owner=self.owner, expires_at=self._clock.now() + timedelta(seconds=self.ttl_s)
        )

    def holder(self, lease: Lease | None) -> Holder:
        if lease is None:
            return Holder.FREE
        if lease.owner == self.owner:
            return Holder.MINE
        return Holder.BUSY if lease.is_live(self._clock.now()) else Holder.STALE

    def renew(self, lease: Lease) -> Lease:
        """Продлить свою аренду отдельной записью; LeaseLost, если её успели перехватить или снять."""
        renewed = self.fresh(lease.task_id)
        try:
            with self._uow() as uow:
                uow.leases.put(renewed, expected=lease)
                uow.commit()
        except ConcurrentModification:
            raise LeaseLost(
                f"аренду задачи {lease.task_id} перехватили: продлить не удалось", task_id=lease.task_id
            ) from None
        return renewed
