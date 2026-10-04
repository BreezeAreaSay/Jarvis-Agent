"""Команды инструментов: `jarvis tools` и `jarvis tools show <id>` — только описание.

Исполнить инструмент из CLI в обход задачи нельзя: вызов возможен только из стадии задачи через
Tool Runtime, с preview, политикой, трассой и аудитом.
"""

import json
from typing import Annotated

import typer

from jarvis.app.composition import build_app
from jarvis.cli.common import load_or_exit
from jarvis.domain.tools import ToolDefinition, ToolId

tools_app = typer.Typer(invoke_without_command=True, help="Инструменты: список и описание.")


def _definitions() -> list[ToolDefinition]:
    loaded = load_or_exit()
    app = build_app(loaded.config, stages={}, home=loaded.home, config_file=loaded.config_path)
    return app.tools.definitions()


@tools_app.callback()
def tools_list(context: typer.Context) -> None:
    """Список инструментов: ID, возможные эффекты, краткое описание."""
    if context.invoked_subcommand is not None:
        return
    for definition in _definitions():
        effects = ",".join(sorted(kind.value for kind in definition.effects)) or "нет"
        hidden = "" if definition.model_visible else " [служебный: вызывает только Jarvis]"
        typer.echo(f"{definition.id:<22} {effects:<8} {definition.summary}{hidden}")


@tools_app.command("show")
def tools_show(
    tool_id: Annotated[str, typer.Argument(help="ID инструмента, например filesystem.search")],
) -> None:
    """Определение инструмента: описание, эффекты, цели, таймаут, схемы аргументов и результата."""
    found = {definition.id: definition for definition in _definitions()}
    definition = found.get(ToolId(tool_id))
    if definition is None:
        typer.echo(f"нет инструмента {tool_id}; список — jarvis tools", err=True)
        raise typer.Exit(2)
    typer.echo(f"{definition.id}\n")
    typer.echo(definition.description.strip())
    typer.echo("")
    typer.echo(f"эффекты: {', '.join(sorted(kind.value for kind in definition.effects)) or 'нет'}")
    typer.echo(f"цели: {', '.join(sorted(kind.value for kind in definition.targets))}")
    typer.echo(f"таймаут: {definition.timeout_s:g} с")
    typer.echo(f"результат — недоверенные данные: {'да' if definition.untrusted_output else 'нет'}")
    typer.echo("\nаргументы:")
    typer.echo(json.dumps(definition.input_schema, ensure_ascii=False, indent=2))
    typer.echo("\nрезультат:")
    typer.echo(json.dumps(definition.output_schema, ensure_ascii=False, indent=2))
