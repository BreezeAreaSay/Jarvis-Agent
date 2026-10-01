"""Ядро импортируется без инфраструктуры: проверка в чистом процессе по sys.modules."""

import json
import subprocess
import sys

FORBIDDEN_MODULES = {
    "sqlite3", "httpx", "mcp", "psutil", "llama_cpp", "ollama", "openai", "docker", "winreg",
    "win32api", "pywintypes", "comtypes", "pywinauto", "tauri", "sounddevice", "pyaudio", "vosk",
    "faster_whisper", "whisper", "openwakeword", "pyttsx3", "yaml", "typer", "click", "rich",
    "jarvis.adapters", "jarvis.app", "jarvis.config", "jarvis.cli", "jarvis.evals",
}  # fmt: skip

PROGRAM = """
import importlib, json, pkgutil, sys
import jarvis.domain, jarvis.ports, jarvis.core
for package in (jarvis.domain, jarvis.ports, jarvis.core):
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)
print(json.dumps(sorted(sys.modules)))
"""


def test_core_imports_no_infrastructure() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = set(json.loads(result.stdout))
    assert "jarvis.core.runner" in loaded
    leaked = {
        module
        for module in loaded
        if any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN_MODULES)
    }
    assert leaked == set()
