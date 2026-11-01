"""Самопроверка собранного ядра: `python -X utf8 -m jarvis --selftest [--strict] [--json]`.

Сборка установщика (scripts/build-win.ps1) запускает её встроенным Python сразу после сборки ядра — до
того, как установщик уйдёт людям: не попала зависимость, не грузится нативная библиотека, битый словарь
ударений, ядро не поднимается — сборка падает. В разработке — `uv run python -m jarvis --selftest`.

Без сети и без настоящих данных человека: каталог данных — временная папка, платформа — заглушка (fake).
По пунктам печатает ✓/✗ и время; код выхода 0 — всё ✓, 1 — есть ✗. --strict — для сборки установщика: без
словаря ударений — ✗ (иначе — пропуск). --json — итог одной строкой JSON (для CI).
"""

import contextlib
import hashlib
import importlib
import json
import os
import pkgutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# библиотеки с нативными частями или без которых ядро не работает: нет в сборке — ✗
LIBRARIES = (
    "numpy",
    "sounddevice",
    "sherpa_onnx",
    "miniaudio",
    "psutil",
    "PIL.Image",
    "mss",
    "rapidfuzz",
    "edge_tts",
    "mcp",
    "websockets",
    "python_socks",
    "httpx",
    "uvicorn",
    "fastapi",
    "pydantic",
    "bs4",
    "qrcode",
    "send2trash",
)
WINDOWS_LIBRARIES = (
    "win32api",
    "win32crypt",  # шифрование секретов (DPAPI)
    "pythoncom",
    "comtypes",
    "pycaw.pycaw",
    "uiautomation",
    "pynput",
    "soundcard",
    "winrt.windows.foundation",
    "winrt.windows.media.control",
    "winrt.windows.media.speechsynthesis",
    "winrt.windows.ui.notifications",
)
SHERPA_VERSION = "1.13.8"  # версии обёртки и ядра sherpa-onnx должны совпадать (ABI)


@dataclass
class Check:
    name: str
    ok: bool = True
    detail: str = ""
    skipped: bool = False
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)


def _run(name: str, fn: Callable[[Check], None]) -> Check:
    check = Check(name)
    started = time.perf_counter()
    try:
        fn(check)
    except Exception as e:
        check.ok, check.detail = False, f"{type(e).__name__}: {e}"
    check.seconds = time.perf_counter() - started
    if check.problems:
        check.ok = False
        check.detail = "; ".join(check.problems[:8]) + (" …" if len(check.problems) > 8 else "")
    return check


def _python(check: Check) -> None:
    version = sys.version_info
    check.detail = f"{version.major}.{version.minor}.{version.micro} ({sys.platform})"
    if (version.major, version.minor) not in ((3, 12), (3, 13)):
        check.problems.append(f"нужен Python 3.12 или 3.13, а не {version.major}.{version.minor}")


def _modules(check: Check) -> None:
    import jarvis

    count = 0
    for info in pkgutil.walk_packages(jarvis.__path__, "jarvis."):
        count += 1
        try:
            importlib.import_module(info.name)
        except Exception as e:
            check.problems.append(f"{info.name}: {type(e).__name__}: {e}")
    check.detail = f"{count} модулей"


def _libraries(check: Check) -> None:
    names = LIBRARIES + (WINDOWS_LIBRARIES if sys.platform == "win32" else ())
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:
            check.problems.append(f"{name}: {type(e).__name__}: {e}")
    check.detail = f"{len(names)} библиотек"


def _voice(check: Check) -> None:
    import numpy as np
    import sherpa_onnx
    import sounddevice

    version = str(getattr(sherpa_onnx, "__version__", ""))
    if version != SHERPA_VERSION:
        check.problems.append(f"sherpa-onnx {version or '?'} вместо {SHERPA_VERSION}")
    # нативная часть sherpa-onnx грузится по-настоящему: детектор речи без модели не создать, конфиг — можно
    sherpa_onnx.VadModelConfig()
    np.zeros(16, dtype=np.float32).tobytes()
    portaudio = sounddevice.get_portaudio_version()[1]
    check.detail = f"sherpa-onnx {version}, {portaudio}"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _resources(strict: bool) -> Callable[[Check], None]:
    def check_resources(check: Check) -> None:
        from jarvis import paths
        from jarvis.voice import stress

        packs = list((paths.resource_dir() / "packs").glob("*.json"))
        if not packs:
            check.problems.append("нет паков команд (jarvis/resources/packs)")
        found = stress.bundled()
        if found is None:
            if strict:
                check.problems.append("нет словаря ударений (jarvis/resources/voice/ru_dict и ru_dict.json)")
            dictionary = "словаря ударений нет"
        else:
            path, info = found
            if _digest(path) != info.get("sha256"):
                check.problems.append("словарь ударений повреждён: SHA-256 не сходится с ru_dict.json")
            if not (path.parent / "LICENSE.md").is_file():
                check.problems.append("рядом со словарём ударений нет LICENSE.md (GPL-3.0, CC BY-SA 4.0)")
            dictionary = f"словарь ударений {path.stat().st_size / 1e6:.1f} МБ"
        check.detail = f"{len(packs)} паков команд, {dictionary}"

    return check_resources


def _core(check: Check) -> None:
    """Ядро поднимается целиком (все сервисы, платформа-заглушка) и отвечает по API."""
    from fastapi.testclient import TestClient

    from jarvis.app import build_context
    from jarvis.server import create_app

    token = "selftest"
    ctx = build_context("fake")
    app = create_app(ctx, token)
    with TestClient(app, headers={"X-Jarvis-Token": token}) as client:
        health = client.get("/api/health")
        if health.status_code != 200 or not health.json().get("ok"):
            check.problems.append(f"/api/health: {health.status_code}")
        for path in ("/api/settings", "/api/commands", "/api/ai/providers", "/api/background"):
            response = client.get(path)
            if response.status_code != 200:
                check.problems.append(f"{path}: {response.status_code} {response.text[:120]}")
        if client.get("/api/settings", headers={"X-Jarvis-Token": "wrong-token"}).status_code != 401:
            check.problems.append("API отвечает без токена")
        check.detail = f"версия {health.json().get('version', '?')}"
    with contextlib.suppress(Exception):
        ctx.db.close()


def run(*, strict: bool = False) -> list[Check]:
    from jarvis import paths

    with tempfile.TemporaryDirectory(prefix="jarvis-selftest-") as tmp:
        previous = os.environ.get("JARVIS_VOICE_AUTODOWNLOAD")
        data_dir = paths._override  # каталог данных, заданный до проверки (тесты), — вернуть после
        os.environ["JARVIS_VOICE_AUTODOWNLOAD"] = "0"  # без сети
        paths.set_data_dir(tmp)
        try:
            return [
                _run("Python", _python),
                _run("Модули Jarvis", _modules),
                _run("Библиотеки", _libraries),
                _run("Голос (sherpa-onnx, PortAudio)", _voice),
                _run("Ресурсы", _resources(strict)),
                _run("Ядро отвечает по API", _core),
            ]
        finally:
            paths.set_data_dir(data_dir)
            if previous is None:
                os.environ.pop("JARVIS_VOICE_AUTODOWNLOAD", None)
            else:
                os.environ["JARVIS_VOICE_AUTODOWNLOAD"] = previous


def main(argv: list[str]) -> int:
    strict, as_json = "--strict" in argv, "--json" in argv
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    started = time.perf_counter()
    checks = run(strict=strict)
    failed = [c for c in checks if not c.ok]
    if as_json:
        data: dict[str, Any] = {
            "ok": not failed,
            "seconds": round(time.perf_counter() - started, 2),
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "seconds": round(c.seconds, 2)}
                for c in checks
            ],
        }
        print(json.dumps(data, ensure_ascii=False), flush=True)
    else:
        for c in checks:
            print(f"{'✓' if c.ok else '✗'} {c.name} — {c.detail} ({c.seconds:.1f} с)", flush=True)
        total = time.perf_counter() - started
        print(
            f"{'Самопроверка пройдена' if not failed else 'Самопроверка не пройдена'}: "
            f"{len(checks) - len(failed)}/{len(checks)} за {total:.1f} с",
            flush=True,
        )
    return 1 if failed else 0
