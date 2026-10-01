"""Трасса, уникальность ID, атомарность контрольной точки и детерминизм прогона."""

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.domain.errors import StorageError
from jarvis.domain.states import ALLOWED_TRANSITIONS, TaskStatus
from jarvis.domain.trace import EventKind
from jarvis.evals.scenario import ScriptStep
from tests.helpers import S, agent_prefix, request, scripted, step, transitions

pytestmark = pytest.mark.anyio


def full_path() -> list[ScriptStep]:
    return [
        *agent_prefix(),
        step(S.EXECUTING, S.EXECUTING, charge={"steps": 1}),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.COMPLETED, answer="готово"),
    ]


async def test_trace_records_every_transition_in_order() -> None:
    app, _ = scripted(*full_path())
    task_id = app.tasks.submit(request("найди причину"))
    await app.tasks.run_until_blocked(task_id)
    events = app.tasks.trace(task_id)

    assert [event.kind for event in events] == [
        EventKind.TASK_CREATED,
        *[EventKind.TASK_TRANSITION] * 5,
        EventKind.TASK_FINISHED,
    ]
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert [event.id for event in events] == [f"{task_id}.ev_{n}" for n in range(1, len(events) + 1)]
    assert events[0].payload == {"text": "найди причину", "origin": "eval"}
    assert events[-1].payload == {"status": "COMPLETED", "answer": "готово"}

    moves = [event.payload for event in events if event.kind is EventKind.TASK_TRANSITION]
    previous = TaskStatus.CREATED
    for move in moves:  # цепочка без разрывов, каждый переход допустим
        assert move["from"] == previous
        target = TaskStatus(str(move["to"]))
        assert target in ALLOWED_TRANSITIONS[previous]
        previous = target


async def test_ids_are_unique_across_tasks() -> None:
    app, _ = scripted(*full_path(), *full_path(), *full_path())
    ids = [app.tasks.submit(request()) for _ in range(3)]
    for task_id in ids:
        await app.tasks.run_until_blocked(task_id)

    assert ids == ["task_1", "task_2", "task_3"]
    event_ids = [event.id for task_id in ids for event in app.tasks.trace(task_id)]
    assert len(event_ids) == len(set(event_ids))


async def test_checkpoint_is_atomic_and_ids_are_not_reused() -> None:
    storage = InMemoryStorage()
    app, _ = scripted(*full_path(), storage=storage)
    task_id = app.tasks.submit(request())  # task.created — ev_1

    storage.fail_commit()  # контрольная точка CREATED → ROUTING (ev_2) не запишется
    with pytest.raises(StorageError):
        await app.tasks.run_until_blocked(task_id)
    snapshot = app.tasks.get(task_id)
    assert (snapshot.status, snapshot.version) == (S.CREATED, 1)
    assert [event.id for event in app.tasks.trace(task_id)] == [f"{task_id}.ev_1"]

    await app.tasks.run_until_blocked(task_id)
    event_ids = [event.id for event in app.tasks.trace(task_id)]
    assert f"{task_id}.ev_2" not in event_ids  # номер сорвавшейся записи не выдаётся повторно
    assert event_ids[1] == f"{task_id}.ev_3"
    assert transitions(app, task_id)[0] is S.ROUTING


async def test_journal_events_survive_a_failed_checkpoint() -> None:
    storage = InMemoryStorage()
    app, _ = scripted(*agent_prefix(), step(S.EXECUTING, S.VERIFYING, fail=True), storage=storage)
    task_id = app.tasks.submit(request())
    # После submit успешны ещё четыре записи (ROUTING, PLANNING, EXECUTING и журнальное событие
    # error); пятая — контрольная точка перехода в FAILED — падает.
    storage.fail_commit(after=4)
    with pytest.raises(StorageError):
        await app.tasks.run_until_blocked(task_id)

    assert app.tasks.get(task_id).status is S.EXECUTING
    kinds = [event.kind for event in app.tasks.trace(task_id)]
    assert kinds[-1] is EventKind.ERROR  # журнальное событие записано и не откатилось
    assert transitions(app, task_id) == [S.ROUTING, S.PLANNING, S.EXECUTING]


async def test_same_scenario_gives_identical_runs() -> None:
    async def run_once() -> tuple[object, list[object]]:
        app, _ = scripted(*full_path(), clock=ManualClock())
        task_id = app.tasks.submit(request())
        snapshot = await app.tasks.run_until_blocked(task_id)
        return snapshot, [event.model_dump() for event in app.tasks.trace(task_id)]

    first = await run_once()
    second = await run_once()
    assert first == second
