"""Помощники для встроенных команд: запись команды одной строкой и раскрытие вариантов фраз."""

import re
from dataclasses import dataclass

from jarvis.commands.model import Action, Command

_GROUP = re.compile(r"\(([^()]*)\)")


@dataclass(frozen=True)
class Builtin:
    command: Command
    exclude: tuple[str, ...] = ()  # начала слов, при которых команда не подходит


def variants(*patterns: str) -> list[str]:
    """«(сделай|поставь) громкость (на|) {number}» → все сочетания; пустая альтернатива — слово можно
    опустить."""
    out: list[str] = []
    for pattern in patterns:
        m = _GROUP.search(pattern)
        if m is None:
            out.append(" ".join(pattern.split()))
            continue
        for alt in m.group(1).split("|"):
            out.extend(variants(pattern[: m.start()] + alt + pattern[m.end() :]))
    return list(dict.fromkeys(p for p in out if p))


def cmd(
    id_: str,
    name: str,
    phrases: list[str] | tuple[str, ...],
    actions: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    *,
    category: str,
    icon: str,
    confirm: bool = False,
    chain: bool = True,
    say: str = "",
    optional: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> Builtin:
    """Встроенная команда: id builtin.<id_>, фразы — уже раскрытые варианты."""
    return Builtin(
        Command(
            id=f"builtin.{id_}",
            name=name,
            category=category,
            phrases=list(dict.fromkeys(phrases)),
            optional=list(optional),
            actions=[Action(type=t, value=v) for t, v in actions],
            confirm=confirm,
            chain=chain,
            icon=icon,
            say=say,
        ),
        exclude,
    )


def key(combo: str) -> tuple[str, str]:
    return ("Keys.Press", combo)
