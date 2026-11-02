"""Telegram Desktop для действий агента: имя чата с падежом и ошибками распознавания, выбор из нескольких,
места в окне (поиск, найденные чаты, шапка, поле сообщения, сообщения) и запрет шагов составного действия."""

from typing import Any

import pytest

from jarvis.ai import telegram_desktop as tg
from jarvis.ai.uia import UiElement, UiSnapshot
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, calls, reply

ANYAS = ["Аня (работа)", "Аня Смирнова"]


@pytest.mark.parametrize(
    ("query", "names", "expected"),
    [
        ("Маме", ["Мама", "Папа"], ["Мама"]),
        ("Аня", ANYAS, ANYAS),  # обе — выбирает человек
        ("Ане Смирновой", ANYAS, ["Аня Смирнова"]),
        ("Аня", ["Аня", "Аня Смирнова"], ["Аня"]),  # имя целиком важнее части имени
        ("Сергею", ["Сергей Иванов", "Сергей"], ["Сергей"]),
        ("Марии", ["Мария"], ["Мария"]),
        ("Игорю", ["Игорь"], ["Игорь"]),
        ("Ивану", ["Иван 12:30"], ["Иван 12:30"]),  # время в строке списка — не часть имени
        ("Аня", ["Анна", "Антон"], []),
        ("Мама", ["Mама"], ["Mама"]),  # латинская M от распознавания
        ("Anna", ["Аппа|Anna"], ["Аппа|Anna"]),  # два прочтения слова (ru|en)
        ("работа", ["Работа", "Аня (работа)"], ["Работа"]),
        ("Избранному", ["Избранное", "Аня"], ["Избранное"]),
        ("Дмитрию", ["Дмитрий Петров", "Дима"], ["Дмитрий Петров"]),
        ("Яне", ["Ян", "Яна"], ["Ян", "Яна"]),  # одна основа — выбирает человек
        ("", ["Мама"], []),
    ],
)
def test_pick_chat_by_name(query: str, names: list[str], expected: list[str]) -> None:
    assert tg.pick(query, names) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Смирновой", "Аня Смирнова"),
        ("той, что с работы", "Аня (работа)"),
        ("второй", "Аня Смирнова"),
        ("2", "Аня Смирнова"),
        ("первая", "Аня (работа)"),
        ("не знаю", ""),
        ("", ""),
    ],
)
def test_answer_picks_one_of_the_chats(answer: str, expected: str) -> None:
    assert tg.answer_pick(answer, ANYAS) == expected


def test_stem_drops_case_endings() -> None:
    assert tg.stem("ане") == tg.stem("аня") == "ан"
    assert tg.stem("смирновой") == tg.stem("смирнова") == "смирнов"
    assert tg.stem("ян") == "ян"
    assert tg.stem("избранному") == tg.stem("избранное") == "избранн"
    assert tg.stem("марии") == tg.stem("мария") == tg.stem("марию") == "мар"
    assert tg.stem("сергею") == tg.stem("сергей") == "серге"


WINDOW = (0, 0, 1200, 800)


def _snapshot(*elements: UiElement) -> UiSnapshot:
    return UiSnapshot("Telegram", "Telegram.exe", list(elements), rect=WINDOW)


def _line(name: str, rect: tuple[int, int, int, int]) -> UiElement:
    return UiElement("", name, "Text", rect, ocr=True)


SEARCH = UiElement("e1", "Поиск", "Edit", (60, 44, 370, 80))
FIELD = UiElement("e2", "Написать сообщение…", "Edit", (400, 740, 1140, 790))


def test_places_in_the_window() -> None:
    snapshot = _snapshot(
        SEARCH,
        FIELD,
        _line("Аня Смирнова", (20, 120, 360, 150)),
        _line("Ок, договорились!", (20, 150, 360, 180)),
        _line("Аня Смирнова", (400, 42, 700, 64)),  # шапка
        _line("был(а) недавно", (400, 66, 700, 84)),
        _line("Привет!", (400, 600, 520, 640)),  # её — слева
        _line("Ок, договорились!", (900, 660, 1150, 700)),  # своё — справа
    )
    assert tg.search_field(snapshot) is SEARCH
    edge = tg.split_x(snapshot, SEARCH)
    assert [r.name for r in tg.results(snapshot, SEARCH)] == ["Аня Смирнова", "Ок, договорились!"]
    assert tg.chat_open(snapshot, edge, "Аня Смирнова") is not None
    assert tg.chat_open(snapshot, edge, "Аня (работа)") is None
    assert tg.chat_title(snapshot, edge) == "Аня Смирнова"
    assert tg.message_field(snapshot, edge) is FIELD
    lines = tg.messages(snapshot, edge, FIELD)
    assert lines == [tg.Line("Привет!", False), tg.Line("Ок, договорились!", True)]
    assert tg.mine_count(lines, "Ок, договорились!") == 1
    assert tg.mine_count(lines, "Привет!") == 0  # чужое — не отправленное


def test_search_and_message_field_from_recognized_text() -> None:
    """UI Automation полей не видит — поиск и поле сообщения по распознанным подсказкам."""
    search = _line("Поиск", (70, 50, 120, 72))
    hint = _line("Написать сообщение...", (420, 750, 640, 772))
    snapshot = _snapshot(search, hint, _line("Поиск", (700, 400, 760, 420)))  # «Поиск» в сообщении — не поле
    assert tg.search_field(snapshot) is search
    assert tg.message_field(snapshot, tg.split_x(snapshot, search)) is hint


def test_draft_and_typed_text() -> None:
    edge = 380
    assert tg.draft(FIELD) == ""
    assert (
        tg.draft(UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Написать сообщение…"))
        == ""
    )
    with_draft = UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Купи хлеба")
    assert tg.draft(with_draft) == "Купи хлеба"
    typed = UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Буду в 7")
    assert tg.typed_shown(_snapshot(typed), edge, typed, "Буду в 7") is True
    assert tg.typed_shown(_snapshot(with_draft), edge, with_draft, "Буду в 7") is False
    # поле значения не отдаёт — набранное видно распознанной строкой внизу окна
    seen = _snapshot(FIELD, _line("Буду в 7", (420, 750, 520, 772)))
    assert tg.typed_shown(seen, edge, FIELD, "Буду в 7") is True
    assert tg.typed_shown(_snapshot(FIELD), edge, FIELD, "Буду в 7") is None


@pytest.mark.parametrize("step", ["type_text", "press_keys", "click_point"])
async def test_steps_denied_deny_composite_action(ctx: AppContext, llm: ScriptedLlm, step: str) -> None:
    """Запрещено «Печатать текст» (или клавиши, или щелчок мышью) — telegram_send тоже не выполняется."""
    ctx.settings.patch({"ai": {"vision": "off", "tool_policy": {f"agent.{step}": "deny"}}})
    llm.add(
        reply('{"steps": ["Написать Маме"]}'),
        calls(("telegram_send", {"to": "Маме", "text": "Буду в 7"})),
        calls(("finish", {"status": "failed", "summary": "Нельзя."})),
    )
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Напиши Маме в Telegram: «Буду в 7»"))
    tools = [str(m.get("content")) for m in llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert "запрещено в настройках" in tools[0]
    platform: Any = ctx.platform
    assert not [c for c in platform.calls if c[0] in ("type_text", "press_keys", "mouse_click")]
