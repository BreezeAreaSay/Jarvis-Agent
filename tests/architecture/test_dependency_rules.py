import subprocess
import sys

import pytest

from tests.architecture.rules import SRC, check_core_source, check_env_access, check_repository


def test_repository_follows_the_rules() -> None:
    assert [str(violation) for violation in check_repository()] == []


def test_import_linter_contracts_are_kept() -> None:
    # Отдельный процесс: import-linter перенастраивает logging и выключил бы логгеры остальных тестов.
    program = "from importlinter.cli import lint_imports; raise SystemExit(lint_imports(config_filename=%r))"
    config = str(SRC.parents[1] / "pyproject.toml")
    result = subprocess.run(
        [sys.executable, "-c", program % config], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source", "rule", "detail"),
    [
        ("import sqlite3", "import", "sqlite3"),
        ("import subprocess as sp", "import", "subprocess"),
        ("from asyncio.subprocess import PIPE", "import", "asyncio.subprocess"),
        ("from asyncio import subprocess", "import", "asyncio.subprocess"),
        ("import httpx", "import", "httpx"),
        ("from mcp.client import stdio", "import", "mcp"),
        ("import llama_cpp", "import", "llama_cpp"),
        ("from os import path", "import", "os"),
        ("from pathlib import Path", "import", "pathlib"),
        ("import winreg", "import", "winreg"),
        ("import faster_whisper", "import", "faster_whisper"),
        ("from jarvis.adapters.memory import InMemoryStorage", "import", "jarvis.adapters"),
        ("from jarvis.config import load_config", "import", "jarvis.config"),
        ("open('x.txt')", "file-io", "open()"),
        ("path.write_text('x')", "file-io", ".write_text()"),
    ],
)
def test_core_rule_catches_violations(source: str, rule: str, detail: str) -> None:
    violations = check_core_source(source)
    assert [(item.rule, item.detail) for item in violations] == [(rule, detail)]


def test_core_rule_allows_pure_code() -> None:
    source = (
        "import asyncio\nimport json\nimport logging\nfrom importlib import resources\n"
        "from pydantic import BaseModel\nfrom jarvis.domain.ids import TaskId\n"
        "text = 'a'.replace('a', 'b')\n"
    )
    assert check_core_source(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.environ['X']",
        "import os\nos.getenv('X')",
        "from os import environ",
        "from os import getenv as read",
    ],
)
def test_env_rule_catches_violations(source: str) -> None:
    assert [item.rule for item in check_env_access(source)] == ["env"]
