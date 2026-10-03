"""Точка входа `jarvis`: --version, config, eval, run, model, tasks, trace, cancel, tools."""

import asyncio
import io
import json
import sys
from collections.abc import Iterator, Mapping
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any

import typer

from jarvis.cli.common import load_or_exit
from jarvis.cli.models import model_app
from jarvis.cli.run import run_command
from jarvis.cli.tasks import cancel_command, tasks_command, trace_command
from jarvis.cli.tools import tools_app
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.engine import run_scenarios
from jarvis.evals.report import write_report
from jarvis.evals.scenario import ScenarioError, load_scenarios

DEFAULT_SCENARIOS = Path("evals/scenarios")
DEFAULT_REPORTS = Path("evals/reports")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Jarvis — локальный Agent Runtime.")
config_app = typer.Typer(no_args_is_help=True, help="Конфигурация: проверка и итоговые значения.")
app.add_typer(config_app, name="config")
app.command("run")(run_command)
app.add_typer(model_app, name="model")
app.command("tasks")(tasks_command)
app.command("trace")(trace_command)
app.command("cancel")(cancel_command)
app.add_typer(tools_app, name="tools")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"jarvis {package_version('jarvis-agent')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Показать версию и выйти."),
    ] = False,
) -> None:
    """Jarvis — локальный Agent Runtime."""


@config_app.command("check")
def config_check() -> None:
    """Проверить конфиг: синтаксис, схему, неизвестные ключи."""
    loaded = load_or_exit()
    if loaded.config_file_exists:
        typer.echo(f"Конфиг в порядке: {loaded.config_path}")
    else:
        typer.echo(f"Файла конфига нет ({loaded.config_path}) — действуют умолчания.")


@config_app.command("show")
def config_show(
    sources: Annotated[
        bool, typer.Option("--sources", help="Показать слой, откуда пришло значение.")
    ] = False,
) -> None:
    """Показать итоговые значения конфига."""
    loaded = load_or_exit()
    if sources:
        typer.echo(f"# JARVIS_HOME: {loaded.home}")
        typer.echo(f"# config: {loaded.config_path}{'' if loaded.config_file_exists else ' (нет файла)'}")
    for key, value in _leaves(loaded.config.model_dump(mode="json")):
        line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        if sources:
            line += f"  [{loaded.sources.get(key, 'default')}]"
        typer.echo(line)


@app.command("eval")
def eval_command(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Файлы или папки сценариев (по умолчанию evals/scenarios).")
    ] = None,
    scenario: Annotated[
        list[str] | None, typer.Option("--scenario", "-s", help="Запустить только сценарии с этими ID.")
    ] = None,
    report_dir: Annotated[
        Path, typer.Option("--report-dir", help="Куда записать отчёт (JSON и Markdown).")
    ] = DEFAULT_REPORTS,
) -> None:
    """Прогнать сценарии eval в scripted-режиме."""
    try:
        scenarios = load_scenarios(paths or [DEFAULT_SCENARIOS])
    except ScenarioError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(2) from None
    if scenario:
        unknown = set(scenario) - {item.id for item in scenarios}
        if unknown:
            typer.echo(f"нет сценариев: {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(2)
        scenarios = [item for item in scenarios if item.id in scenario]
    if not scenarios:
        typer.echo("сценарии не найдены", err=True)
        raise typer.Exit(2)

    # Eval не зависит от конфига пользователя: только умолчания и параметры сценария.
    result = asyncio.run(run_scenarios(scenarios, JarvisConfig()))
    for item in result.results:
        mark = "✓" if item.passed else "✗"
        typer.echo(f"{mark} {item.id:<28} {item.status:<16} {item.duration_ms} мс")
        for problem in item.problems:
            typer.echo(f"    {problem}")
    passed = sum(item.passed for item in result.results)
    typer.echo(f"Итого: {passed} из {len(result.results)} сценариев прошли.")
    json_path, markdown_path = write_report(result, report_dir)
    typer.echo(f"Отчёт: {json_path}, {markdown_path}")
    if not result.passed:
        raise typer.Exit(1)


def _leaves(data: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, f"{path}.")  # pyright: ignore[reportUnknownArgumentType]
        else:
            yield path, value


def run() -> None:
    # Вывод в канал (CI, перенаправление в файл) — в UTF-8, независимо от кодировки системы.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app()
