"""Память о пользователе: запись только командой человека, атомарный файл, кэш по mtime, поиск по части
текста, контекст для мозга и защита префикса рук (память туда не попадает)."""

import json
import logging
import os
import threading
from pathlib import Path

import pytest

from jarvis import hands, memory
from jarvis.context import Context
from jarvis.memory import JsonList, StoreError
from pc.windows import WindowInfo


def data_file(data: Path) -> Path:
    return data / memory.FILE_NAME


def texts() -> list[str]:
    return [i.text for i in memory.items()]


# --- запомнить / забыть ----------------------------------------------------------------------------------


def test_remember_writes_utf8_json_atomically(_isolated_data: Path) -> None:
    item, added = memory.remember("  я пью кофе\tбез сахара ", now=100.0)
    assert added and item.text == "я пью кофе без сахара" and item.ts == 100.0 and item.id
    raw = data_file(_isolated_data).read_text(encoding="utf-8")
    assert "кофе" in raw  # ensure_ascii=False
    assert json.loads(raw) == [{"id": item.id, "text": "я пью кофе без сахара", "ts": 100.0}]
    assert sorted(p.name for p in _isolated_data.iterdir()) == ["memory.json"]  # без временных файлов


def test_remember_duplicate_is_not_added() -> None:
    first, _ = memory.remember("Меня зовут Дима")
    again, added = memory.remember("меня  зовут дима")
    assert not added and again == first and texts() == ["Меня зовут Дима"]


@pytest.mark.parametrize("text", ["", " ", "я", "\n"])
def test_remember_refuses_empty(text: str) -> None:
    with pytest.raises(StoreError):
        memory.remember(text)


def test_remember_refuses_too_long() -> None:
    with pytest.raises(StoreError, match="короче"):
        memory.remember("а" * (memory.TEXT_MAX + 1))
    assert memory.remember("б" * memory.TEXT_MAX)[1]


@pytest.mark.parametrize(
    "text",
    ["мой пароль от почты qwerty", "PIN-код карты 4321", "pin 1234", "api key для бота", "сид-фраза кошелька",
     "токен бота", "Password: hunter"],
)  # fmt: skip
def test_remember_refuses_secrets(text: str) -> None:
    """Память уходит в облако с каждым вопросом GPT: пароли и ключи туда не кладём."""
    with pytest.raises(StoreError, match="Пароли"):
        memory.remember(text)
    assert memory.items() == []


@pytest.mark.parametrize("text", ["ключи от машины в ящике", "мой пинг в играх 20", "спиннинг на рыбалку"])
def test_remember_allows_lookalikes(text: str) -> None:
    assert memory.remember(text)[1]


def test_remember_control_chars_become_spaces() -> None:
    item, _ = memory.remember("строка\nдва\x1b[31m три")
    assert item.text == "строка два [31m три"


def test_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "LIMIT", 2)
    memory.remember("раз")
    memory.remember("два")
    with pytest.raises(StoreError, match="полна"):
        memory.remember("три")
    assert texts() == ["раз", "два"]


def test_forget_by_number_text_last_and_all() -> None:
    for i, text in enumerate(["моя машина синяя", "я пью кофе без сахара", "кофе по утрам — капучино"]):
        memory.remember(text, now=100.0 + i)
    assert [i.text for i in memory.forget(1)] == ["моя машина синяя"]
    assert memory.forget(5) == [] and memory.forget(0) == []
    assert [i.text for i in memory.forget("последнее")] == ["кофе по утрам — капучино"]
    memory.remember("моя машина синяя", now=200.0)
    assert [i.text for i in memory.forget("машину")] == ["моя машина синяя"]  # падеж: «машину» ~ «машина»
    assert memory.forget("велосипед") == []
    memory.remember("кофе — только зерновой", now=300.0)
    assert len(memory.forget("всё")) == 2 and memory.items() == []


def test_forget_text_removes_every_match() -> None:
    memory.remember("я пью кофе без сахара")
    memory.remember("кофе по утрам")
    memory.remember("моя машина синяя")
    gone = memory.forget("кофе")
    assert [i.text for i in gone] == ["я пью кофе без сахара", "кофе по утрам"]
    assert texts() == ["моя машина синяя"]


def test_forget_digit_string_and_bool() -> None:
    memory.remember("раз")
    memory.remember("два")
    assert [i.text for i in memory.forget("2")] == ["два"]
    assert memory.forget(True) == []  # type: ignore[arg-type]


def test_forget_ids_and_restore_keep_order() -> None:
    a, _ = memory.remember("раз", now=1.0)
    b, _ = memory.remember("два", now=2.0)
    c, _ = memory.remember("три", now=3.0)
    gone = memory.forget_ids([b.id, "нет-такого"])
    assert gone == [b] and texts() == ["раз", "три"]
    back = [{"id": b.id, "text": "два", "ts": 2.0}, {"id": a.id, "text": "раз", "ts": 1.0}]
    assert memory.restore(back) == [b]
    assert texts() == ["раз", "два", "три"]  # по времени, дубль id пропущен
    assert memory.restore([{"мусор": 1}, "строка"]) == []  # type: ignore[list-item]
    assert c in memory.items()


@pytest.mark.parametrize(
    ("query", "text", "ok"),
    [
        ("кофе", "я пью кофе без сахара", True),
        ("про машину", "моя машина синяя", True),
        ("день рождения", "день рождения мамы 5 мая", True),
        ("ДНЯ РОЖДЕНИЯ", "день рождения мамы", False),  # «дня» — не начало «день»
        ("пиццу", "вынуть пиццу из духовки", True),
        ("пицца", "вынуть пиццу", True),
        ("про", "что угодно про всё", False),  # одни служебные слова — не запрос
        ("ёлка", "купить елку", True),
        ("чай", "я пью кофе", False),
    ],
)
def test_matches(query: str, text: str, ok: bool) -> None:
    assert memory.matches(query, text) is ok


# --- контекст для мозга --------------------------------------------------------------------------------


def test_as_context_newest_first_within_limit_in_order() -> None:
    assert memory.as_context() == ""
    for i, text in enumerate(["раз", "два", "три"]):
        memory.remember(text, now=float(i))
    assert memory.as_context() == "раз; два; три"
    assert memory.as_context(limit_chars=8) == "два; три"  # не влезает — самые старые не уходят
    assert memory.as_context(limit_chars=2) == ""


def test_as_context_full_memory_fits_600() -> None:
    for i in range(memory.LIMIT):
        memory.remember(f"заметка номер {i} " + "x" * 150)
    assert 0 < len(memory.as_context()) <= memory.CONTEXT_CHARS


# --- руки: память не трогает префикс и сообщение ----------------------------------------------------------


def test_hands_prefix_and_message_do_not_depend_on_memory() -> None:
    ctx = Context(active_window=WindowInfo(1, "Почта", 2, "mail.exe"))
    ctx.remember("отчёт.docx", "file")
    prefix, message = hands.prefix_bytes(), hands.user_message("громкость 20", ctx)
    body = hands.build_body(message, 64)
    memory.remember("я пью кофе без сахара")
    memory.remember("меня зовут Дима")
    assert hands.prefix_bytes() == prefix
    assert hands.user_message("громкость 20", ctx) == message
    assert hands.build_body(hands.user_message("громкость 20", ctx), 64) == body
    assert "кофе".encode() not in body and "Дима".encode() not in body


# --- файл: битый, кэш, чужая запись, ошибки --------------------------------------------------------------


def test_broken_file_is_empty_and_kept_as_bad(_isolated_data: Path, caplog: pytest.LogCaptureFixture) -> None:
    data_file(_isolated_data).write_text("{не json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="jarvis"):
        assert memory.items() == []
    assert "битый" in caplog.text
    assert (_isolated_data / "memory.json.bad").read_text(encoding="utf-8") == "{не json"
    assert memory.remember("снова работаю")[1]
    assert texts() == ["снова работаю"]


@pytest.mark.parametrize("content", ['{"a": 1}', "42", '"строка"', "null"])
def test_not_a_list_is_broken(_isolated_data: Path, content: str) -> None:
    data_file(_isolated_data).write_text(content, encoding="utf-8")
    assert memory.items() == []


def test_bad_records_skipped_and_bom_ok(_isolated_data: Path) -> None:
    records = [
        {"id": "a1", "text": "хорошая", "ts": 1},
        {"id": "", "text": "без id", "ts": 1},
        {"id": "b2", "text": "  ", "ts": 1},
        {"id": "c3", "text": 5, "ts": 1},
        {"id": "d4", "text": "без времени"},
        "строка",
    ]
    data_file(_isolated_data).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8-sig")
    got = [(i.id, i.text, i.ts) for i in memory.items()]
    assert got == [("a1", "хорошая", 1.0), ("d4", "без времени", 0.0)]


def test_read_is_cached_by_mtime(_isolated_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    memory.remember("раз")
    reads: list[Path] = []
    real = Path.read_text

    def counting(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting)
    for _ in range(5):
        assert texts() == ["раз"]
        memory.as_context()
    assert reads == []  # своя запись — уже в кэше, файл не перечитывается
    # другой процесс (например, `jarvis ask`) переписал файл — перечитываем
    path = data_file(_isolated_data)
    record = [{"id": "x", "text": "из CLI", "ts": 5}]
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert texts() == ["из CLI"] and len(reads) == 1


def test_save_error_is_store_error_without_tmp(_isolated_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    memory.remember("раз")

    def deny(src: object, dst: object) -> None:
        raise PermissionError(13, "занято")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", deny)
        m.setattr(memory, "REPLACE_PAUSE_S", 0)
        with pytest.raises(StoreError, match="Не удалось сохранить"):
            memory.remember("два")
    assert sorted(p.name for p in _isolated_data.iterdir()) == ["memory.json"]
    assert texts() == ["раз"]  # кэш и файл — прежние


def test_replace_retries_while_file_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: os.replace падает, пока файл открыт на чтение другим процессом, — короткий повтор."""
    real = os.replace
    fails = [PermissionError(13, "занято")]

    def flaky(src: object, dst: object) -> None:
        if fails:
            raise fails.pop()
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(memory, "REPLACE_PAUSE_S", 0)
    assert memory.remember("раз")[1] and texts() == ["раз"]


def test_concurrent_writes_are_serialized() -> None:
    threads = [threading.Thread(target=memory.remember, args=(f"заметка {i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(texts()) == sorted(f"заметка {i}" for i in range(10))


def test_data_dir_change_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    memory.remember("в первом каталоге")
    other = tmp_path / "другой"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(other))
    assert memory.items() == []
    assert memory.path() == other / "memory.json"


def test_json_list_generic(_isolated_data: Path) -> None:
    store: JsonList[str] = JsonList("пример.json", lambda d: d.get("v"), lambda v: {"v": v})
    assert store.load() == [] and store.version() == 0
    store.save(["а", "б"])
    assert store.load() == ["а", "б"] and store.version() > 0
    saved = (_isolated_data / "пример.json").read_text(encoding="utf-8")
    assert json.loads(saved) == [{"v": "а"}, {"v": "б"}]
