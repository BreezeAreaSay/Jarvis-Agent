"""Все маршруты HTTP API ядра. Каждая область — свой модуль с router."""

from fastapi import FastAPI

from jarvis.api import (
    ai,
    commands,
    core,
    data,
    desks,
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
        tools,
    ):
        app.include_router(module.router)
