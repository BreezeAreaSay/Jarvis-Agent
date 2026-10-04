"""Стадия EXECUTING для прямых команд (ADR 0026, ADR 0030).

Модели у этой стадии нет вовсе — ей не передают Model Gateway, а бюджет маршрута `direct` не допускает
ни одного вызова модели. Такт: аргументы из решения Router → `ToolRuntime.call` с исполнителем DIRECT
(политика различает прямую команду и предложение модели) → ответ по шаблону → VERIFYING. Подтверждение
человека, отказ и dry run проходят тот же путь, что у агента.
"""

from pydantic import JsonValue

from jarvis.core.agent.observations import observation_of
from jarvis.core.budget import BudgetMeter
from jarvis.core.direct.commands import answer, direct_call
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.agent import AgentState, AgentStep, FinishAction, ProposedAction, ToolAction
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import InvalidTransition, ToolDenied
from jarvis.domain.routing import Route, RouteDecision
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.tools import Invoker, PolicyOutcome, ToolOutcome, ToolOutcomeKind
from jarvis.domain.trace import EventKind
from jarvis.ports.storage import UnitOfWorkFactory


class DirectStage:
    def __init__(self, *, tools: ToolRuntime, uow: UnitOfWorkFactory, tracer: Tracer) -> None:
        self._tools = tools
        self._uow = uow
        self._tracer = tracer

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        decision = task.routing
        if decision is None or decision.strategy is not Route.DIRECT or decision.intent is None:
            raise InvalidTransition("прямая команда без решения Router")
        state = task.state or AgentState()
        if state.pending is not None:  # человек решил по вызову: довести его
            outcome = await self._tools.resume(task, budget)
            if outcome is None:
                raise ToolDenied("решения по вызову нет: прямая команда не исполнялась")
            if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
                return StageOutcome(
                    next_status=TaskStatus.WAITING_CONFIRMATION,
                    reason=f"{outcome.call.tool_id}: затронутое изменилось, нужно новое подтверждение",
                    changes=TaskChanges(state=state),
                )
            return self._finish(decision, state, outcome)

        budget.charge(BudgetLimit.STEPS)
        call = direct_call(decision)
        proposal = ProposedAction(
            decision=shorten(f"прямая команда {decision.intent.value}: {decision.reason}", 280),
            action=ToolAction(type="tool", tool=call.tool, arguments=call.arguments),
        )
        self._proposed(task, proposal)
        outcome = await self._tools.call(task, budget, call.tool, call.arguments, invoker=Invoker.DIRECT)
        step = AgentStep(proposal=proposal, call_id=outcome.call.id)
        if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason=f"{call.tool}: нужно подтверждение человека",
                changes=TaskChanges(state=state.with_step(step)),
            )
        return self._finish(decision, state.with_step(step), outcome)

    def _finish(self, decision: RouteDecision, state: AgentState, outcome: ToolOutcome) -> StageOutcome:
        observation = observation_of(outcome)
        state = state.with_observation(observation)
        tool = outcome.call.tool_id
        match outcome.kind:
            case ToolOutcomeKind.EXECUTED:
                assert outcome.result is not None
                text = answer(decision, outcome.result.output)
                evidence: list[str] = [outcome.call.id]
            case ToolOutcomeKind.DRY_RUN:
                ask = outcome.decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
                text = f"Dry run: {outcome.preview.summary} — не исполнялось" + (
                    " (понадобилось бы подтверждение человека)." if ask else "."
                )
                evidence = list[str]()
            case ToolOutcomeKind.DENIED:
                # Отказ прямой команде — итог задачи: модели, которая искала бы обход, здесь нет.
                raise ToolDenied(
                    f"{tool}: не исполнено — {observation.summary}: {outcome.decision.reason}",
                    rules=list[JsonValue](outcome.decision.rules),
                )
            case ToolOutcomeKind.NEEDS_APPROVAL:
                raise AssertionError("ожидание подтверждения обработано выше")
        finish = ProposedAction(
            decision="ответ по шаблону прямой команды",
            action=FinishAction(type="finish", answer=text, evidence=evidence),
        )
        return StageOutcome(
            next_status=TaskStatus.VERIFYING,
            reason=f"{tool}: прямая команда исполнена" if evidence else f"{tool}: dry run",
            changes=TaskChanges(state=state.with_step(AgentStep(proposal=finish))),
        )

    def _proposed(self, task: Task, proposal: ProposedAction) -> None:
        action = proposal.action
        assert isinstance(action, ToolAction)
        payload: dict[str, JsonValue] = {
            "step": 1,
            "type": "tool",
            "tool": action.tool,
            "decision": proposal.decision,
            "origin": "direct",
        }
        event = self._tracer.event(task.id, EventKind.ACTION_PROPOSED, payload)
        with self._uow() as uow:
            uow.trace.append([event])
            uow.commit()
