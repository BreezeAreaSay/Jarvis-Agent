"""«Входящие.md»: какие строки — новые задачи, подробности с отступом, отметка взятых."""

from jarvis.background import inbox


def test_template_has_no_tasks() -> None:
    assert inbox.parse(inbox.TEMPLATE) == []


def test_parse_items_and_details() -> None:
    text = (
        "---\ntags: [jarvis]\n---\n"
        "# Входящие\n\n"
        "> [!tip] Задачи для Jarvis\n> Пишите сюда\n\n"
        "- Найди 3 лучших робота-пылесоса до 30 000 ₽\n"
        "  - с хорошей уборкой шерсти\n"
        "  только Яндекс Маркет\n"
        "- [ ] каждый вечер записывай курс доллара\n"
        "- [x] Уже сделанное — [[Jarvis/Задачи/Сделано|задача]]\n"
        "  - подробность сделанного\n"
        "* [-] Отменённое\n"
        "1. Сравни тарифы мобильной связи\n"
        "Просто строка без маркера\n"
        "| Таблица | нет |\n"
        "<!-- комментарий\n- не задача\n-->\n"
        "```\n- код, не задача\n```\n"
        "- ок\n"  # слишком коротко
    )
    items = inbox.parse(text)
    assert [i.text for i in items] == [
        "Найди 3 лучших робота-пылесоса до 30 000 ₽",
        "каждый вечер записывай курс доллара",
        "Сравни тарифы мобильной связи",
        "Просто строка без маркера",
    ]
    assert items[0].details == ["с хорошей уборкой шерсти", "только Яндекс Маркет"]
    assert items[0].brief == (
        "Найди 3 лучших робота-пылесоса до 30 000 ₽\nс хорошей уборкой шерсти\nтолько Яндекс Маркет"
    )
    lines = text.splitlines()
    assert lines[items[1].line] == "- [ ] каждый вечер записывай курс доллара"


def test_mark_taken_items_once() -> None:
    text = "# Входящие\n\n- Найди пылесос\n  - до 30 000 ₽\n  * Сравни тарифы\n1. Обзор новостей\n"
    items = inbox.parse(text)
    assert [i.text for i in items] == ["Найди пылесос", "Обзор новостей"]  # «* Сравни…» — подробность
    marked = inbox.mark(text, {items[0].line: "Jarvis/Задачи/2026-10-30 Найди пылесос.md", items[1].line: ""})
    assert marked.splitlines()[2] == "- [x] Найди пылесос — [[Jarvis/Задачи/2026-10-30 Найди пылесос|задача]]"
    assert marked.splitlines()[5] == "- [x] Обзор новостей"
    assert marked.endswith("\n")
    assert inbox.parse(marked) == []  # второй раз не берётся
