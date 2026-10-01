"""Аренды задач в работе ядра: два «процесса» (приложения с разными владельцами) над одним хранилищем.

Каждый сценарий проходит и на InMemory, и на SQLite (для SQLite «процессы» — отдельные соединения к
одному файлу). Время аренды — управляемые часы, поэтому гонки воспроизводятся детерминированно.
"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import App, build_app
from jarvis.core.budget import BudgetMeter
from jarvis.core.leases import Leases
from jarvis.core.runner import StageHandler
from jarvis.domain.errors import ConcurrentModification, LeaseLost, TaskBusy
from jarvis.domain.ids import TaskId
from jarvis.domain.lease import Lease
from jarvis.domain.settings import JarvisConfig, RuntimeSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task
from jarvis.domain.trace import EventKind
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, agent_prefix, error_of, request, step, transitions

pytestmark = pytest.mark.anyio

TTL = 30.0
Spawn = Callable[..., App]


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def shared() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def spawn(backend: str, shared: InMemoryStorage, tmp_path: Path, clock: ManualClock) -> Iterator[Spawn]:
    opened: list[SqliteStorage] = []

    def make(owner: str, stages: dict[TaskStatus, StageHandler] | None = None, *, ttl_s: float = TTL) -> App:
        if backend == "memory":
            storage: InMemoryStorage | SqliteStorage = shared
        else:
            storage = SqliteStorage(tmp_path / "jarvis.db")
            opened.append(storage)
        config = JarvisConfig(runtime=RuntimeSettings(lease_ttl_s=ttl_s))
        return build_app(config, stages=stages or {}, storage=storage, clock=clock, owner=owner)

    yield make
    for storage in opened:
        storage.close()


LeaseOf = Callable[[TaskId], Lease | None]


@pytest.fixture
def lease_of(backend: str, shared: InMemoryStorage, tmp_path: Path) -> LeaseOf:
    """Строка аренды глазами отдельного читателя хранилища."""
    reader = shared if backend == "memory" else None

    def read(task_id: TaskId) -> Lease | None:
        storage = reader or SqliteStorage(tmp_path / "jarvis.db")
        try:
            with storage.unit_of_work() as uow:
                return uow.leases.get(task_id)
        finally:
            if storage is not reader:
                storage.close()  # type: ignore[union-attr]

    return read


class Hangs:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        self.started.set()
        await self.release.wait()
        return StageOutcome(next_status=TaskStatus.VERIFYING, reason="шаг сделан")


def path_with(stage: StageHandler) -> dict[TaskStatus, StageHandler]:
    return {**ScriptedStages(agent_prefix()).handlers(), S.EXECUTING: stage}


async def test_submit_takes_the_lease_and_finishing_releases_it(
    spawn: Spawn, clock: ManualClock, lease_of: LeaseOf
) -> None:
    script = ScriptedStages([*agent_prefix(), step(S.EXECUTING, S.VERIFYING), step(S.VERIFYING, S.COMPLETED)])
    app = spawn("A", script.handlers())
    task_id = app.tasks.submit(request())
    assert lease_of(task_id) == Lease(
        task_id=task_id, owner="A", expires_at=clock.now() + timedelta(seconds=TTL)
    )
    await app.tasks.run_until_blocked(task_id)
    assert lease_of(task_id) is None


async def test_waiting_for_confirmation_holds_no_lease(spawn: Spawn, lease_of: LeaseOf) -> None:
    script = ScriptedStages([*agent_prefix(), step(S.EXECUTING, S.WAITING_CONFIRMATION)])
    app = spawn("A", script.handlers())
    task_id = app.tasks.submit(request())
    await app.tasks.run_until_blocked(task_id)
    assert lease_of(task_id) is None
    spawn("B").tasks.cancel(task_id, "отклонить и остановить")  # любой процесс может отменить
    assert app.tasks.get(task_id).status is S.CANCELLED


async def test_each_checkpoint_extends_the_lease(spawn: Spawn, clock: ManualClock, lease_of: LeaseOf) -> None:
    hangs = Hangs()
    app = spawn("A", path_with(hangs))
    task_id = app.tasks.submit(request())
    clock.advance(20)
    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    lease = lease_of(task_id)
    assert lease is not None
    assert lease.expires_at == clock.now() + timedelta(seconds=TTL)  # продлена контрольной точкой
    app.tasks.cancel(task_id, "стоп")
    await run


async def test_live_lease_keeps_other_processes_out(spawn: Spawn) -> None:
    hangs = Hangs()
    owner = spawn("A", path_with(hangs))
    other = spawn("B", path_with(Hangs()))
    task_id = owner.tasks.submit(request())
    run = asyncio.create_task(owner.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)

    with pytest.raises(TaskBusy) as busy:
        await other.tasks.run_until_blocked(task_id)
    assert busy.value.details["owner"] == "A"
    with pytest.raises(TaskBusy):
        other.tasks.cancel(task_id, "чужая отмена")
    assert other.tasks.recover_interrupted() == []
    assert other.tasks.get(task_id).status is S.EXECUTING

    hangs.release.set()
    owner.tasks.cancel(task_id, "свой процесс")
    assert (await run).status is S.CANCELLED


async def test_task_of_a_dead_process_is_recovered_as_interrupted(
    spawn: Spawn, clock: ManualClock, lease_of: LeaseOf
) -> None:
    dead = spawn("A")
    task_id = dead.tasks.submit(request())  # процесс A «умер», не запустив задачу дальше
    survivor = spawn("B")
    assert survivor.tasks.recover_interrupted() == []  # аренда ещё жива

    clock.advance(TTL)
    assert survivor.tasks.recover_interrupted() == [task_id]
    snapshot = survivor.tasks.get(task_id)
    assert snapshot.status is S.FAILED
    error = error_of(snapshot)
    assert error.category == "interrupted"
    events = survivor.tasks.trace(task_id)
    assert [event.kind for event in events][-3:] == [
        EventKind.ERROR,
        EventKind.TASK_TRANSITION,
        EventKind.TASK_FINISHED,
    ]
    assert events[-2].payload["reason"] == "interrupted"
    assert lease_of(task_id) is None
    assert survivor.tasks.recover_interrupted() == []  # повторное восстановление ничего не меняет


async def test_running_a_task_of_a_dead_process_does_not_continue_it(
    spawn: Spawn, clock: ManualClock
) -> None:
    script = ScriptedStages([step(S.ROUTING, S.COMPLETED, route="clarify")])
    task_id = spawn("A").tasks.submit(request())
    clock.advance(TTL + 1)
    other = spawn("B", script.handlers())
    snapshot = await other.tasks.run_until_blocked(task_id)
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "interrupted"
    assert script.remaining == 1  # ни одна стадия не вызывалась


async def test_cancelling_an_orphaned_task_records_the_crash(
    spawn: Spawn, clock: ManualClock, lease_of: LeaseOf
) -> None:
    task_id = spawn("A").tasks.submit(request())
    clock.advance(TTL + 1)
    other = spawn("B")
    snapshot = other.tasks.cancel(task_id, "пользователь отменил")
    # Процесс умер раньше, чем пользователь решил отменить: в итоге — сбой, а не отмена.
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "interrupted"
    assert lease_of(task_id) is None


async def test_cancel_returns_the_state_after_the_attempt(spawn: Spawn) -> None:
    app = spawn("A")
    task_id = app.tasks.submit(request())
    assert app.tasks.cancel(task_id, "передумал").status is S.CANCELLED  # своя, ещё не запущенная
    assert app.tasks.cancel(task_id, "ещё раз").status is S.CANCELLED  # завершённая не меняется


async def test_old_owner_cannot_write_after_takeover(spawn: Spawn, clock: ManualClock) -> None:
    hangs = Hangs()
    old = spawn("A", path_with(hangs))
    task_id = old.tasks.submit(request())
    run = asyncio.create_task(old.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)

    clock.advance(TTL + 1)  # A «завис» дольше срока аренды
    assert spawn("B").tasks.recover_interrupted() == [task_id]
    hangs.release.set()  # A просыпается и пытается записать результат такта
    with pytest.raises((ConcurrentModification, LeaseLost)):
        await run
    snapshot = old.tasks.get(task_id)
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "interrupted"
    assert transitions(old, task_id)[-2:] == [S.EXECUTING, S.FAILED]  # запись A не прошла


async def test_heartbeat_detects_a_takeover_and_stops_the_run(spawn: Spawn, clock: ManualClock) -> None:
    hangs = Hangs()
    old = spawn("A", path_with(hangs), ttl_s=0.15)  # heartbeat каждые 0.05 с реального времени
    task_id = old.tasks.submit(request())
    run = asyncio.create_task(old.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)

    clock.advance(1)  # без await: heartbeat A не успевает продлить аренду до восстановления
    assert spawn("B", ttl_s=0.15).tasks.recover_interrupted() == [task_id]
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(run, timeout=5)  # heartbeat не смог продлить аренду и прервал такт
    assert old.tasks.get(task_id).status is S.FAILED


async def test_heartbeat_keeps_a_long_tick_alive(tmp_path: Path) -> None:
    """Реальные часы: такт дольше срока аренды, но heartbeat продлевает её, и чужое восстановление
    задачу не трогает."""
    config = JarvisConfig(runtime=RuntimeSettings(lease_ttl_s=1.0))

    class Slow:
        async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
            await asyncio.sleep(2.5)
            return StageOutcome(next_status=TaskStatus.VERIFYING, reason="долгий шаг")

    script = ScriptedStages([*agent_prefix(), step(S.VERIFYING, S.COMPLETED)])
    stages: dict[TaskStatus, StageHandler] = {**script.handlers(), S.EXECUTING: Slow()}
    with SqliteStorage(tmp_path / "jarvis.db") as first, SqliteStorage(tmp_path / "jarvis.db") as second:
        owner = build_app(config, stages=stages, storage=first, owner="A")
        other = build_app(config, stages={}, storage=second, owner="B")
        task_id = owner.tasks.submit(request())
        run = asyncio.create_task(owner.tasks.run_until_blocked(task_id))
        for _ in range(7):  # всё время такта (дольше двух сроков аренды) аренда жива
            await asyncio.sleep(0.3)
            assert other.tasks.recover_interrupted() == []
        assert (await asyncio.wait_for(run, timeout=10)).status is S.COMPLETED


async def test_heartbeat_failure_does_not_replace_the_result(
    spawn: Spawn, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow:
        async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
            await asyncio.sleep(0.3)
            return StageOutcome(next_status=TaskStatus.VERIFYING, reason="шаг")

    script = ScriptedStages([*agent_prefix(), step(S.VERIFYING, S.COMPLETED)])
    app = spawn("A", {**script.handlers(), S.EXECUTING: Slow()}, ttl_s=0.15)
    task_id = app.tasks.submit(request())
    original_renew = Leases.renew
    calls = 0

    def flaky_renew(self: Leases, lease: Lease) -> Lease:
        nonlocal calls
        calls += 1
        if calls == 2:  # первую проверку при старте пропускаем, ломается heartbeat
            raise RuntimeError("сбой продления")
        return original_renew(self, lease)

    monkeypatch.setattr(Leases, "renew", flaky_renew)
    assert (await asyncio.wait_for(app.tasks.run_until_blocked(task_id), timeout=5)).status is S.COMPLETED
    assert calls >= 2
