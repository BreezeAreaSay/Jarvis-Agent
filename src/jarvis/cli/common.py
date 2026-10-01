"""Общее для команд CLI."""

import typer

from jarvis.config import LoadedConfig, load_config
from jarvis.domain.errors import ConfigError


def load_or_exit() -> LoadedConfig:
    try:
        return load_config()
    except ConfigError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
