"""Публичный репозиторий без лишнего: ни ключей и паролей, ни личных путей и адресов почты, ни тяжёлых файлов
и программ. Проверяется всё, что лежит в git; поддельные ключи и адреса в тестах — не в счёт (тесты маскировки
без них не обойдутся), личные пути — и в тестах тоже."""

import re
import subprocess
from pathlib import Path

import pytest

from jarvis import redact

ROOT = Path(__file__).resolve().parents[2]
LOCKS = ("uv.lock", "package-lock.json")  # чужие адреса почты и большие, но текстовые
# здесь образцы того, что ищем, — в комментариях
SELF = ("core/jarvis/redact.py", "core/tests/test_hygiene.py")
TEXT_MAX = 1024 * 1024
BINARY_MAX = 512 * 1024
BANNED = (
    ".exe", ".dll", ".msi", ".zip", ".7z", ".rar", ".onnx", ".gguf", ".bin", ".pt", ".safetensors",
    ".db", ".sqlite", ".sqlite3", ".pyc", ".log", ".pem", ".pfx", ".p12", ".key",
)  # fmt: skip
SECRETS = (*redact._TOKEN_RES, redact._PEM_RE, redact._URL_CREDS_RE)
# имя в пути — образец, а не чьё-то настоящее
PLACEHOLDERS = frozenset(
    {"me", "u", "user", "username", "you", "name", "пользователь", "имя", "тони", "сашка", "public",
     "default", "runneradmin", "…", "..."}
)  # fmt: skip
WIN_PATH = re.compile(r"(?i)\b[a-z]:(?:\\\\|\\|/)users(?:\\\\|\\|/)([^\\/\s\"'<>%$`*|:]+)")
UNIX_PATH = re.compile(r"(?<![\w@.~-])/(?:home|Users)/([A-Za-z0-9._-]+)")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
SAFE_DOMAINS = re.compile(
    r"(?i)(^|\.)(example(\.(com|org|net|ru))?|test|local|invalid|localhost|users\.noreply\.github\.com)$"
)


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("нет git: проверка только в рабочей копии репозитория")
    files = [f for f in out.decode("utf-8").split("\0") if f and (ROOT / f).is_file()]
    if not files:
        pytest.skip("пустой список файлов git")
    return files


def _text(path: str) -> str | None:
    data = (ROOT / path).read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


@pytest.fixture(scope="module")
def tracked() -> dict[str, str | None]:
    return {path: _text(path) for path in _tracked()}


def test_no_heavy_files_or_programs(tracked: dict[str, str | None]) -> None:
    banned = [p for p in tracked if p.casefold().endswith(BANNED) or Path(p).name.casefold() == ".env"]
    assert banned == [], "программы, модели, базы, журналы и ключи в репозиторий не кладём"
    heavy = [
        f"{p}: {(ROOT / p).stat().st_size // 1024} КБ"
        for p, text in tracked.items()
        if (ROOT / p).stat().st_size > (TEXT_MAX if text is not None else BINARY_MAX)
    ]
    assert heavy == [], "тяжёлые файлы — в выпуск или в загрузку по ссылке, не в git"


def test_no_secrets(tracked: dict[str, str | None]) -> None:
    found = [
        f"{path}: {m.group(0)[:24]}…"
        for path, text in tracked.items()
        if text is not None and not path.startswith("core/tests/") and path not in SELF
        for pattern in SECRETS
        for m in pattern.finditer(text)
        if redact.MASK not in m.group(0)  # уже маска («postgres://user:•••@host» в описании) — не секрет
    ]
    assert found == [], "похоже на ключ, токен или пароль — уберите и смените его"


def test_no_personal_paths(tracked: dict[str, str | None]) -> None:
    found = [
        f"{path}: {m.group(0)}"
        for path, text in tracked.items()
        if text is not None and path not in SELF
        for pattern in (WIN_PATH, UNIX_PATH)
        for m in pattern.finditer(text)
        if m.group(1).casefold().rstrip(".") not in PLACEHOLDERS
    ]
    assert found == [], "в примерах — C:\\Users\\me, а не настоящее имя пользователя"


def test_no_real_emails(tracked: dict[str, str | None]) -> None:
    found = [
        f"{path}: {m.group(0)}"
        for path, text in tracked.items()
        if text is not None and not path.startswith("core/tests/") and not path.endswith(LOCKS)
        for m in EMAIL.finditer(text)
        if not SAFE_DOMAINS.search(m.group(1))
    ]
    assert found == [], "адреса почты в примерах — на example.com, авторы — через noreply GitHub"


def test_patterns_catch_what_they_should() -> None:
    assert any(p.search("ключ sk-proj-" + "A" * 24) for p in SECRETS)
    assert WIN_PATH.search(r"C:\Users\Dmitry\Desktop").group(1) == "Dmitry"  # type: ignore[union-attr]
    assert WIN_PATH.search(r"C:\\Users\\me\\x").group(1) == "me"  # type: ignore[union-attr]
    assert UNIX_PATH.search("/home/dmitry/.ssh").group(1) == "dmitry"  # type: ignore[union-attr]
    assert UNIX_PATH.search("@/pages/home/HomePage") is None
    assert not SAFE_DOMAINS.search("gmail.com") and SAFE_DOMAINS.search("mail.example.com")
    assert EMAIL.search("207325794+x@users.noreply.github.com").group(1) == "users.noreply.github.com"  # type: ignore[union-attr]
