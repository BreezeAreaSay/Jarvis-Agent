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
    ("query", "names", "whole", "partial"),
    [
        ("Маме", ["Мама", "Папа"], ["Мама"], []),
        ("Аня", ANYAS, [], ANYAS),  # часть имени у обеих — выбирает человек
        ("Ане", ["Аня Смирнова"], [], ["Аня Смирнова"]),  # и одна, но не целиком — тоже спросить
        ("Ане Смирновой", ANYAS, ["Аня Смирнова"], []),
        ("Аня", ["Аня", "Аня Смирнова"], ["Аня"], []),  # имя целиком важнее части имени
        ("Сергею", ["Сергей Иванов", "Сергей"], ["Сергей"], []),
        ("Марии", ["Мария"], ["Мария"], []),
        ("Игорю", ["Игорь"], ["Игорь"], []),
        ("Ивану", ["Иван 12:30"], ["Иван 12:30"], []),  # время в строке списка — не часть имени
        ("Аня", ["Анна", "Антон"], [], []),
        ("Мама", ["Mама"], ["Mама"], []),  # латинская M от распознавания
        ("Anna", ["Аппа|Anna"], ["Аппа|Anna"], []),  # два прочтения слова (ru|en)
        ("работа", ["Работа", "Аня (работа)"], ["Работа"], []),
        ("Избранному", ["Избранное", "Аня"], ["Избранное"], []),
        ("Дмитрию", ["Дмитрий Петров", "Дима"], [], ["Дмитрий Петров"]),
        ("Яне", ["Ян", "Яна"], ["Ян", "Яна"], []),  # одна основа — выбирает человек
        ("Ане", ["Папа", "Передай Ане привет"], [], ["Передай Ане привет"]),  # строка сообщения — спросить
        ("", ["Мама"], [], []),
    ],
)
def test_pick_chat_by_name(query: str, names: list[str], whole: list[str], partial: list[str]) -> None:
    assert tg.pick(query, names) == tg.Picked(whole, partial)


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
    assert tg.answer_pick("да", ["Аня Смирнова"]) == "Аня Смирнова"  # один вариант — «да»
    assert tg.answer_pick("нет", ["Аня Смирнова"]) == ""


def test_same_name_is_the_whole_name() -> None:
    assert tg.same_name("Аня Смирнова", "Аня Смирнова 12:30")
    assert tg.same_name("Мама", "Mама")  # латинская M
    for want, seen in [
        ("Аня", "Аня Смирнова"),
        ("Аня", "Аня (работа)"),
        ("Иванов", "Иванова"),
        ("Александр", "Александра"),
        ("Кира", "Ира"),
    ]:
        assert not tg.same_name(want, seen), (want, seen)


def test_voice_text_as_said() -> None:
    """Слоты команды — нормализованные слова; текст сообщения и имя — как их сказали."""
    from jarvis.commands.executor import _chat_name, _message_text, as_said

    said = "Джарвис, напиши Ане Смирновой в телеграме: буду в 7, позвоню из Москвы!"
    assert _message_text("буду в 7 позвоню из москвы", said) == "Буду в 7, позвоню из Москвы!"
    assert _chat_name(as_said(said, "ане смирновой")) == "Ане Смирновой"
    assert _message_text("что я опоздаю", "напиши маме в телеге, что я опоздаю") == "Я опоздаю"
    assert _message_text("чтобы купила хлеб", "") == "Чтобы купила хлеб"  # смысл не меняем
    assert _message_text("ещё раз", "") == "Ещё раз"
    assert as_said("совсем другое", "буду в 7") == "буду в 7"


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
        _line("Все чаты", (4, 120, 52, 140)),  # папки слева от поиска — не чаты
        _line("Аня Смирнова", (70, 120, 360, 150)),
        _line("Ок, договорились!", (70, 150, 360, 180)),
        _line("Глобальный поиск", (70, 190, 360, 210)),
        _line("Аня Смирнова", (70, 214, 360, 240)),  # чужой канал — ниже «Глобального поиска»
        _line("Аня Смирнова", (400, 42, 700, 64)),  # шапка — на высоте поля поиска
        _line("был(а) недавно", (400, 66, 700, 84)),
        _line("Мама", (400, 120, 470, 140)),  # имя отправителя в группе — уже не шапка
        _line("Привет!", (400, 600, 520, 640)),  # её — слева
        _line("Ок, договорились!", (900, 660, 1150, 700)),  # своё — справа
    )
    assert tg.search_field(snapshot) is SEARCH
    assert [r.name for r in tg.results(snapshot, SEARCH)] == ["Аня Смирнова", "Ок, договорились!"]
    assert tg.chat_open(snapshot, SEARCH, "Аня Смирнова") is not None
    assert tg.chat_open(snapshot, SEARCH, "Аня (работа)") is None
    assert tg.chat_open(snapshot, SEARCH, "Мама") is None
    assert tg.chat_title(snapshot, SEARCH) == "Аня Смирнова"
    assert tg.message_field(snapshot, SEARCH) is FIELD
    lines = tg.messages(snapshot, SEARCH, FIELD)
    assert lines == [tg.Line("Мама", False), tg.Line("Привет!", False), tg.Line("Ок, договорились!", True)]
    assert tg.mine_count(lines, "Ок, договорились!") == 1
    assert tg.mine_count(lines, "Привет!") == 0  # чужое — не отправленное


def test_search_and_message_field_from_recognized_text() -> None:
    """UI Automation полей не видит — поиск и поле сообщения по распознанным подсказкам; слово «сообщение»
    в сообщении внизу окна — не подсказка поля."""
    search = _line("Поиск", (70, 50, 120, 72))
    hint = _line("Написать сообщение...", (420, 750, 640, 772))
    words = _line("Напиши сообщение маме", (420, 690, 640, 712))
    snapshot = _snapshot(search, hint, words, _line("Поиск", (700, 400, 760, 420)))
    assert tg.search_field(snapshot) is search
    assert tg.message_field(snapshot, search) is hint
    assert tg.message_field(_snapshot(search, words), search) is None  # черновик закрыл подсказку


def test_draft_and_typed_text() -> None:
    assert tg.draft(_snapshot(FIELD), FIELD) == ""
    with_hint = UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Написать сообщение…")
    assert tg.draft(_snapshot(with_hint), with_hint) == ""
    with_draft = UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Купи хлеба")
    assert tg.draft(_snapshot(with_draft), with_draft) == "Купи хлеба"
    # значения поле не отдаёт — черновик виден распознанной строкой внутри поля
    drawn = _snapshot(FIELD, _line("Купи хлеба", (420, 750, 520, 772)))
    assert tg.draft(drawn, FIELD) == "Купи хлеба"
    typed = UiElement("e2", "Написать сообщение…", "Edit", FIELD.rect, value="Буду в 7")
    assert tg.typed_shown(_snapshot(typed), typed, "Буду в 7") is True
    assert tg.typed_shown(_snapshot(with_draft), with_draft, "Буду в 7") is False
    seen = _snapshot(FIELD, _line("Буду в 7", (420, 750, 520, 772)))
    assert tg.typed_shown(seen, FIELD, "Буду в 7") is True
    merged = _snapshot(FIELD, _line("Купи хлеба Буду в 7", (420, 750, 620, 772)))
    assert tg.typed_shown(merged, FIELD, "Буду в 7") is False  # черновик + наше — не отправлять
    assert tg.typed_shown(_snapshot(FIELD), FIELD, "Буду в 7") is None
    # подсказка распознана — после набора на её месте строка текста (длинный — его последняя строка)
    hint = _line("Написать сообщение...", (420, 750, 640, 772))
    assert tg.typed_shown(_snapshot(_line("Буду в 7", (420, 750, 500, 772))), hint, "Буду в 7") is True
    tail = _snapshot(_line("позвоню из Москвы", (420, 750, 600, 772)))
    assert tg.typed_shown(tail, hint, "Буду в 7, позвоню из Москвы") is True
    assert tg.typed_shown(_snapshot(_line("в 7", (420, 750, 460, 772))), hint, "Буду в 7") is False


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
