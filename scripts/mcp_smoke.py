"""Проверка MCP-сервера pc без Codex: клиент mcp по stdio запускает `python -m pc.mcp`, печатает инструменты
(ожидается 18), результат list_windows и find_files("отчёт").

Запуск: `uv run python scripts/mcp_smoke.py`; в сессии — с JARVIS_DATA_DIR во временной папке.
Код выхода: 0 — 18 инструментов, 1 — иначе.
"""

import io
import os
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = 18


def server_params() -> StdioServerParameters:
    """Клиент mcp передаёт серверу только «безопасные» системные переменные — наши задаём явно."""
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for name in ("JARVIS_DATA_DIR", "JARVIS_CONFIG"):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    return StdioServerParameters(command=sys.executable, args=["-m", "pc.mcp"], cwd=str(ROOT), env=env)


def text_of(result: CallToolResult) -> str:
    parts = [getattr(block, "text", f"<{block.type}>") for block in result.content]
    prefix = "ОШИБКА ПРОТОКОЛА: " if result.isError else ""
    return prefix + "\n".join(parts)


async def run() -> int:
    started = time.perf_counter()
    async with stdio_client(server_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        ready = time.perf_counter() - started
        tools = (await session.list_tools()).tools
        print(f"Инструментов: {len(tools)} (ожидается {EXPECTED_TOOLS}); сервер готов за {ready:.2f} с")
        for tool in tools:
            print(f"  {tool.name} — {tool.description}")
        for name, args in (("list_windows", {}), ("find_files", {"query": "отчёт"})):
            t0 = time.perf_counter()
            result = await session.call_tool(name, args)
            print(f"\n{name} {args} — {time.perf_counter() - t0:.2f} с:\n{text_of(result)}")
    return 0 if len(tools) == EXPECTED_TOOLS else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    return anyio.run(run)


if __name__ == "__main__":
    sys.exit(main())
