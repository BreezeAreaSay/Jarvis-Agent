from datetime import UTC

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import build_app
from jarvis.core.timeline import render_timeline
from jarvis.domain.approvals import ApprovalDecision
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.models import ModelReply, ScriptedModel
from tests.fakes import FakeTool
from tests.helpers import S, approval_step, budget_config, request, scripted, step

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


async def test_tool_events_in_the_timeline() -> None:
    clock = ManualClock()
    app, _ = scripted(
        step(S.ROUTING, S.EXECUTING, route="direct"),
        approval_step(S.COMPLETED, path="/keys/a", expect="executed", answer="готово"),
        clock=clock,
    )
    task_id = app.tasks.submit(request("прочитай ключ"))
    await app.tasks.run_until_blocked(task_id)
    [approval] = app.tasks.approvals(task_id)
    app.tasks.resolve_approval(approval.id, ApprovalDecision.APPROVE, via="cli")
    await app.tasks.run_until_blocked(task_id)
    inspection = app.tasks.inspect(task_id)
    text = render_timeline(inspection.task, inspection.events, inspection.metrics, tz=UTC)
    tool_lines = [
        line for line in text.splitlines() if " tool " in line or "policy" in line or "approval" in line
    ]
    assert tool_lines == [
        "00:00:00 tool test.secret  task_1.call_1",
        "00:00:00 policy require_approval  (zone.secrets.read)",
        "00:00:00 ? approval task_1.appr_1",
        "00:00:00 approval task_1.appr_1 approved via cli",
        "00:00:00 tool test.secret  task_1.call_1",
        "00:00:00 policy allow  (approval.approved, zone.secrets.read)",
        "00:00:00 tool started  task_1.call_1",
        "00:00:00 tool succeeded  task_1.call_1  0 мс",
    ]
    assert "effects=read /keys/a.pem" in text
    assert "результат 15 байт, недоверенные данные" in text
    assert "verify passed  task_1.call_1" in text
    assert inspection.metrics.tool_calls == 1
    assert text.rstrip().endswith("инструментов 1 · токенов 0")


async def test_control_characters_from_data_are_escaped() -> None:
    app, _ = scripted(step(S.ROUTING, S.COMPLETED, route="clarify"))
    task_id = app.tasks.submit(request("имя\x1b[31mКРАСНОЕ\x07"))
    await app.tasks.run_until_blocked(task_id)
    inspection = app.tasks.inspect(task_id)
    text = render_timeline(inspection.task, inspection.events, inspection.metrics, tz=UTC)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "request=«имя\\x1b[31mКРАСНОЕ\\x07»" in text


async def test_agent_steps_in_the_timeline() -> None:
    call = {"type": "tool", "tool": "fake.read", "arguments": {"path": "/data/a.txt"}}
    done = {"type": "finish", "answer": "готово", "evidence": ["task_1.call_1"]}
    model = ScriptedModel(
        [
            ModelReply(text="не JSON"),
            ModelReply.model_validate({"json": {"decision": "посмотрю\x1b[31m файл", "action": call}}),
            ModelReply.model_validate({"json": {"decision": "отвечаю", "action": done}}),
        ]
    )
    app = build_app(
        JarvisConfig(),
        models={ModelRole.EXECUTOR: model},
        storage=InMemoryStorage(),
        clock=ManualClock(),
        tools=[FakeTool("fake.read")],
    )
    task_id = app.tasks.submit(request("что в файле?"))
    await app.tasks.run_until_blocked(task_id)
    inspection = app.tasks.inspect(task_id)
    text = render_timeline(inspection.task, inspection.events, inspection.metrics, tz=UTC)
    assert "00:00:00 model executor invalid  task_1.mc_1  попытка 1  " in text
    assert "          ответ — не JSON: Expecting value (символ 0)\n" in text
    assert "00:00:00 STEP 1  tool fake.read\n          «посмотрю\\x1b[31m файл»\n" in text
    assert "00:00:00 STEP 2  finish ответ\n          «отвечаю»\n" in text
    assert inspection.metrics.model_calls == 3
    assert inspection.metrics.prompt_tokens > 0
    assert "вызовов модели 3 · инструментов 1 · токенов " in text
