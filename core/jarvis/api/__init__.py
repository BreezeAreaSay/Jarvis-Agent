"""Все маршруты HTTP API ядра. Каждая область — свой модуль с router."""

from fastapi import FastAPI

from jarvis.api import (
    agent_browser,
    ai,
    background,
    browser,
    commands,
    core,
    data,
    desks,
    diagnostics,
    images,
    mcp,
    notes,
    plugins,
    remote,
    scenarios,
    sysops,
    tools,
    voice,
    widgets,
    ws,
)


def include_all(app: FastAPI) -> None:
    for module in (
        core,
        ws,
        commands,
        voice,
        ai,
        browser,
        agent_browser,
        background,
        images,
        notes,
        mcp,
        plugins,
        remote,
        widgets,
        sysops,
        desks,
        scenarios,
        data,
        diagnostics,
        tools,
    ):
        app.include_router(module.router)
