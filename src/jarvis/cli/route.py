"""`jarvis route "<запрос>"`: решение Router без исполнения (ADR 0026).

Показывает стратегию, уровень, намерение, сущности, сработавшие правила, сколько вызовов модели
потребует исполнение и какой инструмент вызвала бы прямая команда. Ничего не исполняется и не
записывается: ни задачи, ни трассы. Время решения Router меряется отдельно от загрузки инвентаря.
"""

import json
import time
from pathlib import Path
from typing import Annotated

import typer

from jarvis.app.composition import build_router
from jarvis.core.direct.commands import direct_call
from jarvis.core.routing.router import decision_payload
from jarvis.core.timeline import clean_line
from jarvis.domain.routing import CloudMode, Route, RouteDecision

MODEL_CALLS = {Route.DIRECT: "0", Route.CLARIFY: "0", Route.AGENT: "1+ (агент на модели)"}


def route_command(
    text: Annotated[list[str], typer.Argument(help="Запрос на естественном языке.")],
    cwd: Annotated[
        Path | None, typer.Option("--cwd", help="Рабочая папка запроса (по умолчанию текущая).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Решение в JSON.")] = False,
    mode: Annotated[
        CloudMode | None, typer.Option("--mode", help="Режим: auto, local_only, smart, coding.")
    ] = None,
) -> None:
    """Показать, как Jarvis исполнил бы запрос: стратегия, намерение, сущности, правила. Без исполнения."""
    request = " ".join(text)
    working_directory = str((cwd or Path.cwd()).resolve())
    loaded = time.perf_counter()
    router = build_router()
    router.warm_up()  # инвентарь читается здесь, а не внутри замера решения
    inventory_ms = (time.perf_counter() - loaded) * 1000
    started = time.perf_counter()
    decision = router.decide(request, working_directory, mode)
    router_ms = (time.perf_counter() - started) * 1000
    call = direct_call(decision) if decision.strategy is Route.DIRECT else None
    if as_json:
        document = {
            **decision_payload(decision, round(router_ms, 3)),
            "model_calls": 0 if decision.strategy is not Route.AGENT else None,
            "tool": {"id": call.tool, "arguments": call.arguments} if call else None,
            "inventory_ms": round(inventory_ms, 1),
        }
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))
        return
    for line in describe(decision, router_ms=router_ms, inventory_ms=inventory_ms):
        typer.echo(line)


def describe(decision: RouteDecision, *, router_ms: float, inventory_ms: float) -> list[str]:
    """Решение для человека. Сущности и текст запроса — данные: управляющие символы экранируются."""
    entities = [
        f"{entity.kind.value}={clean_line(entity.value)} «{clean_line(entity.label)}» ({entity.source})"
        for entity in decision.entities
    ]
    lines = [
        f"strategy:    {decision.strategy.value}",
        f"level:       {decision.level.value if decision.level else '—'}",
        f"mode:        {decision.mode.value}",
        f"intent:      {decision.intent.value if decision.intent else '—'}",
        f"entities:    {'; '.join(entities) or '—'}",
        f"rules:       {', '.join(decision.rules)}",
        f"reason:      {clean_line(decision.reason)}",
    ]
    if decision.question is not None:
        lines.append(f"question:    {clean_line(decision.question)}")
    if decision.strategy is Route.DIRECT:
        call = direct_call(decision)
        arguments = json.dumps(call.arguments, ensure_ascii=False)
        lines.append(f"tool:        {call.tool} {clean_line(arguments)}")
    lines += [
        f"model_calls: {MODEL_CALLS.get(decision.strategy, '?')}",
        f"router:      {router_ms:.2f} мс (инвентарь загружен за {inventory_ms:.0f} мс)",
        "исполнение:  нет — только решение (выполнить: jarvis run)",
    ]
    return lines
