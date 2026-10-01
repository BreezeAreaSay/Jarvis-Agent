"""Ошибки стадий и недопустимые результаты: задача завершается FAILED с понятной причиной."""

import logging

import pytest

from jarvis.core.budget import BudgetMeter
from jarvis.domain.errors import Disposition, JarvisError
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.trace import EventKind
from jarvis.evals.scenario import ScriptStep
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, agent_prefix, error_of, make_app, request, scripted, step, transitions

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([step(S.ROUTING, S.PLANNING, route="agent"), step(S.PLANNING, S.COMPLETED)], "PLANNING → COMPLETED"),
        ([step(S.ROUTING, S.ROUTING)], "должен сменить состояние"),
        ([*agent_prefix(), step(S.EXECUTING, S.VERIFYING, route="chat")], "только в ROUTING"),
    ],
)
async def test_invalid_stage_outcome_fails_the_task(steps: list[ScriptStep], message: str) -> None:
    app, _ = scripted(*steps)
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.FAILED
    error = error_of(snapshot)
    assert error.category == "invalid_transition"
    assert message in error.message
    errors = [event for event in app.tasks.trace(task_id) if event.kind is EventKind.ERROR]
    assert [event.payload["category"] for event in errors] == ["invalid_transition"]


async def test_state_without_handler_fails_the_task() -> None:
    script = ScriptedStages([step(S.ROUTING, S.PLANNING, route="agent")])
    handlers = script.handlers()
    del handlers[S.PLANNING]
    app = make_app(handlers)
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.FAILED
    error = error_of(snapshot)
    assert "нет обработчика" in error.message


async def test_jarvis_error_from_stage_keeps_its_category() -> None:
    app, _ = scripted(*agent_prefix(), step(S.EXECUTING, S.VERIFYING, error="fatal", reason="сбой"))
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert transitions(app, task_id)[-1] is S.FAILED
    error = error_of(snapshot)
    assert error.category == "scripted_failure"
    assert error.message == "сбой"


class FeedbackError(JarvisError):
    category = "feedback_case"
    disposition = Disposition.FEEDBACK


class Raising:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        raise self.error


async def test_error_escaping_the_stage_ends_the_task_whatever_its_disposition() -> None:
    app = make_app({TaskStatus.ROUTING: Raising(FeedbackError("модель ошиблась"))})
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)
    error = error_of(snapshot)
    assert error.category == "feedback_case"


async def test_unknown_exception_is_internal_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    app = make_app({TaskStatus.ROUTING: Raising(KeyError("missing"))})
    task_id = app.tasks.submit(request())
    with caplog.at_level(logging.ERROR, logger="jarvis.core.runner"):
        snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.FAILED
    error = error_of(snapshot)
    assert error.category == "internal"
    assert error.message == "KeyError: 'missing'"
    assert "необработанное исключение" in caplog.text


class Returning:
    def __init__(self, outcome: StageOutcome) -> None:
        self.outcome = outcome

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        return self.outcome


async def test_answer_is_kept_only_in_the_terminal_outcome() -> None:
    outcome = StageOutcome(next_status=S.COMPLETED, reason="ok", changes=TaskChanges(answer="да"))
    app = make_app({TaskStatus.ROUTING: Returning(outcome)})
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "да"
