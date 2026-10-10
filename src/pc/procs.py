"""Процессы. ЗАГОТОВКА: реализует владелец модуля по docs/architecture.md."""

from dataclasses import dataclass, field

from pc.result import Caller, Result


@dataclass(frozen=True)
class ProcGroup:
    exe: str
    count: int
    pids: list[int] = field(default_factory=list)


def find_processes(name: str | None) -> list[ProcGroup]:
    raise NotImplementedError


def own_pids() -> set[int]:
    raise NotImplementedError


def processes(name: str | None, caller: Caller) -> Result:
    raise NotImplementedError


def kill(name: str, caller: Caller) -> Result:
    raise NotImplementedError
