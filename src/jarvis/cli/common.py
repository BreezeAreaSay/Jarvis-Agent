"""Общее для команд CLI."""

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from jarvis.config import LoadedConfig


def load_or_exit() -> "LoadedConfig":
    from jarvis.config import load_config  # конфиг нужен командам, но не `jarvis --version`
    from jarvis.domain.errors import ConfigError

    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
