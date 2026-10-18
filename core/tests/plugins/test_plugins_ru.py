"""Русские падежи для голосовых фраз плагинов."""

import pytest

from jarvis.plugins import ru


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("люстра", "люстру"),
        ("настольная лампа", "настольную лампу"),
        ("яндекс станция", "яндекс станцию"),
        ("лампа в спальне", "лампу в спальне"),
        ("свет кухня", "свет кухня"),
        ("кондиционер", "кондиционер"),
        ("шторы", "шторы"),
        ("ворота", "ворота"),
        ("tv", "tv"),
    ],
)
def test_accusative(name: str, expected: str) -> None:
    assert ru.accusative(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("кухня", "на кухне"),
        ("спальня", "в спальне"),
        ("гостиная", "в гостиной"),
        ("прихожая", "в прихожей"),
        ("детская комната", "в детской комнате"),
        ("второй этаж", "на втором этаже"),
        ("ванная", "в ванной"),
        ("балкон", "на балконе"),
        ("кабинет", "в кабинете"),
        ("лоджия", "на лоджии"),
        ("вторая спальня", "во второй спальне"),
    ],
)
def test_place(name: str, expected: str) -> None:
    assert ru.place(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("ноутбук", "ноутбуке"), ("игровой пк", "игровом пк"), ("рабочий компьютер", "рабочем компьютере")],
)
def test_prepositional(name: str, expected: str) -> None:
    assert ru.prepositional(name) == expected


@pytest.mark.parametrize(
    ("count", "word"), [(1, "файл"), (3, "файла"), (5, "файлов"), (11, "файлов"), (22, "файла")]
)
def test_plural(count: int, word: str) -> None:
    assert ru.plural(count, "файл", "файла", "файлов") == word
