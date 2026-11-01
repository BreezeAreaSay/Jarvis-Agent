"""1.4 как в жизни: браузер агента фоновых задач (AgentBrowser) запускает свой Chrome без окна — в Job
Object.

Данные — в C:\\jarvis-check\\data (профиль браузера — C:\\jarvis-check\\data\\agent-browser). Печатает
состояние браузера, PID этого процесса («ядра») и браузера; ждёт 10 минут. Проверка: Stop-Process -Force
-Id <pid_ядра> — через 10 с процессов браузера с профилем agent-browser в C:\\jarvis-check\\data быть
не должно.
"""

import asyncio
import os
from typing import Any

import _common

from jarvis.app import build_context


async def main() -> None:
    ctx: Any = build_context("windows")
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    browser = ctx.agent_browser
    await browser.start()
    client = await browser.connect(launch=True)
    proc = browser._proc
    _common.report(
        {
            "pid_ядра": os.getpid(),
            "pid_браузера": proc.pid if proc is not None else None,
            "подключён": client is not None,
            "состояние": browser.state(),
        }
    )
    await asyncio.sleep(600)


if __name__ == "__main__":
    asyncio.run(main())
