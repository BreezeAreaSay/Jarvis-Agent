"""Текст для голоса: чистка markdown/ссылок/эмодзи, латиница русскими буквами, проценты, предложения."""

import pytest

from jarvis.voice.textprep import prepare, split_sentences, transliterate


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("Telegram", "телеграм"),
        ("YouTube", "ютуб"),
        ("GPT", "джипити"),
        ("NBA", "эн би эй"),
        ("USB", "ю эс би"),
        ("store", "стор"),
        ("Spotify", "спотифай"),
        ("Blender", "блендер"),
        ("x", "экс"),
    ],
)
def test_transliterate(word: str, expected: str) -> None:
    assert transliterate(word) == expected


def test_transliterate_rules_give_cyrillic() -> None:
    out = transliterate("Shadowheart")
    assert out and all(not ("a" <= ch <= "z") for ch in out.lower())


def test_prepare_russian_voice() -> None:
    assert prepare("Открыл Telegram, сэр.") == "Открыл телеграм, сэр."
    assert prepare("Громкость 50%") == "Громкость 50 процентов"
    assert prepare("Рост на 1% и на 3%, а курс 2.5") == "Рост на 1 процент и на 3 процента, а курс 2,5"
    assert prepare("Готово 👍 сэр!!!") == "Готово сэр!"


def test_prepare_strips_markup_links_code() -> None:
    text = """# Итоги
**Важно:** смотрите [документацию](https://example.com/docs) и `config.toml`.
- первый пункт
- второй пункт

```python
print("hello")
```
| a | b |
|---|---|
Файл лежит в C:\\Users\\me\\report.docx, ссылка https://x.y/z.
"""
    out = prepare(text)
    assert "**" not in out and "#" not in out and "```" not in out and "|" not in out
    assert "http" not in out and "print" not in out and "C:" not in out
    assert "документацию" in out and "Важно" in out and "первый пункт" in out
    assert "конфиг" in out or "config" not in out  # короткий код оставлен и прочитан по-русски


def test_prepare_non_russian_engine_keeps_latin() -> None:
    assert prepare("Opened **Telegram** 50%", russian=False) == "Opened Telegram 50%"


def test_prepare_empty_and_symbols_only() -> None:
    assert prepare("") == ""
    assert prepare("🙂 ✨ ---") == ""
    assert prepare("```\ncode only\n```") == ""


def test_split_sentences() -> None:
    parts = split_sentences("Добрый день, сэр. Все системы работают в штатном режиме! Что дальше?")
    assert parts == ["Добрый день, сэр.", "Все системы работают в штатном режиме!", "Что дальше?"]
    assert split_sentences("Да. Конечно, сэр.") == ["Да. Конечно, сэр."]  # короткое — со следующим


def test_split_long_sentence_by_commas() -> None:
    long = ", ".join(f"пункт номер {i} с пояснением" for i in range(30)) + "."
    parts = split_sentences(long, max_len=120)
    assert len(parts) > 1 and all(len(p) <= 120 for p in parts)
    assert " ".join(parts).replace("  ", " ").startswith("пункт номер 0")


def test_split_without_punctuation_by_words() -> None:
    parts = split_sentences("слово " * 100, max_len=50)
    assert all(len(p) <= 50 for p in parts) and len(parts) >= 10
