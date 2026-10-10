"""Проверка Everything и es.exe: что проиндексировано и проходит ли кириллица.

Запуск: uv run --no-project --python 3.12 python C:\\Jarvis\\scripts\\es_check.py
Путь к es.exe — переменная JARVIS_ES (по умолчанию C:\\Jarvis\\bin\\es.exe). Тестовый файл кладётся в C:\\Jarvis\\scratch.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

if sys.stdout is not None and not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ES = os.environ.get("JARVIS_ES", r"C:\Jarvis\bin\es.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HINTS = {8: "Everything не запущен (или это 1.5 alpha — тогда нужен -instance 1.5a)", 7: "сбой связи с Everything"}


def es(*args: str) -> tuple[int, str]:
    r = subprocess.run(
        [ES, *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=15, creationflags=NO_WINDOW
    )
    out = r.stdout.decode("utf-8", "replace").strip() or r.stderr.decode("utf-8", "replace").strip()
    return r.returncode, out


for args in (["-version"], ["-get-everything-version"]):
    code, out = es(*args)
    print(f"{' '.join(args)}: {out} (код {code}{', ' + HINTS[code] if code in HINTS else ''})")

for path in (None, r"C:\Windows", os.environ.get("USERPROFILE", ""), "D:\\"):
    code, out = es("-get-result-count", *(["-path", path] if path else []))
    print(f"объектов в индексе {path or '(всего)'}: {out} (код {code})")

probe = Path(r"C:\Jarvis\scratch\Тест_ёЁ_поиска.txt")
probe.parent.mkdir(parents=True, exist_ok=True)
probe.touch()
time.sleep(3)
for args in (["-cp", "65001"], []):
    code, out = es(*args, "-n", "5", probe.stem)
    exact = str(probe).lower() in out.lower()
    print(f"кириллица {' '.join(args) or 'без -cp'}: код {code}, точный путь найден: {exact}, вывод: {ascii(out[:120])}")
