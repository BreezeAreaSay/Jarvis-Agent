"""Встроенные команды Jarvis: id — builtin.<имя>, категории — jarvis.commands.model.CATEGORIES."""

from functools import lru_cache

from jarvis.commands.library import apps, assistant, inputs, system, web
from jarvis.commands.library.base import Builtin


@lru_cache(maxsize=1)
def builtin_commands() -> tuple[Builtin, ...]:
    """Все встроенные команды (собираются один раз на процесс)."""
    items = [*system.commands(), *assistant.commands(), *inputs.commands(), *web.commands(), *apps.commands()]
    seen: set[str] = set()
    for item in items:
        if item.command.id in seen:
            raise RuntimeError(f"повтор id встроенной команды: {item.command.id}")
        seen.add(item.command.id)
    return tuple(items)
