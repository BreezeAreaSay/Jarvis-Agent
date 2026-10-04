"""Ядро импортируется без инфраструктуры: проверка в чистом процессе по sys.modules.

Интерпретатор или окружение (`.pth`, sitecustomize) могут загрузить модуль из запрещённого списка ещё до
импорта ядра — на Windows, например, `winreg`. Вычитать такие модули из результата нельзя: тогда импорт
того же модуля из ядра перестал бы ловиться. Поэтому перед импортом ядра они выгружаются из
`sys.modules`: если ядро их импортирует, они появятся снова.
"""

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
forbidden = json.loads(sys.argv[1])
def matches(name):
    return any(name == item or name.startswith(item + ".") for item in forbidden)
preloaded = sorted(name for name in sys.modules if matches(name))
for name in preloaded:
    del sys.modules[name]
import jarvis.domain, jarvis.ports, jarvis.core
for package in (jarvis.domain, jarvis.ports, jarvis.core):
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)
print(json.dumps({"loaded": sorted(sys.modules), "preloaded": preloaded}))
"""


def leaked(modules: list[str]) -> set[str]:
    return {
        module
        for module in modules
        if any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN_MODULES)
    }


def run_isolated(program: str = PROGRAM) -> dict[str, list[str]]:
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(sorted(FORBIDDEN_MODULES))],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def test_core_imports_no_infrastructure() -> None:
    report = run_isolated()
    assert "jarvis.core.runner" in report["loaded"]
    assert leaked(report["loaded"]) == set()


def test_a_preloaded_forbidden_module_imported_by_core_is_still_caught() -> None:
    # Модуль загружен до импорта ядра (как winreg на Windows), а «ядро» импортирует его снова.
    program = PROGRAM.replace(
        "import jarvis.domain, jarvis.ports, jarvis.core",
        "import jarvis.domain, jarvis.ports, jarvis.core\nimport sqlite3",
    ).replace("import importlib, json, pkgutil, sys", "import importlib, json, pkgutil, sys, sqlite3")
    report = run_isolated(program)
    assert "sqlite3" in report["preloaded"]
    assert "sqlite3" in leaked(report["loaded"])
