"""Разбор веб-адресов прямых команд и политики: только http(s), без учётных данных и чужих схем."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jarvis.domain.urls import is_web_url, normalize_web_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("github.com", "https://github.com"),
        ("GitHub.com/Anthropics", "https://github.com/Anthropics"),
        ("ya.ru", "https://ya.ru"),
        ("сайт.рф", "https://сайт.рф"),
        ("https://docs.python.org/3/", "https://docs.python.org/3/"),
        ("HTTP://Example.COM/a?b=1#c", "http://example.com/a?b=1#c"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://192.168.1.1/admin", "http://192.168.1.1/admin"),
        ("  https://example.org  ", "https://example.org"),
    ],
)
def test_web_addresses_are_normalized(raw: str, expected: str) -> None:
    assert normalize_web_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "README.md",  # зона .md — расширение файла, а не сайт
        "main.py",
        "evil.exe",
        "archive.zip",
        "github",
        "localhost:8000",  # без схемы — только домен с известной зоной
        "file:///etc/passwd",
        "ftp://example.com",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "http://user:pass@evil.com",
        "https://evil.com@good.com",
        "https://exa mple.com",
        "https://example.com/\x1b[31m",
        "http://999.1.1.1",
        "http://example.com:99999",
        "\\\\server\\share",
        "",
        "https://" + "a" * 2000 + ".com",
    ],
)
def test_anything_else_is_not_a_web_address(raw: str) -> None:
    assert normalize_web_url(raw) is None


def test_a_bare_domain_needs_permission() -> None:
    assert normalize_web_url("github.com", allow_bare=False) is None
    assert normalize_web_url("https://github.com", allow_bare=False) == "https://github.com"


def test_policy_accepts_only_already_normalized_addresses() -> None:
    assert is_web_url("https://github.com")
    assert not is_web_url("github.com")
    assert not is_web_url("HTTPS://GitHub.com")
    assert not is_web_url("file:///etc/passwd")


@given(st.text(max_size=80))
def test_a_normalized_address_is_always_http_without_credentials_or_spaces(raw: str) -> None:
    url = normalize_web_url(raw)
    if url is None:
        return
    assert url.startswith(("http://", "https://"))
    assert not any(ch.isspace() or ord(ch) < 32 for ch in url)
    assert "@" not in url.split("/", 3)[2]  # ни логина, ни пароля в адресе
    assert normalize_web_url(url, allow_bare=False) == url  # нормализация идемпотентна
