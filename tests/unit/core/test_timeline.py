from datetime import UTC

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.core.timeline import render_timeline
from tests.helpers import S, budget_config, request, scripted, step

pytestmark = pytest.mark.anyio


async def test_timeline_snapshot() -> None:
    clock = ManualClock()
    app, _ = scripted(
        step(S.ROUTING, S.PLANNING, route="agent", reason="маршрут agent"),
        step(S.PLANNING, S.EXECUTING, reason="план принят"),
        step(S.EXECUTING, S.EXECUTING, charge={"steps": 2}),
        step(S.EXECUTING, S.EXECUTING, charge={"steps": 1}),
        clock=clock,
        config=budget_config(max_steps=2),
    )
    task_id = app.tasks.submit(request("найди причину"))
    await app.tasks.run_until_blocked(task_id)
    inspection = app.tasks.inspect(task_id)
    text = render_timeline(inspection.task, inspection.events, inspection.metrics, tz=UTC)
    assert text == (
        "TASK task_1  BUDGET_EXCEEDED  route=agent\n"
        "\n"
        "00:00:00 CREATED\n"
        "          request=«найди причину»\n"
        "          origin=eval\n"
        "\n"
        "00:00:00 ROUTING\n"
        "          reason=задача принята\n"
        "\n"
        "00:00:00 PLANNING\n"
        "          route=agent\n"
        "          reason=маршрут agent\n"
        "\n"
        "00:00:00 EXECUTING\n"
        "          reason=план принят\n"
        "\n"
        "00:00:00 ! budget\n"
        "          steps: 3 при максимуме 2\n"
        "\n"
        "00:00:00 BUDGET_EXCEEDED\n"
        "          reason=превышен лимит steps: 3 при максимуме 2\n"
        "\n"
        "00:00:00 FINISHED BUDGET_EXCEEDED\n"
        "\n"
        "METRICS завершена · длительность 0.0 с · активно 0.0 с · переходов 4 · ошибок 0 · "
        "перепланирований 0 · вызовов модели 0 · инструментов 0 · токенов 0\n"
    )
