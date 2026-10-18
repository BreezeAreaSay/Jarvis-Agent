"""Числа словами и цифрами во фразах."""

import pytest

from jarvis.commands import numbers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("пятьдесят", 50),
        ("двадцать пять", 25),
        ("сто", 100),
        ("ноль", 0),
        ("один", 1),
        ("одна", 1),
        ("одну", 1),
        ("два", 2),
        ("две", 2),
        ("одиннадцать", 11),
        ("девяносто девять", 99),
        ("сто двадцать пять", 125),
        ("двести", 200),
        ("тысяча", 1000),
        ("две тысячи двадцать шесть", 2026),
        ("полтора", 1.5),
        ("полторы", 1.5),
        ("три с половиной", 3.5),
        ("половину", 50),
        ("наполовину", 50),
        ("максимум", 100),
        ("полную", 100),
        ("минимум", 0),
        ("50", 50),
        ("50%", 50),
        ("2.5", 2.5),
        ("35 процентов", 35),
        ("сорок два %", 42),
        ("тридцать пять процентов", 35),
        ("двадцати", 20),
        ("пятидесяти", 50),
        ("сорока", 40),
        ("девяноста", 90),
        ("ста", 100),
        ("трехсот", 300),
        ("пяти", 5),
        ("восьми", 8),
        ("семьюдесятью", 70),
    ],
)
def test_parse_number(text: str, expected: float) -> None:
    assert numbers.parse_number(text) == expected


@pytest.mark.parametrize(
    "text", ["", "привет", "на полную", "ноль пять", "двадцать двенадцать", "сто сто", "раз"]
)
def test_not_a_number(text: str) -> None:
    assert numbers.parse_number(text) is None


def test_parse_at_offers_shorter_variants() -> None:
    # «процентов» может принадлежать шаблону — поэтому есть вариант и без него
    assert numbers.parse_at(["двадцать", "пять", "процентов", "громче"], 0) == [(25.0, 3), (25.0, 2)]
    assert numbers.parse_at(["громкость", "пятьдесят"], 1) == [(50.0, 2)]
    assert numbers.parse_at(["громкость"], 0) == []


def test_find_number() -> None:
    assert numbers.find_number(["подожди", "две", "секунды"]) == (2.0, 1, 2)
    assert numbers.find_number(["подожди", "немного"]) is None


def test_formatting_and_plural() -> None:
    assert numbers.fmt_number(50.0) == "50"
    assert numbers.fmt_number(1.5) == "1.5"
    assert numbers.plural(1, numbers.MINUTES) == "минута"
    assert numbers.plural(3, numbers.MINUTES) == "минуты"
    assert numbers.plural(11, numbers.MINUTES) == "минут"
    assert numbers.plural(21, numbers.MINUTES) == "минута"
    assert numbers.plural(1.5, numbers.HOURS) == "часа"
    assert numbers.count_text(5, numbers.SECONDS) == "5 секунд"


def test_durations() -> None:
    assert numbers.duration_text(330) == "5 минут 30 секунд"
    assert numbers.duration_text(3600) == "1 час"
    assert numbers.duration_text(60, accusative=True) == "1 минуту"
    assert numbers.parse_duration("2") == 2
    assert numbers.parse_duration("500ms") == 0.5
    assert numbers.parse_duration("5m") == 300
    assert numbers.parse_duration("1h") == 3600
    assert numbers.parse_duration("2,5") == 2.5
    assert numbers.parse_duration("10", default_unit="m") == 600
    assert numbers.parse_duration("скоро") is None
