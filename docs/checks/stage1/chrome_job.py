"""1.4, механизм: Chrome (или Edge) без окна — в Job Object этого процесса Python, как браузер агента у ядра.

Печатает PID этого процесса и браузера и ждёт 10 минут. Проверка: убить ЭТОТ процесс Python
(Stop-Process -Force -Id <pid_ядра>) — через 10 с процессов браузера с профилем C:\\jarvis-check\\chrome-job
быть не должно.
"""

import os
import subprocess
import time
from pathlib import Path

import _common

from jarvis.ai.browser import launch
from jarvis.ai.browser.agent_browser import HEADLESS
from jarvis.platform.windows.jobs import spawn_with_core

PROFILE = Path(r"C:\jarvis-check\chrome-job")


def main() -> None:
    app = launch.find("auto")
    if app is None:
        raise SystemExit("Chrome и Edge не найдены")
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = spawn_with_core(
        [app.path, *launch.arguments(PROFILE, "about:blank"), *HEADLESS],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    _common.report(
        {
            "pid_ядра": os.getpid(),
            "pid_браузера": proc.pid,
            "браузер": app.title,
            "жив": proc.poll() is None,
            "профиль": str(PROFILE),
        }
    )
    time.sleep(600)


if __name__ == "__main__":
    main()
