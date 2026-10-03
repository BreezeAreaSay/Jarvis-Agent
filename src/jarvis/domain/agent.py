"""Действия агента и его рабочая память (02-domain.md §2, ADR 0009, ADR 0023).

Модель за один шаг предлагает одно действие: вызвать инструмент или закончить с ответом. Плана пока
нет (планировщик — M8), поэтому нет и действий `step_done` и `replan`. Рабочая память — часть задачи:
продолжить задачу (например, после подтверждения) может другой процесс.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

DECISION_CHARS = 280  # зачем это действие — коротко, не рассуждения (ADR 0015)
ANSWER_CHARS = 4000


class ToolAction(BaseModel, frozen=True, extra="forbid"):
    type: Literal["tool"]
    tool: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = {}


class FinishAction(BaseModel, frozen=True, extra="forbid"):
    type: Literal["finish"]
    answer: str = Field(min_length=1, max_length=ANSWER_CHARS)
    evidence: list[str] = []  # ID вызовов, на результатах которых основан ответ


Action = Annotated[ToolAction | FinishAction, Field(discriminator="type")]


class ProposedAction(BaseModel, frozen=True, extra="forbid"):
    decision: str = Field(min_length=1, max_length=DECISION_CHARS)
    action: Action


ObservationStatus = Literal["executed", "denied", "dry_run", "failed"]


class Observation(BaseModel, frozen=True, extra="forbid"):
    """Что вернул вызов. `summary` пишет Jarvis; `data` — вывод инструмента или текст ошибки: это
    данные извне, в промпт они попадают только блоком DATA. `sensitive` — прочитано из зоны секретов
    (с разрешения человека): такие данные не сохраняются в журналах и удаляются, когда задача
    завершается."""

    status: ObservationStatus
    summary: str
    data: str | None = None
    sensitive: bool = False


class AgentStep(BaseModel, frozen=True, extra="forbid"):
    """Шаг агента: принятое действие и его итог или отвергнутый ответ модели."""

    proposal: ProposedAction | None = None  # None — ответ модели не прошёл проверку
    problems: list[str] = []  # почему не прошёл: модель увидит это на следующем шаге
    call_id: str | None = None  # вызов инструмента этого шага
    observation: Observation | None = None  # None у вызова, который ждёт решения человека


class AgentState(BaseModel, frozen=True, extra="forbid"):
    steps: list[AgentStep] = []

    @property
    def pending(self) -> AgentStep | None:
        """Шаг, чей вызов ждёт решения человека."""
        last = self.steps[-1] if self.steps else None
        if last is not None and last.call_id is not None and last.observation is None:
            return last
        return None

    @property
    def answer(self) -> FinishAction | None:
        last = self.steps[-1] if self.steps else None
        if last is not None and last.proposal is not None and isinstance(last.proposal.action, FinishAction):
            return last.proposal.action
        return None

    def executed_calls(self) -> list[str]:
        """Вызовы, которые исполнились и прошли проверку: на них может ссылаться ответ."""
        return [
            step.call_id
            for step in self.steps
            if step.call_id is not None
            and step.observation is not None
            and step.observation.status == "executed"
        ]

    def has_secrets(self) -> bool:
        return any(
            step.observation is not None and step.observation.sensitive and step.observation.data is not None
            for step in self.steps
        )

    def without_secrets(self) -> "AgentState":
        """Рабочая память завершённой задачи: данные секретов удалены, остальное — как было."""
        steps: list[AgentStep] = []
        for step in self.steps:
            observation = step.observation
            if observation is not None and observation.sensitive and observation.data is not None:
                observation = observation.model_copy(
                    update={"data": None, "summary": f"{observation.summary}; данные секрета удалены"}
                )
                step = step.model_copy(update={"observation": observation})
            steps.append(step)
        return AgentState(steps=steps)

    def with_step(self, step: AgentStep) -> "AgentState":
        return AgentState(steps=[*self.steps, step])

    def with_observation(self, observation: Observation) -> "AgentState":
        """Итог вызова, ждавшего человека."""
        pending = self.pending
        if pending is None:
            raise ValueError("нет вызова, который ждёт итога")
        return AgentState(steps=[*self.steps[:-1], pending.model_copy(update={"observation": observation})])
