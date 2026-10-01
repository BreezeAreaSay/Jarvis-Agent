"""Контракт инструмента (03-contracts.md §2, ADR 0004, ADR 0022).

Три отдельные стадии: `preview` (только чтение: что будет затронуто), `execute` (единственное место
эффектов), `verify` (постусловие). Инструмент не пишет трассу и аудит и не видит хранилища: всё это
делает Tool Runtime — единственная граница исполнения.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from jarvis.domain.tools import ExecutionTarget, ToolDefinition, ToolPreview, ToolVerification


@dataclass(frozen=True)
class ToolContext:
    target: ExecutionTarget
    working_directory: str | None  # от неё считаются относительные пути
    protected_roots: tuple[str, ...] = ()  # канонические пути, куда обход (поиск) не спускается


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        """Без побочных эффектов. Нормализует аргументы (канонические пути) и называет эффекты."""
        ...

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        """Получает нормализованные аргументы из preview; возвращает экземпляр output_model."""
        ...

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        """Постусловие вызова, без LLM."""
        ...
