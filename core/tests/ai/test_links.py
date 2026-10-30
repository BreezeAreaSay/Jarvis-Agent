"""Имена сайтов в вопросах «Открыть …?»: настоящее имя (punycode) для имён не латиницей и подделки —
буквы разных алфавитов в одном имени или русские двойники латинских букв."""

import pytest

from jarvis.ai.policy import ascii_host, link_note, lookalike_host, unicode_host


@pytest.mark.parametrize(
    "host",
    [
        "gооgle.com",  # русские «о»
        "xn--ggle-55da.com",  # то же, уже в punycode
        "раураl.com",  # русские «р», «а», «у» и латинская «l»
        "аррӏе.com",  # целиком из русских двойников латинских букв
        "www.аррӏе.com",
        "αpple.com",  # греческая «α»
        "sberbаnk.ru",  # русская «а» в латинском имени
    ],
)
def test_lookalikes(host: str) -> None:
    assert lookalike_host(host)
    assert link_note(host).startswith("Осторожно: имя сайта")


@pytest.mark.parametrize(
    "host",
    [
        "google.com",
        "президент.рф",
        "яндекс.рф",
        "пример.com",
        "сайт.ru",
        "bücher.de",
        "localhost",
        "",
        "東京.jp",
    ],
)
def test_not_lookalikes(host: str) -> None:
    assert not lookalike_host(host)


def test_names_both_ways() -> None:
    assert ascii_host("пример.рф") == "xn--e1afmkfd.xn--p1ai"
    assert unicode_host("xn--e1afmkfd.xn--p1ai") == "пример.рф"
    assert ascii_host("google.com") == "google.com" and unicode_host("Google.COM") == "google.com"
    assert unicode_host("xn--зло.com") == "xn--зло.com"  # битый punycode — как есть
    assert link_note("google.com") == ""
    assert link_note("президент.рф") == "Настоящее имя сайта — xn--d1abbgf6aiiy.xn--p1ai."
