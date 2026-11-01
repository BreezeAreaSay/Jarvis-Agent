"""Таблицы замеров: строка по дате (та же дата — заменяется), новые колонки, числа, график SVG, чтение для
интерфейса."""

from datetime import datetime
from pathlib import Path

import pytest

from jarvis.background import measure
from jarvis.background.vault import Vault
from jarvis.context import AppContext
from jarvis.services import UserFacingError


@pytest.fixture
def vault(ctx: AppContext, tmp_path: Path) -> Vault:
    root = tmp_path / "Мои заметки"
    (root / ".obsidian").mkdir(parents=True)
    ctx.settings.patch({"background": {"vault": str(root)}})
    return Vault(ctx)


def at(day: int, hour: int = 21) -> datetime:
    return datetime(2026, 10, day, hour, 5)


def test_parse_number() -> None:
    assert measure.parse_number("81,42 ₽") == 81.42
    assert measure.parse_number("1 234,5") == 1234.5
    assert measure.parse_number("12 000 ₽") == 12000
    assert measure.parse_number("−3.2%") == -3.2
    assert measure.parse_number("нет данных") is None


def test_normalize_date() -> None:
    assert measure.normalize_date("28.10.2026", hourly=False) == "2026-10-28"
    assert measure.normalize_date("2026-10-28T21:05", hourly=True) == "2026-10-28 21:00"
    assert measure.normalize_date("вчера", hourly=False) == "вчера"


def test_rows_by_date_replace_and_new_columns(vault: Vault) -> None:
    first = measure.add_row(vault, "Курс доллара", {"Курс ЦБ, ₽": "81,10"}, now=at(28))
    assert first["path"] == "Jarvis/Замеры/Курс доллара.md" and not first["replaced"] and not first["chart"]
    measure.add_row(vault, "Курс доллара", [{"column": "Курс ЦБ, ₽", "value": "81,42"}], now=at(29))
    again = measure.add_row(vault, "Курс доллара", {"Курс ЦБ, ₽": "81,50"}, now=at(29, 22))  # тот же день
    assert again["replaced"] and again["rows"] == 2 and again["chart"]
    measure.add_row(vault, "Курс доллара.md", {"Курс ЦБ, ₽": "81,7", "Евро | ЦБ": "94,6"}, now=at(30))
    note = (vault.root / "Jarvis/Замеры/Курс доллара.md").read_text("utf-8")
    columns, rows = measure.read_table(note)
    assert columns == ["Дата", "Курс ЦБ, ₽", "Евро / ЦБ"]
    assert rows == [
        ["2026-10-28", "81,10", ""],
        ["2026-10-29", "81,50", ""],
        ["2026-10-30", "81,7", "94,6"],
    ]
    assert "![[Jarvis/Замеры/Курс доллара.svg]]" in note and note.count("![[") == 1
    assert note.startswith("---\ntype: jarvis-measurements") and "# Курс доллара" in note
    svg = (vault.root / "Jarvis/Замеры/Курс доллара.svg").read_text("utf-8")
    assert svg.startswith("<svg") and "Курс ЦБ, ₽: 81,7" in svg and "<path" in svg


def test_human_edits_around_table_survive(vault: Vault) -> None:
    path = vault.root / "Jarvis" / "Замеры" / "Вес.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Вес\n\nМои заметки сверху.\n\n| Дата | Вес, кг |\n|---|---|\n| 2026-10-28 | 80,5 |\n\nИ снизу.\n",
        "utf-8",
    )
    measure.add_row(vault, "Вес", {"Вес, кг": "80,1", "Дата": "29.10.2026"}, now=at(30))
    text = path.read_text("utf-8")
    assert "Мои заметки сверху." in text and "И снизу." in text
    assert measure.read_table(text)[1] == [["2026-10-28", "80,5"], ["2026-10-29", "80,1"]]


def test_hourly_rows_and_errors(vault: Vault) -> None:
    measure.add_row(vault, "Биткоин", {"Цена, $": "68 000"}, hourly=True, now=at(30, 9))
    measure.add_row(vault, "Биткоин", {"Цена, $": "68 500"}, hourly=True, now=at(30, 12))
    rows = measure.read_table((vault.root / "Jarvis/Замеры/Биткоин.md").read_text("utf-8"))[1]
    assert [r[0] for r in rows] == ["2026-10-30 09:00", "2026-10-30 12:00"]
    with pytest.raises(UserFacingError):
        measure.add_row(vault, "Биткоин", {"Цена, $": ""})
    # имя таблицы — только имя: папка всегда своя, «Замеры/»
    assert measure.add_row(vault, "../../наружу", {"x": "1"})["path"] == "Jarvis/Замеры/наружу.md"
    assert not (vault.root.parent / "наружу.md").exists()


def test_load_for_ui(vault: Vault) -> None:
    for day, value in ((27, "80,9"), (28, "81,1"), (29, "81,4")):
        measure.add_row(vault, "Курс доллара", {"Курс ЦБ, ₽": value, "Источник": "cbr.ru"}, now=at(day))
    table = measure.load(vault, "Jarvis/Замеры/Курс доллара.md")
    assert table is not None
    assert table["columns"] == ["Дата", "Курс ЦБ, ₽", "Источник"] and table["numeric"] == [1]
    assert table["total"] == 3 and table["rows"][-1] == ["2026-10-29", "81,4", "cbr.ru"]
    assert table["link"].startswith("obsidian://open?")
    assert measure.load(vault, "Jarvis/Замеры/Нет такой.md") is None


def test_review_fixes_names_hours_and_pipes(vault: Vault) -> None:
    # «/» в названии — не папка: USD/RUB и EUR/RUB — разные таблицы
    usd = measure.add_row(vault, "Курс USD/RUB", {"Курс ЦБ, ₽": "81,4"}, now=at(30))
    eur = measure.add_row(vault, "Курс EUR/RUB", {"Курс ЦБ, ₽": "94,6"}, now=at(30))
    assert usd["path"] == "Jarvis/Замеры/Курс USD-RUB.md" and eur["path"] == "Jarvis/Замеры/Курс EUR-RUB.md"
    # частый замер с датой без часа: сегодня — текущий час, а не 00:00 (иначе все замеры дня — одна строка)
    measure.add_row(vault, "Биткоин", {"Дата": "30.10.2026", "Цена, $": "68 000"}, hourly=True, now=at(30, 9))
    measure.add_row(
        vault, "Биткоин", {"Дата": "2026-10-30", "Цена, $": "68 500"}, hourly=True, now=at(30, 12)
    )
    rows = measure.read_table((vault.root / "Jarvis/Замеры/Биткоин.md").read_text("utf-8"))[1]
    assert [r[0] for r in rows] == ["2026-10-30 09:00", "2026-10-30 12:00"]
    assert measure.normalize_date("29.10.2026", hourly=True, now=at(30, 12)) == "2026-10-29 00:00"
    # ссылка с подписью в ячейке человека ([[заметка\|подпись]]) — не граница ячейки
    path = vault.root / "Jarvis" / "Замеры" / "Вес.md"
    path.write_text(
        "| Дата | Вес, кг | Где |\n|---|---|---|\n| 2026-10-28 | 80,5 | [[Спорт\\|зал]] |\n", "utf-8"
    )
    measure.add_row(vault, "Вес", {"Вес, кг": "80,1"}, now=at(29))
    columns, rows = measure.read_table(path.read_text("utf-8"))
    assert columns == ["Дата", "Вес, кг", "Где"] and rows[0] == ["2026-10-28", "80,5", "[[Спорт\\|зал]]"]
    assert "[[Спорт\\|зал]]" in path.read_text("utf-8")
