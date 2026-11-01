"""Общее для проверок этапа 1 на Windows: путь к ядру в sys.path, свой каталог данных, окно по exe, отчёт
JSON.

Скрипты запускаются из папки core клона: `uv run python ..\\docs\\checks\\stage1\\<скрипт>.py …`. Данные
Jarvis — только в C:\\jarvis-check\\data (JARVIS_DATA_DIR), а не в настоящем Jarvis человека.
"""

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "core"))
os.environ.setdefault("JARVIS_DATA_DIR", r"C:\jarvis-check\data")


def dpi(hwnd: int) -> int:
    """DPI окна (96 — 100 %, 120 — 125 %, 144 — 150 %); не Windows — 0."""
    if sys.platform != "win32":
        return 0
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))  # type: ignore[attr-defined]
    except Exception:
        return 0


def window_by_exe(platform: Any, exe: str) -> Any:
    """Видимое окно программы exe (без учёта регистра) с заголовком; нет — SystemExit с понятным текстом."""
    want = exe.casefold()
    found = [w for w in platform.list_windows() if w.exe.casefold() == want and w.title and not w.minimized]
    if not found:
        raise SystemExit(f"Окно программы {exe} не найдено (открыто ли оно и не свёрнуто?)")
    return found[0]


def report(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str), flush=True)  # вывод — и в файл сразу
