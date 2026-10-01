"""Общие помощники тестов: scripted-приложение и короткая запись шагов сценария."""

from collections.abc import Sequence
from typing import Any

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App, Stages, StagesFactory, build_app
from jarvis.domain.errors import ErrorInfo
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.tools import EffectKind
from jarvis.domain.trace import EventKind
from jarvis.evals.scenario import ScriptStep
from jarvis.evals.scripted import ScriptedStages
from jarvis.ports.tools import Tool
from tests.fakes import FakeTool

S = TaskStatus

# Инструмент, чей вызов всегда требует подтверждения: читает файл с именем секрета.
APPROVAL_TOOL = "test.secret"


def approval_tool() -> FakeTool:
    return FakeTool(APPROVAL_TOOL, effects=((EffectKind.READ, "{path}.pem"),))


def step(status: TaskStatus, next_status: TaskStatus, **fields: Any) -> ScriptStep:
    return ScriptStep.model_validate({"status": status, "next": next_status, **fields})


def approval_step(
    next_status: TaskStatus = S.VERIFYING,
    *,
    path: str = "/keys/server",
    expect: str | None = None,
    **fields: Any,
) -> ScriptStep:
    """Шаг EXECUTING с вызовом, которому нужно подтверждение: задача уходит в WAITING_CONFIRMATION,
    а после решения этот шаг доводит вызов (`expect` — его ожидаемый итог)."""
    tool = {"id": APPROVAL_TOOL, "arguments": {"path": path}, "expect": expect}
    return step(S.EXECUTING, next_status, tool=tool, **fields)


def request(text: str = "сделай что-нибудь") -> TaskRequest:
    return TaskRequest(text=text, origin=Origin.EVAL)


def make_app(
    stages: Stages | StagesFactory,
    *,
    config: JarvisConfig | None = None,
    storage: InMemoryStorage | None = None,
    clock: ManualClock | None = None,
    tools: Sequence[Tool] | None = None,
) -> App:
    return build_app(
        config or JarvisConfig(),
        stages=stages,
        storage=storage if storage is not None else InMemoryStorage(),
        clock=clock if clock is not None else ManualClock(),
        tools=tools if tools is not None else [approval_tool()],
    )


def scripted(
    *steps: ScriptStep,
    config: JarvisConfig | None = None,
    storage: InMemoryStorage | None = None,
    clock: ManualClock | None = None,
) -> tuple[App, ScriptedStages]:
    script = ScriptedStages(steps)
    return make_app(script.handlers, config=config, storage=storage, clock=clock), script


def transitions(app: App, task_id: TaskId) -> list[TaskStatus]:
    return [
        TaskStatus(str(event.payload["to"]))
        for event in app.tasks.trace(task_id)
        if event.kind is EventKind.TASK_TRANSITION
    ]


def agent_prefix() -> list[ScriptStep]:
    """ROUTING → PLANNING → EXECUTING."""
    return [step(S.ROUTING, S.PLANNING, route="agent"), step(S.PLANNING, S.EXECUTING)]


def budget_config(**limits: float) -> JarvisConfig:
    agent = JarvisConfig().budgets.agent.model_copy(update=limits)
    return JarvisConfig.model_validate({"budgets": {"agent": agent.model_dump()}})


def error_of(snapshot: TaskSnapshot) -> ErrorInfo:
    assert snapshot.outcome is not None
    assert snapshot.outcome.error is not None
    return snapshot.outcome.error
