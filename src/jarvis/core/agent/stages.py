"""Стадии задачи Architecture V2 (02-domain.md §3, ADR 0009, ADR 0023, ADR 0026).

    запрос → ROUTING: Router решает стратегию без модели
        DIRECT  → EXECUTING: прямая команда (инструмент без модели) → VERIFYING → COMPLETED
        CLARIFY → COMPLETED с уточняющим вопросом
        AGENT   → PLANNING (плана нет) → EXECUTING ⟲ → VERIFYING → COMPLETED

На каждом такте агентного EXECUTING исполнитель просит модель (через Model Gateway) об одном действии.
Модель отвечает только проверенным JSON: вызов инструмента или ответ. Вызов уходит в Tool Runtime — с
preview, политикой, подтверждением, проверкой и аудитом; модель не получает к компьютеру никакого
другого пути. Итог вызова становится наблюдением в рабочей памяти задачи, и на следующем такте
модель видит его блоком DATA. Ответ проверяется детерминированно: ссылаться можно только на
исполненные вызовы этой задачи. Планировщика на модели нет (ADR 0026): PLANNING — проходная стадия.
"""

from collections.abc import Mapping

from pydantic import JsonValue

from jarvis.core.agent.actions import decision_output
from jarvis.core.agent.context import executor_prompt
from jarvis.core.agent.observations import failed_observation, observation_of
from jarvis.core.budget import BudgetMeter
from jarvis.core.direct.stage import DirectStage
from jarvis.core.models.gateway import ModelGateway, ModelRoute
from jarvis.core.routing.router import Router, RoutingStage
from jarvis.core.runner import StageHandler
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.agent import (
    AgentState,
    AgentStep,
    ConsentRequest,
    FinishAction,
    Observation,
    ProposedAction,
    ToolAction,
)
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import (
    InvalidModelOutput,
    InvalidTransition,
    ModelContextExceeded,
    ToolCancelled,
    ToolError,
    VerificationFailed,
)
from jarvis.domain.models import ModelRole
from jarvis.domain.privacy import ANY_PROVIDER, CLOUD_SHARE_TOOL, CloudGrant
from jarvis.domain.routing import CloudMode, Route, RoutingLevel
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.tools import Invoker, ToolOutcome, ToolOutcomeKind
from jarvis.domain.trace import EventKind
from jarvis.ports.storage import UnitOfWorkFactory


class AgentPlanning:
    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        return StageOutcome(
            next_status=TaskStatus.EXECUTING,
            reason="планировщика на модели нет (ADR 0026): агент действует по запросу",
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

        # Служебные инструменты (согласие на облако) модель не видит.
        definitions = [definition for definition in self._tools.definitions() if definition.model_visible]
        output = decision_output(definitions, state.executed_calls())
        route = model_route(task, state)
        room = self._gateway.prompt_budget(ModelRole.EXECUTOR, output, route)
        prompt = executor_prompt(task, state, definitions, budget_tokens=room)
        # Облачному провайдеру уровня мешает только неразрешённый класс данных: спросить человека один
        # раз на задачу (ADR 0028). Отказ — задача продолжается на локальной модели.
        consent = self._gateway.consent_request(ModelRole.EXECUTOR, prompt, output, route)
        if consent is not None and not state.asked(consent):
            return await self._ask_consent(task, budget, state, consent)

        budget.charge(BudgetLimit.STEPS)
        try:
            try:
                generation = await self._gateway.generate(
                    ModelRole.EXECUTOR, prompt, output, task_id=task.id, budget=budget, route=route
                )
            except ModelContextExceeded:
                # Оценка токенов разошлась с токенизатором сервера: ещё раз, с половиной места.
                prompt = executor_prompt(task, state, definitions, budget_tokens=room // 2)
                generation = await self._gateway.generate(
                    ModelRole.EXECUTOR, prompt, output, task_id=task.id, budget=budget, route=route
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
            observation = failed_observation(exc)
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

    async def _ask_consent(
        self, task: Task, budget: BudgetMeter, state: AgentState, consent: ConsentRequest
    ) -> StageOutcome:
        """Спросить человека через Tool Runtime и подтверждения — второго механизма согласия нет."""
        arguments: dict[str, JsonValue] = {
            "provider": consent.provider,
            "data_classes": [item.value for item in consent.data_classes],
        }
        try:
            outcome = await self._tools.call(
                task, budget, CLOUD_SHARE_TOOL, arguments, invoker=Invoker.ROUTER
            )
        except ToolCancelled:
            raise
        except ToolError as exc:  # спросить нечем — продолжаем локально, без облака
            return _stay(
                state.with_consent(consent, granted=False), f"согласие на облако не запрошено: {exc.category}"
            )
        if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
            classes = ", ".join(item.value for item in consent.data_classes)
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason=f"облако: нужно согласие человека отправить {classes} провайдеру {consent.provider}",
                changes=TaskChanges(
                    state=state.with_step(AgentStep(consent=consent, call_id=outcome.call.id))
                ),
            )
        granted = outcome.kind is ToolOutcomeKind.EXECUTED
        return _stay(state.with_consent(consent, granted=granted), _consent_reason(consent, granted))

    async def _resume_consent(
        self, task: Task, budget: BudgetMeter, state: AgentState, consent: ConsentRequest
    ) -> StageOutcome:
        try:
            outcome = await self._tools.resume(task, budget)
        except ToolCancelled:
            raise
        except ToolError:
            outcome = None
        if outcome is not None and outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason="облако: нужно новое согласие человека",
                changes=TaskChanges(state=state),
            )
        granted = outcome is not None and outcome.kind is ToolOutcomeKind.EXECUTED
        reason = _consent_reason(consent, granted)
        observation = Observation(status="executed" if granted else "denied", summary=reason)
        return _stay(state.with_observation(observation).with_consent(consent, granted=granted), reason)

    async def _resume(self, task: Task, budget: BudgetMeter, state: AgentState) -> StageOutcome:
        """Человек решил по вызову: довести его и записать итог."""
        pending = state.pending
        if pending is not None and pending.consent is not None:
            return await self._resume_consent(task, budget, state, pending.consent)
        try:
            outcome = await self._tools.resume(task, budget)
        except ToolCancelled:
            raise
        except ToolError as exc:
            budget.record_failure()
            observation = failed_observation(exc)
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
        if outcome.kind is ToolOutcomeKind.DENIED:
            budget.record_failure()
        return observation_of(outcome)

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


class ByRoute:
    """EXECUTING по стратегии задачи: прямую команду ведёт стадия без модели, остальное — агент."""

    def __init__(self, handlers: Mapping[Route, StageHandler]) -> None:
        self._handlers = dict(handlers)

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        handler = self._handlers.get(task.route) if task.route is not None else None
        if handler is None:
            raise InvalidTransition(f"нет исполнителя для маршрута {task.route}")
        return await handler.handle(task, budget)


def agent_stages(
    *, gateway: ModelGateway, tools: ToolRuntime, router: Router, uow: UnitOfWorkFactory, tracer: Tracer
) -> dict[TaskStatus, StageHandler]:
    """Стадии Architecture V2: ROUTING — Router (без модели), EXECUTING — по стратегии (ADR 0026)."""
    return {
        TaskStatus.ROUTING: RoutingStage(router=router, uow=uow, tracer=tracer),
        TaskStatus.PLANNING: AgentPlanning(),
        TaskStatus.EXECUTING: ByRoute(
            {
                Route.AGENT: Executor(gateway=gateway, tools=tools, uow=uow, tracer=tracer),
                Route.DIRECT: DirectStage(tools=tools, uow=uow, tracer=tracer),
            }
        ),
        TaskStatus.VERIFYING: AnswerVerifier(),
    }


def model_route(task: Task, state: AgentState) -> ModelRoute:
    """Маршрут вызова модели: уровень и режим — из решения Router, разрешения человека на облако — из
    задачи (до конца её). В режиме local_only уровень всегда local."""
    decision = task.routing
    mode = decision.mode if decision is not None else (task.request.mode or CloudMode.AUTO)
    level = decision.level if decision is not None and decision.level is not None else RoutingLevel.LOCAL
    if mode is CloudMode.LOCAL_ONLY:
        level = RoutingLevel.LOCAL
    launch = {CloudGrant(provider=ANY_PROVIDER, data_class=item) for item in task.request.allow_cloud}
    return ModelRoute(
        level=level,
        mode=mode,
        grants=frozenset({*state.grants, *launch}),
        working_directory=task.request.working_directory,
    )


def _consent_reason(consent: ConsentRequest, granted: bool) -> str:
    classes = ", ".join(item.value for item in consent.data_classes)
    if granted:
        return f"человек разрешил отправить {classes} провайдеру {consent.provider} до конца задачи"
    return f"облако без согласия на {classes}: задача продолжается на локальной модели"


def _stay(state: AgentState, reason: str) -> StageOutcome:
    return StageOutcome(next_status=TaskStatus.EXECUTING, reason=reason, changes=TaskChanges(state=state))
