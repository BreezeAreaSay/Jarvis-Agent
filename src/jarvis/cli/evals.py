"""`jarvis eval`: сценарии eval в scripted-режиме (07-evals-and-benchmarks.md)."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from jarvis.domain.settings import JarvisConfig
from jarvis.evals.engine import run_scenarios
from jarvis.evals.report import write_report
from jarvis.evals.routing import RoutingReport, load_routing_datasets
from jarvis.evals.scenario import ScenarioError, load_scenarios

DEFAULT_PATHS = (Path("evals/scenarios"), Path("evals/routing"))
DEFAULT_REPORTS = Path("evals/reports")


def eval_command(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Файлы или папки сценариев и наборов фраз (по умолчанию evals/scenarios, evals/routing)."
        ),
    ] = None,
    scenario: Annotated[
        list[str] | None, typer.Option("--scenario", "-s", help="Запустить только сценарии с этими ID.")
    ] = None,
    report_dir: Annotated[
        Path, typer.Option("--report-dir", help="Куда записать отчёт (JSON и Markdown).")
    ] = DEFAULT_REPORTS,
) -> None:
    """Прогнать сценарии eval в scripted-режиме и наборы фраз Router."""
    try:
        scenarios = load_scenarios(paths or DEFAULT_PATHS)
        routing = load_routing_datasets(paths or DEFAULT_PATHS)
    except ScenarioError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(2) from None
    if scenario:
        unknown = set(scenario) - {item.id for item in scenarios}
        if unknown:
            typer.echo(f"нет сценариев: {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(2)
        scenarios = [item for item in scenarios if item.id in scenario]
        routing = []
    if not scenarios and not routing:
        typer.echo("сценарии не найдены", err=True)
        raise typer.Exit(2)

    # Eval не зависит от конфига пользователя: только умолчания и параметры сценария.
    result = asyncio.run(run_scenarios(scenarios, JarvisConfig(), routing))
    for item in result.results:
        mark = "✓" if item.passed else "✗"
        typer.echo(f"{mark} {item.id:<28} {item.status:<16} {item.duration_ms} мс")
        for problem in item.problems:
            typer.echo(f"    {problem}")
    for report in result.routing:
        typer.echo(routing_line(report))
        for case in report.failures:
            if case.verdict != "missed_direct":
                typer.echo(f"    {case.verdict}: «{case.text}» → {case.actual} {case.actual_intent or ''}")
        for problem in report.problems:
            typer.echo(f"    {problem}")
    passed = sum(item.passed for item in result.results)
    typer.echo(f"Итого: {passed} из {len(result.results)} сценариев прошли.")
    json_path, markdown_path = write_report(result, report_dir)
    typer.echo(f"Отчёт: {json_path}, {markdown_path}")
    if not result.passed:
        raise typer.Exit(1)


def routing_line(report: RoutingReport) -> str:
    mark = "✓" if report.passed else "✗"
    return (
        f"{mark} {report.id:<28} фраз {report.cases} · ложных DIRECT {report.false_direct} · "
        f"не того действия {report.wrong_direct} · полнота DIRECT {report.direct_recall:.1%} "
        f"({report.direct_hit}/{report.direct_expected}) · p95 {report.p95_ms:.2f} мс"
    )
