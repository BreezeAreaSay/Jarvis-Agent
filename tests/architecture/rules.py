"""AST-проверки правил архитектуры, которые import-linter не покрывает (01-structure.md §4).

- `domain`, `ports`, `core` не импортируют инфраструктуру — ни внешние пакеты, ни модули стандартной
  библиотеки для процессов, БД, сети, ОС и файлов; не открывают файлы.
- Переменные окружения читает только `jarvis.config`.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "jarvis"
CORE_LAYERS = ("domain", "ports", "core")
ENV_READER = "config"

INFRASTRUCTURE = frozenset(
    {
        # процессы, БД, сеть, ОС
        "subprocess", "sqlite3", "socket", "ssl", "http", "urllib", "ctypes", "multiprocessing",
        "asyncio.subprocess", "signal", "winreg", "msvcrt", "_winapi",
        # файлы и пути: ядро работает с TargetPath через порты
        "os", "io", "pathlib", "shutil", "tempfile", "glob", "fileinput",
        # сторонняя инфраструктура
        "httpx", "requests", "aiohttp", "mcp", "psutil", "llama_cpp", "ollama", "openai", "docker",
        "win32api", "win32con", "pywintypes", "pywinauto", "comtypes", "uiautomation",
        "sounddevice", "pyaudio", "vosk", "faster_whisper", "whisper", "openwakeword", "pyttsx3",
        "yaml", "tomllib", "typer", "click", "rich",
        # внешние слои самого проекта
        "jarvis.adapters", "jarvis.app", "jarvis.config", "jarvis.cli", "jarvis.evals",
    }
)  # fmt: skip
FILE_METHODS = frozenset({"read_text", "write_text", "read_bytes", "write_bytes"})
ENV_NAMES = frozenset({"environ", "getenv", "putenv", "unsetenv", "environb"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def _forbidden(module: str) -> str | None:
    for name in INFRASTRUCTURE:
        if module == name or module.startswith(f"{name}."):
            return name
    return None


def check_core_source(source: str, path: str = "<source>") -> list[Violation]:
    """Нарушения для модуля из `domain`, `ports` или `core`."""
    violations: list[Violation] = []
    for node in ast.walk(ast.parse(source)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        for module in modules:
            if (name := _forbidden(module)) is not None:
                violations.append(Violation(path, node.lineno, "import", name))
                break
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                violations.append(Violation(path, node.lineno, "file-io", "open()"))
            if isinstance(func, ast.Attribute) and func.attr in FILE_METHODS:
                violations.append(Violation(path, node.lineno, "file-io", f".{func.attr}()"))
    return violations


def check_env_access(source: str, path: str = "<source>") -> list[Violation]:
    """Чтение окружения вне `jarvis.config`."""
    violations: list[Violation] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            violations.append(Violation(path, node.lineno, "env", f"os.{node.attr}"))
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in ENV_NAMES:
                    violations.append(Violation(path, node.lineno, "env", f"from os import {alias.name}"))
    return violations


def python_files(*parts: str) -> Iterator[Path]:
    yield from sorted(SRC.joinpath(*parts).rglob("*.py"))


def check_repository() -> list[Violation]:
    violations: list[Violation] = []
    for layer in CORE_LAYERS:
        for file in python_files(layer):
            violations += check_core_source(file.read_text(encoding="utf-8"), str(file.relative_to(SRC)))
    for file in python_files():
        if file.relative_to(SRC).parts[0] == ENV_READER:
            continue
        violations += check_env_access(file.read_text(encoding="utf-8"), str(file.relative_to(SRC)))
    return violations
