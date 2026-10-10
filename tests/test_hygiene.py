"""Репозиторий публичный: в него не попадают секреты, бинарники, журналы и личные пути."""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_NAMES = {"jarvis.toml", "auth.json", ".env", ".git-credentials", "pipe.key"}
FORBIDDEN_SUFFIXES = {".log", ".exe", ".gguf", ".onnx", ".bin", ".dll", ".pyd", ".pyc"}
FORBIDDEN_SUFFIXES |= {".ico", ".png", ".zip", ".key", ".msi"}
FORBIDDEN_DIRS = {"journal", "scratch", "logs", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}

SECRETS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),
]
# выдуманные имена профилей в тестах и документации; «meow» — ловушка сравнения по префиксу
ALLOWED_PROFILES = {"me", "meow", "public", "default", "runner~1", "runneradmin", "<имя>", "<user>"}
PROFILE_PATH = re.compile(r"[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)([^\\/\s\"'`),.;:]+)", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = ("example", "example.com", "example.org", "example.net", "invalid", "test")
ALLOWED_USERNAMES = {"me", "runner", "runneradmin", ""}


def _files() -> list[str]:
    out = subprocess.run(
        # quotepath=off и -z: иначе имена с кириллицей приходят в кавычках с \320… и не находятся на диске
        ["git", "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [line for line in out.split("\0") if line and (ROOT / line).is_file()]


def _texts() -> list[tuple[str, str]]:
    result = []
    for name in _files():
        data = (ROOT / name).read_bytes()
        if b"\0" in data[:4096]:
            continue
        result.append((name, data.decode("utf-8", errors="ignore")))
    return result


def test_no_forbidden_files() -> None:
    bad = []
    for name in _files():
        p = PurePosixPath(name)
        if (
            p.name.casefold() in FORBIDDEN_NAMES
            or p.suffix.casefold() in FORBIDDEN_SUFFIXES
            or any(part in FORBIDDEN_DIRS for part in p.parts[:-1])
            or name.startswith("bench/results/")
        ):
            bad.append(name)
    assert not bad, f"в коммит попадут запрещённые файлы: {bad}"


def test_no_secrets() -> None:
    bad = []
    for name, text in _texts():
        if name.startswith("prompts/") or name == "tests/test_hygiene.py":
            continue
        bad += [f"{name}: {m.group(0)[:12]}…" for rx in SECRETS for m in rx.finditer(text)]
    assert not bad, f"похоже на секреты: {bad}"


def test_no_personal_profile_paths() -> None:
    bad = []
    for name, text in _texts():
        if name.startswith("prompts/"):
            continue
        for m in PROFILE_PATH.finditer(text):
            if m.group(1).casefold() not in ALLOWED_PROFILES:
                bad.append(f"{name}: {m.group(0)}")
    assert not bad, f"личные пути профиля: {bad}"


def test_no_current_username() -> None:
    user = os.environ.get("USERNAME", "").strip()
    if user.casefold() in ALLOWED_USERNAMES or len(user) < 3:
        pytest.skip("имя пользователя не задано или служебное")
    rx = re.compile(rf"(?<![\w]){re.escape(user)}(?![\w])", re.IGNORECASE)
    bad = [name for name, text in _texts() if rx.search(text)]
    assert not bad, f"имя текущего пользователя в файлах: {bad}"


def test_no_emails() -> None:
    bad = []
    for name, text in _texts():
        if name.startswith("prompts/"):
            continue
        for m in EMAIL.finditer(text):
            domain = m.group(1).casefold()
            if not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
                bad.append(f"{name}: {m.group(0)}")
    assert not bad, f"e-mail в файлах: {bad}"
