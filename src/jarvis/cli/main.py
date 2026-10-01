"""Точка входа `jarvis`. Команды M1: --version, config check, config show, eval."""

import asyncio
import io
import json
import sys
from collections.abc import Iterator, Mapping
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any

import typer

from jarvis.config import LoadedConfig, load_config
from jarvis.domain.errors import ConfigError
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.engine import run_scenarios
from jarvis.evals.scenario import ScenarioError, load_scenarios

DEFAULT_SCENARIOS = Path("evals/scenarios")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Jarvis — локальный Agent Runtime.")
config_app = typer.Typer(no_args_is_help=True, help="Конфигурация: проверка и итоговые значения.")
app.add_typer(config_app, name="config")


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


def _load() -> LoadedConfig:
    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None


@config_app.command("check")
def config_check() -> None:
    """Проверить конфиг: синтаксис, схему, неизвестные ключи."""
    loaded = _load()
    if loaded.config_file_exists:
        typer.echo(f"Конфиг в порядке: {loaded.config_path}")
    else:
        typer.echo(f"Файла конфига нет ({loaded.config_path}) — действуют умолчания.")


@config_app.command("show")
def config_show(
    sources: Annotated[bool, typer.Option("--sources", help="Показать слой, откуда пришло значение.")] = False,
) -> None:
    """Показать итоговые значения конфига."""
    loaded = _load()
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
    report: Annotated[Path | None, typer.Option("--report", help="Записать отчёт в JSON-файл.")] = None,
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
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"Отчёт: {report}")
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
