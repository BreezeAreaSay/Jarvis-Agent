"""Состояние задачи живёт в хранилище: новое приложение над тем же хранилищем продолжает её."""

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from tests.helpers import S, agent_prefix, request, scripted, step

pytestmark = pytest.mark.anyio


async def test_rebuilt_app_sees_the_same_task() -> None:
    storage = InMemoryStorage()
    clock = ManualClock()
    first, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.WAITING_CONFIRMATION, charge={"steps": 1, "tool_calls": 1}),
        storage=storage,
        clock=clock,
    )
    task_id = first.tasks.submit(request())
    waiting = await first.tasks.run_until_blocked(task_id)

    second, _ = scripted(storage=storage, clock=clock)
    assert second.tasks.get(task_id) == waiting
    assert second.tasks.trace(task_id) == first.tasks.trace(task_id)
