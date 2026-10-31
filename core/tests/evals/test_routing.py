"""Набор фраз маршрутизации (evals/routing.py): ни одного ложного запуска команды на вопросах, отрицаниях,
составных делах и похожих словах, ни одной не той команды; полнота команд — не ниже порога."""

import asyncio
from typing import Any

import pytest

from evals import routing
from jarvis.context import AppContext


@pytest.fixture
async def cmds(ctx: AppContext) -> Any:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    return ctx.commands


async def test_routing_dataset(cmds: Any) -> None:
    report = routing.evaluate(cmds)
    assert report.false_launches == [], "ложный запуск — настоящее действие на компьютере"
    assert report.wrong == [] and report.agent_mismatch == []
    assert report.recall >= routing.MIN_RECALL, report.missed
    assert report.ok and len(report.dataset_sha256) == 64


async def test_question_mark_and_compound_search_rules(cmds: Any) -> None:
    assert cmds.match("выключить компьютер?") is None  # вопрос — не просьба
    assert cmds.match("какая погода?") is not None  # справка — и вопросом
    assert cmds.match("найди отели и забронируй самый дешёвый") is None  # дело агенту, не поиск
    found = cmds.match("найди рецепт борща")
    assert found is not None and found.parts[0].slots == {"query": "рецепт борща"}
