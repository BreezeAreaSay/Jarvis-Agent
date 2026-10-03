"""Стадии агента (02-domain.md §3, ADR 0009, ADR 0023).

Первый путь, где задачу ведёт модель:

    запрос → ROUTING (маршрут agent) → PLANNING (плана пока нет) → EXECUTING ⟲ → VERIFYING → COMPLETED

На каждом такте EXECUTING исполнитель просит модель (через Model Gateway) об одном действии. Модель
отвечает только проверенным JSON: вызов инструмента или ответ. Вызов уходит в Tool Runtime — с
preview, политикой, подтверждением, проверкой и аудитом; модель не получает к компьютеру никакого
другого пути. Итог вызова становится наблюдением в рабочей памяти задачи, и на следующем такте
модель видит его блоком DATA. Ответ проверяется детерминированно: ссылаться можно только на
исполненные вызовы этой задачи.

Маршрутизатор (M7) и планировщик (M8) появятся своими milestone; до тех пор ROUTING и PLANNING
переходят дальше без модели и пишут в трассе, почему.
"""

import json

from pydantic import JsonValue

from jarvis.core.agent.actions import decision_output
from jarvis.core.agent.context import executor_prompt
from jarvis.core.budget import BudgetMeter
from jarvis.core.models.gateway import ModelGateway
from jarvis.core.runner import StageHandler
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.agent import AgentState, AgentStep, FinishAction, Observation, ProposedAction, ToolAction
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import (
    InvalidModelOutput,
    ModelContextExceeded,
    ToolCancelled,
    ToolError,
    VerificationFailed,
)
from jarvis.domain.models import ModelRole
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Route, StageOutcome, Task, TaskChanges
from jarvis.domain.tools import ToolOutcome, ToolOutcomeKind
from jarvis.domain.trace import EventKind
from jarvis.ports.storage import UnitOfWorkFactory

OBSERVATION_BYTES = 6000  # результат вызова в рабочей памяти и промпте; больше — обрезается


class AgentRouting:
    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        return StageOutcome(
            next_status=TaskStatus.PLANNING,
            reason="маршрутизатора ещё нет (M7): запрос ведёт агент",
            changes=TaskChanges(route=Route.AGENT),
        )


class AgentPlanning:
    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        return StageOutcome(
            next_status=TaskStatus.EXECUTING, reason="планировщика ещё нет (M8): агент действует по запросу"
        )


class Executor:
    def __init__(
        self, *, gateway: ModelGateway, tools: ToolRuntime, uow: UnitOfWorkFactory, tracer: Tracer
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._uow = uow
        self._tracer = tracer

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        state = task.state or AgentState()
        if state.pending is not None:
            return await self._resume(task, budget, state)

        budget.charge(BudgetLimit.STEPS)
        definitions = self._tools.definitions()
        output = decision_output(definitions, state.executed_calls())
        room = self._gateway.prompt_budget(ModelRole.EXECUTOR, output)
        try:
            try:
                prompt = executor_prompt(task, state, definitions, budget_tokens=room)
                generation = await self._gateway.generate(
                    ModelRole.EXECUTOR, prompt, output, task_id=task.id, budget=budget
                )
            except ModelContextExceeded:
                # Оценка токенов разошлась с токенизатором сервера: ещё раз, с половиной места.
                prompt = executor_prompt(task, state, definitions, budget_tokens=room // 2)
                generation = await self._gateway.generate(
                    ModelRole.EXECUTOR, prompt, output, task_id=task.id, budget=budget
                )
        except InvalidModelOutput as exc:
            budget.record_failure()
            problems = exc.details.get("problems")
            listed = [str(item) for item in problems] if isinstance(problems, list) else [exc.message]
            return _stay(state.with_step(AgentStep(problems=listed)), "ответ модели не прошёл проверку")

        proposal = generation.value
        self._proposed(task, len(state.steps) + 1, proposal, generation.call_id)
        action = proposal.action
        if isinstance(action, FinishAction):
            return StageOutcome(
                next_status=TaskStatus.VERIFYING,
                reason="модель закончила с ответом",
                changes=TaskChanges(state=state.with_step(AgentStep(proposal=proposal))),
            )
        return await self._call(task, budget, state, proposal, action)

    async def _call(
        self, task: Task, budget: BudgetMeter, state: AgentState, proposal: ProposedAction, action: ToolAction
    ) -> StageOutcome:
        try:
            outcome = await self._tools.call(task, budget, action.tool, action.arguments)
        except ToolCancelled:
            raise
        except ToolError as exc:
            budget.record_failure()
            observation = _failed(exc)
            return _stay(
                state.with_step(AgentStep(proposal=proposal, observation=observation)), observation.summary
            )
        step = AgentStep(proposal=proposal, call_id=outcome.call.id)
        if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason=f"{action.tool}: нужно подтверждение человека",
                changes=TaskChanges(state=state.with_step(step)),
            )
        observation = self._observe(outcome, budget)
        return _stay(
            state.with_step(step.model_copy(update={"observation": observation})), observation.summary
        )

    async def _resume(self, task: Task, budget: BudgetMeter, state: AgentState) -> StageOutcome:
        """Человек решил по вызову: довести его и записать итог."""
        try:
            outcome = await self._tools.resume(task, budget)
        except ToolCancelled:
            raise
        except ToolError as exc:
            budget.record_failure()
            observation = _failed(exc)
            return _stay(state.with_observation(observation), observation.summary)
        if outcome is None:
            observation = Observation(status="failed", summary="решения по вызову нет: вызов не исполнялся")
            return _stay(state.with_observation(observation), observation.summary)
        if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:  # затронутое изменилось: новый запрос
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason=f"{outcome.call.tool_id}: затронутое изменилось, нужно новое подтверждение",
                changes=TaskChanges(state=state),
            )
        observation = self._observe(outcome, budget)
        return _stay(state.with_observation(observation), observation.summary)

    def _observe(self, outcome: ToolOutcome, budget: BudgetMeter) -> Observation:
        tool = outcome.call.tool_id
        match outcome.kind:
            case ToolOutcomeKind.EXECUTED:
                assert outcome.result is not None
                data, note = _clip(json.dumps(outcome.result.output, ensure_ascii=False))
                # Прочитанное из зоны секретов (с разрешения человека) не оседает в журналах.
                secret = any(rule.startswith("zone.secrets") for rule in outcome.decision.rules)
                return Observation(
                    status="executed",
                    summary=f"{tool}: исполнен, проверка пройдена{note}",
                    data=data,
                    sensitive=secret,
                )
            case ToolOutcomeKind.DRY_RUN:
                would = "был бы исполнен" if outcome.would_execute else "потребовал бы подтверждения"
                return Observation(status="dry_run", summary=f"{tool}: dry run — не исполнялся ({would})")
            case ToolOutcomeKind.DENIED:
                budget.record_failure()
                rules = outcome.decision.rules
                if "approval.expired" in rules:
                    verdict = "срок подтверждения истёк"
                elif "approval.denied" in rules:
                    verdict = "отказано человеком"
                else:
                    verdict = "отказано политикой"
                # Причина может содержать пути из аргументов — это данные, а не текст Jarvis.
                reason = f"{outcome.decision.reason} [{', '.join(rules)}]"
                return Observation(status="denied", summary=f"{tool}: {verdict}, не исполнен", data=reason)
            case ToolOutcomeKind.NEEDS_APPROVAL:
                raise AssertionError("ожидание подтверждения обрабатывает стадия")

    def _proposed(self, task: Task, number: int, proposal: ProposedAction, call_id: str) -> None:
        action = proposal.action
        payload: dict[str, JsonValue] = {
            "step": number,
            "type": action.type,
            "decision": shorten(proposal.decision, 300),
            "model_call": call_id,
        }
        if isinstance(action, ToolAction):
            payload["tool"] = action.tool
        else:
            payload["evidence"] = list[JsonValue](action.evidence[:10])
        event = self._tracer.event(task.id, EventKind.ACTION_PROPOSED, payload)
        with self._uow() as uow:
            uow.trace.append([event])
            uow.commit()


class AnswerVerifier:
    """Детерминированная проверка ответа: без модели (ADR 0008)."""

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        state = task.state or AgentState()
        answer = state.answer
        if answer is None:
            raise VerificationFailed("в рабочей памяти нет ответа агента")
        executed = state.executed_calls()
        unknown = [ref for ref in answer.evidence if ref not in executed]
        if unknown:
            raise VerificationFailed(
                f"ответ ссылается на вызовы, которых не было: {', '.join(unknown[:5])}",
                evidence=list[JsonValue](unknown[:5]),
            )
        if answer.evidence:
            reason = f"ответ опирается на исполненные вызовы: {', '.join(answer.evidence)}"
        elif executed:
            reason = "ответ не ссылается на вызовы инструментов"
        else:
            reason = "ответ без вызовов инструментов"
        return StageOutcome(
            next_status=TaskStatus.COMPLETED, reason=reason, changes=TaskChanges(answer=answer.answer)
        )


def agent_stages(
    *, gateway: ModelGateway, tools: ToolRuntime, uow: UnitOfWorkFactory, tracer: Tracer
) -> dict[TaskStatus, StageHandler]:
    return {
        TaskStatus.ROUTING: AgentRouting(),
        TaskStatus.PLANNING: AgentPlanning(),
        TaskStatus.EXECUTING: Executor(gateway=gateway, tools=tools, uow=uow, tracer=tracer),
        TaskStatus.VERIFYING: AnswerVerifier(),
    }


def _stay(state: AgentState, reason: str) -> StageOutcome:
    return StageOutcome(next_status=TaskStatus.EXECUTING, reason=reason, changes=TaskChanges(state=state))


def _failed(error: ToolError) -> Observation:
    return Observation(status="failed", summary=f"вызов не удался ({error.category})", data=error.message)


def _clip(text: str) -> tuple[str, str]:
    encoded = text.encode("utf-8")
    if len(encoded) <= OBSERVATION_BYTES:
        return text, ""
    clipped = encoded[:OBSERVATION_BYTES].decode("utf-8", errors="ignore")
    return clipped, f"; результат обрезан: {OBSERVATION_BYTES} из {len(encoded)} байт"
