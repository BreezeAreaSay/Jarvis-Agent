"""Метрики задачи (05-storage-and-trace.md §3). Считаются из событий трассы, отдельно не хранятся."""

from pydantic import BaseModel, NonNegativeInt


class TaskMetrics(BaseModel, frozen=True, extra="forbid"):
    duration_ms: NonNegativeInt  # от создания до завершения (у незавершённой — до последнего события)
    active_ms: NonNegativeInt  # время в рабочих состояниях по меткам переходов; без ожидания и простоя
    transitions: NonNegativeInt  # число переходов состояния
    failures: NonNegativeInt  # события error
    replans: NonNegativeInt  # переходы в REPLANNING
    tool_calls: NonNegativeInt = 0  # начатые исполнения инструментов (tool.started); dry run и отказы — нет
    model_calls: NonNegativeInt = 0  # попытки вызова модели (model.called)
    prompt_tokens: NonNegativeInt = 0
    completion_tokens: NonNegativeInt = 0
    finished: bool
