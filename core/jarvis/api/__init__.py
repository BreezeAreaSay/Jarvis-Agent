"""Все маршруты HTTP API ядра. Каждая область — свой модуль с router."""

from fastapi import FastAPI

from jarvis.api import ai, commands, core, desks, mcp, notes, plugins, remote, voice, widgets, ws


def include_all(app: FastAPI) -> None:
    for module in (core, ws, commands, voice, ai, notes, mcp, plugins, remote, widgets, desks):
        app.include_router(module.router)
