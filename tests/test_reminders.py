"""Напоминания: разбор времени (чистая функция), файл <data>\\reminders.json, API для приложения
(pop_due, next_due, version), отмена по номеру, тексту, «последнее», «все»."""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from jarvis import reminders
from jarvis.memory import StoreError

NOW = datetime(2026, 10, 11, 12, 0, 0).timestamp()  # воскресенье, полдень (местное время)


@pytest.fixture(autouse=True)
def _fresh_fired() -> None:
    reminders._fired.clear()
    yield
    reminders._fired.clear()


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute, second).timestamp()


# --- parse_when -----------------------------------------------------------------------------------------

PARSE = [
    # относительное время
    ("через 10 минут вынуть пиццу", NOW + 600, "вынуть пиццу"),
    ("через 5 мин проверить", NOW + 300, "проверить"),
    ("через минуту", NOW + 60, ""),
    ("через час позвонить", NOW + 3600, "позвонить"),
    ("через полчаса проверить духовку", NOW + 1800, "проверить духовку"),
    ("через полтора часа выйти", NOW + 5400, "выйти"),
    ("через 2 часа 30 минут", NOW + 9000, ""),
    ("через час и 15 минут встреча", NOW + 4500, "встреча"),
    ("через пару минут", NOW + 120, ""),
    ("через пять минут чай", NOW + 300, "чай"),
    ("через двадцать пять минут", NOW + 1500, ""),
    ("через 1,5 часа", NOW + 5400, ""),
    ("через 1.5 часа", NOW + 5400, ""),
    ("через 2 с половиной часа", NOW + 9000, ""),
    ("через два часа с половиной", NOW + 9000, ""),
    ("через 30 секунд", NOW + 30, ""),
    ("через день полить цветы", NOW + 86400, "полить цветы"),
    ("через неделю", NOW + 7 * 86400, ""),
    ("Через 10 Минут Вынуть Пиццу", NOW + 600, "Вынуть Пиццу"),
    # время на часах: сегодня, а прошедшее — завтра
    ("в 18:30 вынуть пиццу", at(11, 18, 30), "вынуть пиццу"),
    ("вынуть пиццу в 18:30", at(11, 18, 30), "вынуть пиццу"),
    ("в 18 30 созвон", at(11, 18, 30), "созвон"),
    ("созвон в 18 30", at(11, 18, 30), "созвон"),
    ("в 18.30 созвон", at(11, 18, 30), "созвон"),
    ("в 9 позвонить", at(12, 9), "позвонить"),
    ("в 11:59 позвонить", at(12, 11, 59), "позвонить"),
    ("завтра в 8:15 позвонить маме", at(12, 8, 15), "позвонить маме"),
    ("послезавтра в 10", at(13, 10), ""),
    ("в 7 вечера ужин", at(11, 19), "ужин"),
    ("в 9 утра", at(12, 9), ""),
    ("в 3 дня", at(11, 15), ""),
    ("в 12 ночи", at(12, 0), ""),
    ("в 11 ночи", at(11, 23), ""),
    ("в 8:30 вечера", at(11, 20, 30), ""),
    ("сегодня в 9 кино", at(11, 21), "кино"),  # «сегодня в 9», сказанное днём, — вечер
    ("в полдень обед", at(12, 12), "обед"),
    ("в час дня обед", at(11, 13), "обед"),
    ("завтра вечером в 7 позвонить", at(12, 19), "позвонить"),
    ("в 9 часов 30 минут", at(12, 9, 30), ""),
    ("в восемь пятнадцать", at(12, 8, 15), ""),
    ("в девять утра завтра", at(12, 9), ""),
    ("к 9 подготовить отчёт", at(12, 9), "подготовить отчёт"),
    ("поставить машину на 18:30", at(11, 18, 30), "поставить машину"),
    ("про встречу в 10", at(12, 10), "про встречу"),
    # связки и знаки
    ("в 18:30, вынуть пиццу!", at(11, 18, 30), "вынуть пиццу"),
    ("мне что надо позвонить через 10 минут", NOW + 600, "надо позвонить"),
    ("о том, что надо позвонить через 10 минут", NOW + 600, "надо позвонить"),
    ("через 10 минут чтобы не забыть ключи", NOW + 600, "не забыть ключи"),
]


@pytest.mark.parametrize(("text", "due", "rest"), PARSE, ids=[p[0] for p in PARSE])
def test_parse_when(text: str, due: float, rest: str) -> None:
    assert reminders.parse_when(text, NOW) == (due, rest)


@pytest.mark.parametrize(
    "text",
    [
        "", "вынуть пиццу", "через минут", "через сколько закипит вода", "что такое dns",
        "как зовут актёра из матрицы", "в 25:00", "в 9:75", "в 15 утра", "купить билеты на 3 часа",
        "увеличить в 2 раза", "через 400 дней", "через 0 минут", "сегодня в 11 утра", "завтра",
        "позвонить завтра", "в субботу", "завтра утром",
    ],
)  # fmt: skip
def test_parse_when_none(text: str) -> None:
    assert reminders.parse_when(text, NOW) is None


def test_parse_when_today_passed_explicitly() -> None:
    late = at(11, 22)
    assert reminders.parse_when("сегодня в 9 кино", late) is None  # и 21:00 уже прошло
    assert reminders.parse_when("в 9 кино", late) == (at(12, 9), "кино")


def test_parse_when_default_now_is_current_time() -> None:
    import time

    parsed = reminders.parse_when("через 10 минут чай")
    assert parsed is not None and abs(parsed[0] - (time.time() + 600)) < 5


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("10 минут", (600.0, "10 минут", "")),
        ("на полчаса для пиццы", (1800.0, "полчаса", "для пиццы")),
        ("на 3 минуты, для яиц", (180.0, "3 минуты", "для яиц")),
        ("пять минут", (300.0, "пять минут", "")),
        ("час", (3600.0, "час", "")),
        ("пиццу", None),
        ("минут", None),
        ("400 дней", None),
    ],
)
def test_parse_duration(text: str, result: tuple | None) -> None:
    assert reminders.parse_duration(text) == result


@pytest.mark.parametrize(
    ("due", "text"),
    [
        (NOW + 30, "через 30 секунд"),
        (NOW + 1, "через 1 секунду"),
        (NOW + 600, "в 12:10"),
        (at(11, 8, 5), "в 8:05"),
        (at(12, 8, 15), "завтра в 8:15"),
        (at(13, 10), "послезавтра в 10:00"),
        (at(10, 18, 30), "вчера в 18:30"),
        (at(25, 9), "25 октября в 9:00"),
        (datetime(2027, 1, 3, 7, 0).timestamp(), "3 января 2027 в 7:00"),
    ],
)
def test_format_due(due: float, text: str) -> None:
    assert reminders.format_due(due, NOW) == text


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "запись"), (2, "записи"), (4, "записи"), (5, "записей"), (11, "записей"), (21, "запись"),
     (22, "записи"), (112, "записей"), (0, "записей")],
)  # fmt: skip
def test_plural(n: int, word: str) -> None:
    assert reminders.plural(n, ("запись", "записи", "записей")) == word


# --- файл и API приложения ------------------------------------------------------------------------------


def test_add_pending_next_due_pop_due(_isolated_data: Path) -> None:
    assert reminders.pending() == [] and reminders.next_due() is None and reminders.version() == 0
    late = reminders.add("позвонить маме", NOW + 3600, now=NOW)
    soon = reminders.add("вынуть пиццу", NOW + 600, now=NOW)
    assert reminders.version() > 0
    assert reminders.pending() == [soon, late] and reminders.next_due() == NOW + 600
    raw = json.loads((_isolated_data / "reminders.json").read_text(encoding="utf-8"))
    assert {r["text"] for r in raw} == {"позвонить маме", "вынуть пиццу"}
    assert set(raw[0]) == {"id", "text", "due_ts", "created_ts"}
    assert reminders.pop_due(NOW + 599) == []
    assert reminders.pop_due(NOW + 600) == [soon]  # сработавшее удаляется
    assert reminders.pop_due(NOW + 600) == []
    assert reminders.pending() == [late]
    assert reminders.pop_due(NOW + 10**6) == [late] and reminders.next_due() is None
    assert sorted(p.name for p in _isolated_data.iterdir()) == ["reminders.json"]  # без временных файлов


def test_pop_due_returns_missed_in_order() -> None:
    """Jarvis не работал: при старте pop_due отдаёт пропущенные, старые первыми."""
    b = reminders.add("второе", NOW - 60, now=NOW - 7200)
    a = reminders.add("первое", NOW - 3600, now=NOW - 7200)
    assert reminders.pop_due(NOW) == [a, b]


def test_pop_due_save_failure_shows_once(monkeypatch: pytest.MonkeyPatch) -> None:
    r = reminders.add("вынуть пиццу", NOW + 10, now=NOW)

    def deny(src: object, dst: object) -> None:
        raise PermissionError(13, "занято")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", deny)
        m.setattr("jarvis.memory.REPLACE_PAUSE_S", 0)
        assert reminders.pop_due(NOW + 20) == [r]
        assert reminders.pop_due(NOW + 30) == []  # файл не переписался, но второй раз не показываем
    assert reminders.pending() == [] and reminders.next_due() is None


def test_version_changes_on_every_write(_isolated_data: Path) -> None:
    v0 = reminders.version()
    reminders.add("раз", NOW + 60, now=NOW)
    v1 = reminders.version()
    path = _isolated_data / "reminders.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # «другой процесс» переписал файл
    assert v0 == 0 and v1 > 0 and reminders.version() != v1


def test_add_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(StoreError, match="когда"):
        reminders.add("x", float("nan"), now=NOW)
    with pytest.raises(StoreError, match="когда"):
        reminders.add("x", "завтра", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="год"):
        reminders.add("x", NOW + reminders.MAX_AHEAD_S + 60, now=NOW)
    with pytest.raises(StoreError, match="короче"):
        reminders.add("а" * (reminders.TEXT_MAX + 1), NOW + 60, now=NOW)
    assert reminders.add("", NOW + 60, now=NOW).text == reminders.DEFAULT_TEXT
    assert reminders.add("строка\nдва", NOW + 60, now=NOW).text == "строка два"
    monkeypatch.setattr(reminders, "LIMIT", 2)
    with pytest.raises(StoreError, match="много"):
        reminders.add("третье", NOW + 60, now=NOW)


def test_cancel_by_number_last_all_text() -> None:
    pizza = reminders.add("вынуть пиццу", NOW + 600, now=NOW + 1)
    mom = reminders.add("позвонить маме", NOW + 3600, now=NOW + 3)
    milk = reminders.add("купить молоко", NOW + 1800, now=NOW + 2)
    assert reminders.cancel(2) == ([milk], [])  # номер — как в pending(): по времени
    assert reminders.cancel("2") == ([mom], [])
    assert reminders.cancel(5) == ([], [])
    assert reminders.cancel(True) == ([], [])  # type: ignore[arg-type]
    mom = reminders.add("позвонить маме", NOW + 3600, now=NOW + 5)
    assert reminders.cancel("последнее") == ([mom], [])  # созданное последним, а не позднее всех
    assert reminders.cancel("пиццы") == ([pizza], [])  # часть текста, падеж
    assert reminders.cancel("пицца") == ([], [])
    a = reminders.add("раз", NOW + 60, now=NOW)
    b = reminders.add("два", NOW + 120, now=NOW)
    assert reminders.cancel("всё") == ([a, b], []) and reminders.pending() == []


def test_cancel_ambiguous_cancels_nothing() -> None:
    a = reminders.add("позвонить маме", NOW + 600, now=NOW)
    b = reminders.add("позвонить в банк", NOW + 1200, now=NOW)
    assert reminders.cancel("позвонить") == ([], [a, b])
    assert reminders.cancel(None) == ([], [a, b])  # «отмени напоминание» при двух — какое?
    assert reminders.pending() == [a, b]
    reminders.cancel(1)
    assert reminders.cancel(None) == ([b], [])  # одно — его
    assert reminders.cancel(None) == ([], [])


def test_cancel_ids_and_restore() -> None:
    a = reminders.add("раз", NOW + 60, now=NOW)
    b = reminders.add("два", NOW + 120, now=NOW)
    assert reminders.cancel_ids([a.id, "нет"]) == [a] and reminders.pending() == [b]
    record = {"id": a.id, "text": "раз", "due_ts": NOW + 60, "created_ts": NOW}
    assert reminders.restore([record, {"id": b.id, "text": "два", "due_ts": 1.0}]) == [a]
    assert reminders.pending() == [a, b]
    assert reminders.restore([{"text": "без id"}, 5]) == []  # type: ignore[list-item]


def test_broken_file(_isolated_data: Path, caplog: pytest.LogCaptureFixture) -> None:
    (_isolated_data / "reminders.json").write_text("[{битый", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jarvis"):
        assert reminders.pending() == [] and reminders.next_due() is None and reminders.pop_due(NOW) == []
    assert (_isolated_data / "reminders.json.bad").exists()
    assert reminders.add("снова", NOW + 60, now=NOW).text == "снова"


def test_bad_records_skipped(_isolated_data: Path) -> None:
    raw = [
        {"id": "a", "text": "хорошее", "due_ts": NOW + 60, "created_ts": NOW},
        {"id": "b", "text": "без времени"},
        {"id": "c", "text": "nan", "due_ts": float("inf")},
        {"id": "d", "text": "", "due_ts": NOW + 30},
        {"text": "без id", "due_ts": NOW},
        [1, 2],
    ]
    (_isolated_data / "reminders.json").write_text(json.dumps(raw), encoding="utf-8")
    got = reminders.pending()
    assert [(r.id, r.text) for r in got] == [("d", reminders.DEFAULT_TEXT), ("a", "хорошее")]
    assert got[0].created_ts == NOW + 30  # нет created_ts — как due


def test_concurrent_add() -> None:
    threads = [threading.Thread(target=reminders.add, args=(f"№{i}", NOW + 60 + i)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.text for r in reminders.pending()] == [f"№{i}" for i in range(10)]
