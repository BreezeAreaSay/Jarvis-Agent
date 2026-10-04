"""Точка входа `jarvis`: --version, config, eval, bench, run, route, model, tasks, trace, cancel, tools.

Команды загружаются лениво (`LazyGroup`): модуль команды импортируется, только когда её вызвали. Так
`jarvis --version` не грузит ничего, кроме Typer, а `jarvis run` и `jarvis route` — бенчмарк и eval.
"""

import importlib
import io
import json
import sys
from collections.abc import Iterator, Mapping
from importlib.metadata import version as package_version
from typing import Annotated, Any

import typer
from typer import _click as click  # Typer строит команды на своей копии click
from typer.core import TyperGroup

from jarvis.cli.common import load_or_exit

# Имя команды → (модуль, объект): функция команды или Typer группы команд.
LAZY_COMMANDS: dict[str, tuple[str, str]] = {
    "run": ("jarvis.cli.run", "run_command"),
    "route": ("jarvis.cli.route", "route_command"),
    "eval": ("jarvis.cli.evals", "eval_command"),
    "tasks": ("jarvis.cli.tasks", "tasks_command"),
    "trace": ("jarvis.cli.tasks", "trace_command"),
    "cancel": ("jarvis.cli.tasks", "cancel_command"),
    "model": ("jarvis.cli.models", "model_app"),
    "tools": ("jarvis.cli.tools", "tools_app"),
    "bench": ("jarvis.cli.bench", "bench_app"),
}


class LazyGroup(TyperGroup):
    """Группа, которая импортирует модуль команды при первом обращении к ней."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *LAZY_COMMANDS})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        found = super().get_command(ctx, cmd_name)
        if found is not None or cmd_name not in LAZY_COMMANDS:
            return found
        module, attribute = LAZY_COMMANDS[cmd_name]
        target = getattr(importlib.import_module(module), attribute)
        if isinstance(target, typer.Typer):  # группа команд: группой и остаётся, даже из одной команды
            command: click.Command = typer.main.get_group(target)
        else:  # одиночная команда — функция
            single = typer.Typer()
            single.command(cmd_name)(target)
            command = typer.main.get_command(single)
        command.name = cmd_name
        self.commands[cmd_name] = command
        return command


app = typer.Typer(
    cls=LazyGroup, add_completion=False, no_args_is_help=True, help="Jarvis — локальный Agent Runtime."
)
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
