"""Аренда задачи (02-domain.md §3, ADR 0021).

Задачу ведёт только процесс, держащий её аренду. Аренда — строка с владельцем и сроком; живая, пока
срок не истёк. Правила выдачи и продления — в `core.leases`; хранилище только сравнивает и записывает.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from jarvis.domain.ids import TaskId


class Lease(BaseModel, frozen=True, extra="forbid"):
    task_id: TaskId
    owner: str = Field(min_length=1)  # процесс: хост, pid и случайный токен
    expires_at: datetime

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at
