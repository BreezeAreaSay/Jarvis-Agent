"""«Поиск по ПК»: разбор русских фраз, SQL индекса Windows, свой индекс папок, ранжирование, голос с выбором
по номеру, инструменты Jarvis AI и действия через API."""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.tools import ToolEnv
from jarvis.context import AppContext
from jarvis.platform.base import FileQuery, IndexedFile, PlatformError
from jarvis.platform.fake import FakePlatform
from jarvis.platform.windows import files as winfiles
from jarvis.plugins import file_search as fs
from jarvis.plugins.file_index import (
    OwnIndex,
    SearchQuery,
    clear_winner,
    date_range,
    junk_path,
    known_folder_key,
    parse_query,
    rank_files,
    scan_folders,
    search_stem,
    spoken_name,
    where_label,
)
from jarvis.plugins.file_search import FileSearch
from tests.conftest import EventLog
from tests.plugins.conftest import enable, manager, wait_until

NOW = datetime(2026, 10, 18, 15, 30)  # воскресенье
HOME = "C:\\Users\\me"
DOCS = HOME + "\\Documents"
DOWNLOADS = HOME + "\\Downloads"
KNOWN = [(DOWNLOADS, "downloads"), (DOCS, "documents"), (HOME + "\\Pictures", "pictures")]


def ts(*args: int) -> float:
    return datetime(*args).timestamp()  # type: ignore[arg-type]


def plugin_of(ctx: AppContext) -> FileSearch:
    plugin = manager(ctx).get("file_search")
    assert isinstance(plugin, FileSearch)
    return plugin


# --- разбор фраз ------------------------------------------------------------------------------------------


def parsed(text: str) -> dict[str, Any]:
    q = parse_query(text, NOW)
    return {
        "stems": q.stems,
        "kinds": q.kinds,
        "folder": q.folder,
        "path_words": q.path_words,
        "content": q.content,
        "sort": q.order,
        "limit": q.limit,
        "files_only": q.files_only,
        "date_from": datetime.fromtimestamp(q.date_from) if q.date_from is not None else None,
        "date_to": datetime.fromtimestamp(q.date_to) if q.date_to is not None else None,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("договор с Ивановым", {"stems": ["договор", "иванов"], "kinds": [], "sort": "relevance"}),
        ("презентация про бюджет", {"stems": ["бюджет"], "kinds": ["presentation"]}),
        (
            "последний скачанный файл",
            {"stems": [], "folder": "downloads", "sort": "recent", "limit": 1, "files_only": True},
        ),
        (
            "фото за вчера",
            {"kinds": ["image"], "date_from": datetime(2026, 10, 17), "date_to": datetime(2026, 10, 18)},
        ),
        ("pdf в загрузках", {"kinds": ["pdf"], "folder": "downloads", "stems": [], "sort": "recent"}),
        ("папку проекты", {"kinds": ["folder"], "stems": ["проект"], "files_only": False}),
        ("файлы со словом отчёт", {"content": ["отчет"], "stems": [], "files_only": True}),
        ("файлы в которых написано итоги квартала", {"content": ["итог", "квартал"], "stems": []}),
        (
            "таблицу бюджет на прошлой неделе",
            {
                "kinds": ["spreadsheet"],
                "stems": ["бюджет"],
                "date_from": datetime(2026, 10, 5),
                "date_to": datetime(2026, 10, 12),
            },
        ),
        (
            "документы за последние три дня",
            {"kinds": ["document"], "date_from": NOW - timedelta(days=3), "date_to": None},
        ),
        ("архив за 2 недели", {"kinds": ["archive"], "date_from": NOW - timedelta(days=14)}),
        ("видео с рабочего стола", {"kinds": ["video"], "folder": "desktop"}),
        ("отчёт в папке проекты", {"stems": ["отчет"], "path_words": ["проект"], "folder": ""}),
        ("фото в день рождения", {"kinds": ["image"], "stems": ["ден", "рожден"], "date_from": None}),
        ("документацию по api", {"kinds": [], "stems": ["документац", "api"]}),
        ("музыку в музыке", {"kinds": ["audio"], "folder": "music"}),
        ("песни из папки музыка", {"kinds": ["audio"], "folder": "music", "path_words": []}),
        (
            "мои документы в документах за этот месяц",
            {"kinds": ["document"], "folder": "documents", "date_from": datetime(2026, 10, 1)},
        ),
        ("презентации и таблицы про бюджет", {"kinds": ["presentation", "spreadsheet"], "stems": ["бюджет"]}),
        ("скриншоты за сегодня", {"kinds": ["image"], "date_from": datetime(2026, 10, 18), "date_to": None}),
        ("последнюю презентацию", {"kinds": ["presentation"], "sort": "recent", "limit": 1}),
        ("свежие фотки", {"kinds": ["image"], "sort": "recent", "limit": 0}),
        (
            "отчёт за прошлый месяц",
            {"stems": ["отчет"], "date_from": datetime(2026, 9, 1), "date_to": datetime(2026, 10, 1)},
        ),
        ("Отчёт_2024.DOCX", {"stems": ["отчет", "2024"], "kinds": ["document"]}),
        ("excel бюджет за год", {"kinds": ["spreadsheet"], "stems": ["бюджет"]}),
        ("договоры в загрузках за вчера", {"stems": ["договор"], "folder": "downloads"}),
        ("резюме анны петровой", {"stems": ["резюм", "анн", "петров"]}),
        ("инструкция к роутеру", {"stems": ["инструкц", "роутер"], "kinds": []}),
        ("что я скачал последним", {"folder": "downloads", "limit": 1, "stems": []}),
        ("позавчерашние скрины", {"kinds": ["image"], "date_from": datetime(2026, 10, 16)}),
        (
            "фото в прошлом году",
            {"kinds": ["image"], "date_from": datetime(2025, 1, 1), "date_to": datetime(2026, 1, 1)},
        ),
        ("файлы за последний час", {"files_only": True, "date_from": NOW - timedelta(hours=1)}),
    ],
)
def test_parse_query(text: str, expected: dict[str, Any]) -> None:
    got = parsed(text)
    assert {k: got[k] for k in expected} == expected


def test_parse_query_empty_and_text() -> None:
    assert parse_query("найди файл", NOW).empty
    assert parse_query("   ", NOW).empty
    assert not parse_query("pdf", NOW).empty
    assert parse_query("  договор   с  Ивановым ", NOW).text == "договор с Ивановым"


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("договора", "договор"),
        ("ивановым", "иванов"),
        ("презентацию", "презентац"),
        ("бюджет", "бюджет"),
        ("проектов", "проект"),
        ("отчётом", "отчет"),
        ("фото", "фот"),
        ("кафе", "каф"),
        ("акт", "акт"),
        ("Report", "report"),
        ("2024", "2024"),
    ],
)
def test_search_stem(word: str, stem: str) -> None:
    assert search_stem(word) == stem


def test_date_ranges_cross_year_and_week() -> None:
    january = datetime(2026, 1, 15, 10, 0)
    assert date_range("last_month", january) == (ts(2025, 12, 1), ts(2026, 1, 1))
    assert date_range("week", NOW) == (ts(2026, 10, 12), None)  # с понедельника
    assert date_range("last_week", NOW) == (ts(2026, 10, 5), ts(2026, 10, 12))
    assert date_range("year", NOW) == (ts(2026, 1, 1), None)
    assert date_range("nonsense", NOW) == (None, None)


def test_known_folder_key_and_spoken_name() -> None:
    assert known_folder_key("downloads") == "downloads"
    assert known_folder_key("Загрузки") == "downloads"
    assert known_folder_key("документах") == "documents"
    assert known_folder_key("Проекты") == ""
    assert spoken_name("Договор_с_Ивановым.final.docx") == "Договор с Ивановым final"
    assert spoken_name("Счёт №125 от 15.10.pdf") == "Счёт №125 от 15.10"
    assert spoken_name("Проекты 2025", is_dir=True) == "Проекты 2025"


# --- SQL индекса Windows ----------------------------------------------------------------------------------


def outside_literals(sql: str) -> str:
    """SQL без строковых литералов ('…', кавычка внутри удвоена) — всё, что видит разбор запроса."""
    out: list[str] = []
    i = 0
    while i < len(sql):
        if sql[i] != "'":
            out.append(sql[i])
            i += 1
            continue
        i += 1
        while i < len(sql):
            if sql[i] == "'" and sql[i + 1 : i + 2] == "'":
                i += 2
            elif sql[i] == "'":
                i += 1
                break
            else:
                i += 1
        out.append("?")
    return "".join(out)


def test_sql_escapes_user_text() -> None:
    query = FileQuery(
        words=["o'neil", "x' or 1=1 --", "50%_[x]"],
        extensions=["pdf", "x' OR '1'='1", ".DOCX"],
        scopes=["C:\\Users\\me\\Dow'n"],
        path_words=["a'b"],
        content=['"a" OR b* NEAR c'],
        limit=10_000,
    )
    sql = winfiles.build_sql(query)
    rest = outside_literals(sql)
    assert sql.count("'") % 2 == 0
    for leaked in ("neil", "1=1", "--", "OR '1'", "NEAR", "Dow"):
        assert leaked not in rest
    assert rest.startswith("SELECT TOP 300 ")  # предел строк — не больше MAX_ROWS
    assert "[oO]''[nN][eE][iI][lL]" in sql  # кавычка удвоена, регистр не важен
    assert "50[%][_][[][xX]]" in sql  # подстановочные знаки LIKE — буквально
    assert "LIKE '%.[pP][dD][fF]'" in sql and "LIKE '%.[dD][oO][cC][xX]'" in sql
    assert "1'='1" not in sql  # расширение не из белого списка выброшено
    assert "SCOPE='file:C:/Users/me/Dow''n'" in sql
    assert "CONTAINS(*, '\"aorbnearc*\"')" in sql  # в CONTAINS — только буквы и цифры
    assert "System.ItemType <> 'Directory'" in sql


def test_sql_shapes() -> None:
    sql = winfiles.build_sql(FileQuery(words=["отчет"], limit=5))
    assert sql == (
        "SELECT TOP 5 System.ItemPathDisplay, System.FileName, System.Size, System.DateModified, "
        "System.ItemType FROM SYSTEMINDEX WHERE SCOPE='file:' AND "
        "System.FileName LIKE '%[оО][тТ][чЧ][еёЕЁ][тТ]%' ORDER BY System.DateModified DESC"
    )
    any_word = winfiles.build_sql(FileQuery(words=["договор", "иванов"], any_word=True))
    assert "(System.ItemPathDisplay LIKE '%[дД]" in any_word
    assert " OR System.ItemPathDisplay LIKE " in any_word
    folders = winfiles.build_sql(FileQuery(folders_only=True, scopes=["C:\\A", "D:\\B\\"]))
    assert "(SCOPE='file:C:/A' OR SCOPE='file:D:/B')" in folders
    assert "(System.ItemType = 'Directory' OR System.Kind = 'folder')" in folders
    start = datetime(2026, 10, 17, tzinfo=UTC).timestamp()
    dated = winfiles.build_sql(FileQuery(date_from=start, date_to=0.0, path_words=["проект"]))
    assert "System.DateModified >= '2026/10/17 00:00:00'" in dated
    assert "System.DateModified < '1970/01/01 00:00:00'" in dated
    assert "System.ItemFolderPathDisplay LIKE '%[пП][рР][оО][еёЕЁ][кК][тТ]%'" in dated
    assert winfiles.contains_terms("отчет") == '"отчет*" OR "отчёт*"'
    assert winfiles.contains_terms("'\"*") is None


def test_rows_and_dates_from_ado() -> None:
    naive = datetime(2026, 10, 17, 21, 0)
    assert winfiles.to_epoch(naive) == datetime(2026, 10, 17, 21, 0, tzinfo=UTC).timestamp()
    aware = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
    assert winfiles.to_epoch(aware) == aware.timestamp()
    assert winfiles.to_epoch(None) == 0.0
    item = winfiles.parse_row(["C:\\A\\b.txt", "b.txt", 10, naive, ".txt"])
    assert item == IndexedFile("C:\\A\\b.txt", "b.txt", 10, winfiles.to_epoch(naive), False)
    folder = winfiles.parse_row(["C:\\A\\Проекты", None, None, naive, "Directory"])
    assert folder is not None and folder.is_dir and folder.size == 0 and folder.name == "Проекты"
    assert winfiles.parse_row([None, "x", 1, naive, ".x"]) is None


def test_explorer_select_command() -> None:
    command = winfiles.explorer_select_command("C:\\Папка\\файл, копия.txt")
    assert command.endswith(' /select,"C:\\Папка\\файл, копия.txt"')
    assert command.startswith('"') and "explorer.exe" in command
    with pytest.raises(PlatformError):
        winfiles.explorer_select_command('C:\\a"b')


def test_windows_platform_without_windows_reports_unavailable() -> None:
    if sys.platform == "win32":
        pytest.skip("проверка поведения без Windows")
    from jarvis.platform.windows import WindowsPlatform

    with pytest.raises(PlatformError, match="Индекс Windows"):
        WindowsPlatform().search_index(FileQuery(words=["отчет"]))
    with pytest.raises(PlatformError, match="Не найдено"):
        WindowsPlatform().reveal_path("/no/such/file.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="нужен настоящий Windows")
def test_live_windows_index() -> None:
    import pythoncom  # type: ignore[import-not-found]

    from jarvis.platform.windows import WindowsPlatform

    pythoncom.CoInitialize()
    try:
        found = WindowsPlatform().search_index(FileQuery(words=["a"], limit=3))
    except PlatformError:
        return  # служба индексирования выключена — тоже честный ответ
    assert len(found) <= 3 and all(isinstance(f, IndexedFile) and f.path for f in found)


# --- свой индекс -------------------------------------------------------------------------------------------


def make_tree(root: Path) -> Path:
    files = [
        "Договор с Ивановым.docx",
        "Отчёт 2024.pdf",
        "~$Отчёт 2024.docx",
        "фильм.mp4.crdownload",
        "desktop.ini",
        ".hidden/секрет.txt",
        "node_modules/pkg/index.js",
        ".git/config",
        "AppData/Local/кэш.txt",
        "Проекты/Сайт кафе/меню.pdf",
        "deep/l1/l2/l3/l4/глубоко.txt",
    ]
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return root


def test_scan_skips_service_hidden_and_temp(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "home")
    items, truncated = scan_folders([str(root)])
    names = {i.name for i in items}
    assert {"Договор с Ивановым.docx", "Отчёт 2024.pdf", "Проекты", "Сайт кафе", "меню.pdf"} <= names
    assert "глубоко.txt" in names
    assert not names & {"~$Отчёт 2024.docx", "фильм.mp4.crdownload", "desktop.ini", ".hidden", "секрет.txt"}
    assert not names & {"node_modules", "index.js", ".git", "config", "AppData", "кэш.txt"}
    assert truncated is False
    folder = next(i for i in items if i.name == "Проекты")
    assert folder.is_dir and folder.size == 0
    doc = next(i for i in items if i.name == "Договор с Ивановым.docx")
    assert doc.size == 1 and doc.modified > 0 and not doc.is_dir


def test_scan_depth_limit(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "home")
    names = {i.name for i in scan_folders([str(root)], max_depth=3)[0]}
    assert {"deep", "l1", "l2"} <= names  # третий уровень виден, но внутрь не заходим
    assert not names & {"l3", "глубоко.txt"}
    assert {i.name for i in scan_folders([str(root)], max_depth=1)[0]} >= {"deep", "Проекты"}
    assert "l1" not in {i.name for i in scan_folders([str(root)], max_depth=1)[0]}


def test_scan_limits_exclude_links_and_nested_roots(tmp_path: Path) -> None:
    root = make_tree(tmp_path / "home")
    items, truncated = scan_folders([str(root)], max_entries=3)
    assert len(items) == 3 and truncated is True
    names = {i.name for i in scan_folders([str(root)], exclude=[str(root / "Проекты")])[0]}
    assert "Проекты" not in names and "меню.pdf" not in names
    os.symlink(root / "Проекты", root / "ссылка")
    paths = [i.path for i in scan_folders([str(root), str(root / "Проекты"), str(tmp_path / "нет")])[0]]
    assert len(paths) == len(set(paths))  # вложенный корень не обходится второй раз
    assert not any("ссылка" in p for p in paths)
    stop = __import__("threading").Event()
    stop.set()
    assert scan_folders([str(root)], stop=stop) == ([], False)


def test_own_index_cache_roundtrip() -> None:
    items = [
        IndexedFile("C:\\A\\Отчёт.txt", "Отчёт.txt", 5, 100.0, False),
        IndexedFile("C:\\A\\Дом", "Дом", 0, 1.0, True),
    ]
    index = OwnIndex.make(items, ["C:\\A"], ["C:\\A\\tmp"], 42.0, True)
    again = OwnIndex.from_json(index.to_json())
    assert again is not None and again.items == items and again.roots == ["C:\\A"] and again.truncated
    assert OwnIndex.from_json({"version": 99}) is None
    broken = OwnIndex.from_json(
        {"version": 1, "items": [["C:\\A\\x.txt", {}, "?", 0], ["C:\\A\\y.txt", 1, 2, 0], 5]}
    )
    assert broken is not None and [i.name for i in broken.items] == ["y.txt"]
    assert [i.name for i in index.candidates(parse_query("отчет"))] == ["Отчёт.txt"]
    assert len(index.candidates(parse_query("папки"))) == 2  # без слов — всё, отбор по типу — в rank_files


# --- ранжирование ------------------------------------------------------------------------------------------


def item(path: str, days_ago: float = 10, size: int = 1, is_dir: bool = False) -> IndexedFile:
    name = path.rsplit("\\", 1)[-1]
    return IndexedFile(path, name, size, NOW.timestamp() - days_ago * 86400, is_dir)


def ranked(text: str, items: list[IndexedFile], **kw: Any) -> list[str]:
    hits = rank_files(items, parse_query(text, NOW), now=NOW.timestamp(), known=KNOWN, **kw)
    return [h.name for h in hits]


def test_rank_exact_prefix_coverage_recency() -> None:
    items = [
        item(DOCS + "\\Отчёт по бюджету.docx", 1),
        item(DOCS + "\\Бюджет 2026 черновик.xlsx", 2),
        item(DOCS + "\\Бюджет.xlsx", 300),
        item(DOCS + "\\Семейный бюджет.xlsx", 3),
        item(DOCS + "\\Заметки.txt", 0),
    ]
    assert ranked("бюджет", items) == [
        "Бюджет.xlsx",  # имя — ровно запрос, хоть и старое
        "Бюджет 2026 черновик.xlsx",  # с начала имени
        "Семейный бюджет.xlsx",
        "Отчёт по бюджету.docx",
    ]
    people = [
        item(DOCS + "\\Договор Петров.docx", 0),
        item(DOCS + "\\Иванов\\Акт.docx", 0),
        item(DOCS + "\\Договор Иванов.docx", 30),
    ]
    assert ranked("договор с ивановым", people) == ["Договор Иванов.docx", "Договор Петров.docx", "Акт.docx"]
    same = [item(DOCS + "\\План 1.docx", 20), item(DOCS + "\\План 2.docx", 1)]
    assert ranked("план", same) == ["План 2.docx", "План 1.docx"]  # при равном — свежее


def test_rank_filters() -> None:
    items = [
        item(DOWNLOADS + "\\Счёт.pdf", 1),
        item(DOWNLOADS + "\\Счёт.docx", 2),
        item(DOCS + "\\Счёт старый.pdf", 40),
        item(DOCS + "\\Счета", 5, is_dir=True),
        item(HOME + "\\AppData\\Local\\счёт.pdf", 0),
        item(DOCS + "\\Проекты\\Кафе\\Счёт кафе.pdf", 3),
    ]
    assert ranked("pdf счёт", items) == ["Счёт.pdf", "Счёт кафе.pdf", "Счёт старый.pdf"]  # AppData — мимо
    assert ranked("счёт в загрузках", items) == ["Счёт.pdf", "Счёт.docx"]
    assert ranked("папку счета", items) == ["Счета"]
    assert "Счета" not in ranked("файлы счёт", items)
    assert ranked("счёт за прошлый месяц", items) == ["Счёт старый.pdf"]
    assert ranked("счёт в папке проекты", items) == ["Счёт кафе.pdf"]
    assert ranked("pdf", items, exclude=[DOWNLOADS, DOCS + "\\Проекты"]) == ["Счёт старый.pdf"]
    assert ranked("свежие pdf", items)[0] == "Счёт.pdf"
    assert junk_path(HOME + "\\.git\\x.txt") and not junk_path(DOCS + "\\x.txt")


def test_clear_winner_rules() -> None:
    def hits(text: str, items: list[IndexedFile]) -> tuple[list[Any], SearchQuery]:
        q = parse_query(text, NOW)
        return rank_files(items, q, now=NOW.timestamp(), known=KNOWN), q

    one, q = hits("договор", [item(DOCS + "\\Договор.docx")])
    assert clear_winner(one, q)
    two = [item(DOCS + "\\Договор Иванов.docx"), item(DOCS + "\\Договор Петров.docx")]
    exact, q = hits("договор иванов", two)
    assert clear_winner(exact, q)
    similar, q = hits("бюджет", [item(DOCS + "\\Бюджет 2026.xlsx"), item(DOCS + "\\Бюджет 2025.xlsx")])
    assert not clear_winner(similar, q)
    photos, q = hits("фото", [item(DOCS + "\\a.jpg"), item(DOCS + "\\b.jpg")])
    assert not clear_winner(photos, q)
    latest, q = hits("последний pdf", [item(DOCS + "\\a.pdf", 1), item(DOCS + "\\b.pdf", 2)])
    assert clear_winner(latest, q) and latest[0].name == "a.pdf"
    assert not clear_winner([], q)


def test_where_label() -> None:
    assert where_label(DOCS, KNOWN) == "Документы"
    assert where_label(DOCS + "\\Проекты\\Jarvis", KNOWN) == "Документы › Проекты › Jarvis"
    assert where_label(DOCS + "\\a\\b\\c", KNOWN) == "Документы › … › b › c"
    assert where_label("D:\\Работа\\Клиенты\\2026", KNOWN) == "… › Клиенты › 2026"
    assert where_label("/home/me/x", []) == "… › me › x"


# --- плагин: голос ----------------------------------------------------------------------------------------


async def run_phrase(ctx: AppContext, template: str, query: str = "", source: str = "voice") -> Any:
    command = ctx.commands.by_phrase(template)  # type: ignore[attr-defined]
    slots: dict[str, Any] = {"_source": source}
    if query:
        slots["query"] = query
    return await command.handler(slots)


def opened(ctx: AppContext) -> list[str]:
    assert isinstance(ctx.platform, FakePlatform)
    return [args[0] for name, args in ctx.platform.calls if name == "open_path"]


async def test_enable_and_use_windows_index(pctx: AppContext) -> None:
    info = await enable(pctx, "file_search")
    assert info["name"] == "Поиск по ПК" and info["color"] == ["#93C5FD", "#2563EB"]
    assert {f["key"] for f in info["schema"]} == {"use_index", "include", "exclude", "max_results", "hotkey"}
    assert info["config"]["hotkey"] == "Ctrl+Alt+F" and info["config"]["use_index"] is True
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["index_available"] is True)
    assert plugin.status()["mode"] == "index"
    phrases = pctx.commands.phrases()  # type: ignore[attr-defined]
    for p in ("найди договор {query}", "покажи фото за {query}", "где лежит {query}"):
        assert p in phrases
    assert "открой последний скачанный файл" in phrases
    assert "второй" not in phrases  # номера — только пока висит выбор


async def test_voice_single_match_opens(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "file_search")
    result = await run_phrase(pctx, "найди договор {query}", "с ивановым")
    assert result.ok and result.say == "Открываю «Договор с Ивановым»."
    assert result.title == "Открыть «Договор с Ивановым.docx»" and result.message == "Документы"
    assert opened(pctx) == [DOCS + "\\Договор с Ивановым.docx"]
    event = pevents.of("plugin.file_search.opened")[-1]
    assert event == {"path": DOCS + "\\Договор с Ивановым.docx", "index": 0}
    folder = await run_phrase(pctx, "открой папку {query}", "проекты")
    assert folder.say == "Открываю папку «Проекты»." and opened(pctx)[-1] == DOCS + "\\Проекты"
    latest = await run_phrase(pctx, "открой последний скачанный файл")
    assert latest.ok and opened(pctx)[-1] == DOWNLOADS + "\\Счёт №125 от 15.10.pdf"


async def test_voice_several_results_card_and_numbered_followup(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "file_search")
    follow_ups: list[float | None] = []
    pctx.voice.follow_up = lambda seconds=None: follow_ups.append(seconds)  # type: ignore[attr-defined]
    pctx.tts.speaking = True  # type: ignore[misc]  # Jarvis уже говорит «какой открыть?»
    result = await run_phrase(pctx, "покажи фото за {query}", "вчера")
    assert result.ok and result.say == "Нашёл 3, сэр — какой открыть?" and result.data == {"silent": True}
    card = pevents.of("plugin.file_search.results")[-1]
    assert card["query"] == "фото за вчера" and card["total"] == 3 and card["ttl"] == fs.CHOICE_TTL_S
    names = [i["name"] for i in card["items"]]
    assert names == ["Скриншот 2026-10-17 214502.png", "IMG_2042.jpg", "IMG_2041.jpg"]
    assert set(card["items"][0]) >= {"path", "name", "kind", "size", "modified", "folder", "where"}
    assert card["items"][0]["kind"] == "image" and card["items"][0]["where"] == "Изображения › Screenshots"
    await wait_until(lambda: follow_ups == [fs.FOLLOW_UP_S])  # слушаем ответ без «Джарвис»
    assert opened(pctx) == []
    phrases = pctx.commands.phrases()  # type: ignore[attr-defined]
    assert {"второй", "открой вторую", "номер три", "последний", "покажи первый в папке"} <= phrases
    assert pctx.commands.refreshes >= 1  # type: ignore[attr-defined]  # обновлены сразу, без задержки
    second = await run_phrase(pctx, "второй")
    assert second.ok and second.say == "Открываю «IMG 2042»."
    assert opened(pctx) == [HOME + "\\Pictures\\IMG_2042.jpg"]
    assert pevents.of("plugin.file_search.opened")[-1]["index"] == 1
    third = await run_phrase(pctx, "номер три")
    assert opened(pctx)[-1] == HOME + "\\Pictures\\IMG_2041.jpg" and third.ok
    reveal = await run_phrase(pctx, "покажи первый в папке")
    assert reveal.ok and ("reveal_path", (card["items"][0]["path"],)) in pctx.platform.calls  # type: ignore[attr-defined]


async def test_choice_expires(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "CHOICE_TTL_S", 0.05)
    await enable(pctx, "file_search")
    await run_phrase(pctx, "найди pdf {query}", "в загрузках")
    assert "второй" in pctx.commands.phrases()  # type: ignore[attr-defined]
    await wait_until(lambda: "второй" not in pctx.commands.phrases())  # type: ignore[attr-defined]
    plugin = plugin_of(pctx)
    late = await plugin._pick(1, {})
    assert not late.ok and "закрыт" in late.message


async def test_voice_nothing_and_empty(pctx: AppContext) -> None:
    await enable(pctx, "file_search")
    nothing = await run_phrase(pctx, "найди файл {query}", "zxqw")
    assert not nothing.ok and nothing.message == "Ничего не нашлось"
    assert nothing.say == "Не нашёл «zxqw», сэр." and nothing.title == "Поиск: zxqw"
    pctx.settings.patch({"profile": {"address": ""}})
    nothing = await run_phrase(pctx, "найди файл {query}", "zxqw")
    assert nothing.say == "Не нашёл «zxqw»."
    empty = await run_phrase(pctx, "найди файл")
    assert not empty.ok and empty.say == "Что найти?"


async def test_voice_content_search(pctx: AppContext) -> None:
    await enable(pctx, "file_search")
    result = await run_phrase(pctx, "найди файлы {query}", "со словом отчёт", source="text")
    assert result.say == "Нашёл 3, сэр — какой открыть?" and result.title == "Поиск: файлы со словом отчёт"
    calls = pctx.platform.calls  # type: ignore[attr-defined]
    query = next(args[0] for name, args in calls if name == "search_index" and args[0].content)
    assert query.content == ["отчет"] and query.files_only and query.words == []


async def test_voice_asks_before_running_programs(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "file_search")
    plugin = plugin_of(pctx)
    outcome = await plugin.search(parse_query("jarvis setup"))
    hit = outcome.hits[0]
    assert hit.name == "jarvis-setup-2.0.exe"
    task = asyncio.ensure_future(plugin.open_hit(hit, 0, "voice"))
    await wait_until(lambda: pevents.of("confirm.request"))
    request = pevents.of("confirm.request")[-1]
    assert request["title"] == "Запустить «jarvis-setup-2.0.exe»?" and request["kind"] == "plugin"
    pctx.confirmations.resolve(request["id"], False)
    assert await task is False and opened(pctx) == []
    assert await plugin.open_hit(hit, 0, "ui") is True  # щелчок человека — без вопросов
    assert opened(pctx) == [hit.path]


async def test_exclude_folders_in_index_mode(pctx: AppContext) -> None:
    await enable(pctx, "file_search", {"exclude": [{"path": DOCS}]})
    result = await plugin_of(pctx).handle("search", {"text": "бюджет"})
    assert result["items"] == []
    result = await plugin_of(pctx).handle("search", {"text": "pdf"})
    assert result["items"] and all(not i["path"].startswith(DOCS) for i in result["items"])


# --- плагин: свой индекс -----------------------------------------------------------------------------------


async def test_fallback_to_own_index(pctx: AppContext, tmp_path: Path) -> None:
    assert isinstance(pctx.platform, FakePlatform)
    pctx.platform.index_available = False
    root = make_tree(tmp_path / "Мои файлы")
    await enable(pctx, "file_search", {"include": [{"path": str(root)}]})
    plugin = plugin_of(pctx)
    result = await plugin.handle("search", {"text": "договор иванов"})
    assert result["mode"] == "own" and [i["name"] for i in result["items"]] == ["Договор с Ивановым.docx"]
    status = plugin.status()
    assert status["mode"] == "own" and status["index_available"] is False and status["files"] >= 8
    assert "своему индексу" in status["hint"]
    content = await plugin.handle("search", {"text": "файлы со словом меню"})
    assert content["content_skipped"] is True and [i["name"] for i in content["items"]] == ["меню.pdf"]
    folder = await plugin.handle("search", {"text": "папку сайт кафе"})
    assert [i["name"] for i in folder["items"]] == ["Сайт кафе"] and folder["items"][0]["kind"] == "folder"
    voice = await run_phrase(pctx, "найди договор {query}", "с ивановым")
    assert voice.ok and opened(pctx) == [str(root / "Договор с Ивановым.docx")]
    reindex = await plugin.handle("reindex", {})
    assert reindex["mode"] == "own" and reindex["files"] == status["files"]


async def test_own_index_when_windows_index_disabled(pctx: AppContext, tmp_path: Path) -> None:
    root = make_tree(tmp_path / "home")
    await enable(pctx, "file_search", {"use_index": False, "include": [{"path": str(root)}]})
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["files"] > 0)
    assert plugin.status()["mode"] == "own"
    assert not [c for c in pctx.platform.calls if c[0] == "search_index"]  # type: ignore[attr-defined]
    result = await plugin.handle("search", {"text": "отчёт", "kinds": ["pdf"]})
    assert [i["name"] for i in result["items"]] == ["Отчёт 2024.pdf"]
    # кэш: после перезапуска плагина индекс сразу из файла, ещё до обхода папок
    count = plugin.status()["files"]
    await manager(pctx).update("file_search", enabled=False)
    assert plugin._own.items == []
    await plugin._load_cache()
    assert len(plugin._own.items) == count
    await manager(pctx).update("file_search", enabled=True)
    await wait_until(lambda: plugin_of(pctx).status()["files"] == count)


async def test_reindex_restarts_when_folders_change(
    pctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = make_tree(tmp_path / "первая"), make_tree(tmp_path / "вторая")
    real = fs.scan_folders

    def slow(roots: list[str], **kw: Any) -> tuple[list[IndexedFile], bool]:
        if str(first) in roots:
            kw["stop"].wait(5.0)  # обход старых папок долгий — его должны остановить
        return real(roots, **kw)

    monkeypatch.setattr(fs, "scan_folders", slow)
    await enable(pctx, "file_search", {"use_index": False, "include": [{"path": str(first)}]})
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.scanning and plugin.status()["scanning"])
    await manager(pctx).update("file_search", config={"include": [{"path": str(second)}]})
    index = await asyncio.wait_for(plugin.reindex(), 3.0)
    assert str(second) in index.roots and str(first) not in index.roots
    assert index.items and all(i.path.startswith(str(second)) for i in index.items)
    await wait_until(lambda: not plugin.status()["scanning"])


# --- плагин: действия, Jarvis AI, API ----------------------------------------------------------------------


async def test_actions_open_reveal_copy_toggle(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "file_search")
    plugin = plugin_of(pctx)
    found = await plugin.handle("search", {"text": "бюджет", "kinds": ["spreadsheet"], "date": "30d"})
    assert [i["name"] for i in found["items"]] == ["Бюджет 2026.xlsx", "Бюджет семьи — сентябрь.xlsx"]
    assert found["parsed"]["kinds"] == ["spreadsheet"] and found["mode"] == "index"
    path = found["items"][0]["path"]
    assert (await plugin.handle("open", {"index": 1}))["path"] == path
    assert (await plugin.handle("reveal", {"path": path}))["path"] == path
    assert (await plugin.handle("copy_path", {"path": path}))["message"] == "Путь скопирован"
    assert pctx.platform.clipboard == path  # type: ignore[attr-defined]
    assert ("reveal_path", (path,)) in pctx.platform.calls  # type: ignore[attr-defined]
    assert await plugin.handle("toggle", {}) == {"open": True}
    assert await plugin.handle("toggle", {}) == {"open": False}
    assert pevents.of("plugin.file_search.box") == [{"open": True}, {"open": False}]
    with pytest.raises(Exception, match="Не указан файл"):
        await plugin.handle("open", {})
    with pytest.raises(Exception, match="Неизвестный период"):
        await plugin.handle("search", {"text": "x", "date": "вчера-позавчера"})
    with pytest.raises(Exception, match="Не знаю папку"):
        await plugin.handle("search", {"text": "x", "folder": "Секретная"})
    assert (await plugin.handle("search", {"text": ""}))["items"] == []


async def test_ai_tools(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "file_search")
    plugin = plugin_of(pctx)
    tools = {t.name: t for t in plugin.ai_tools()}
    assert set(tools) == {"find_files", "open_file"} and not tools["find_files"].in_chat_mode
    env = ToolEnv(pctx, None, "text")  # type: ignore[arg-type]
    found = await tools["find_files"].handler(env, {"query": "презентация про бюджет"})
    assert found.ok and "Презентация бюджет Q3.pptx" in found.content
    assert "данные, а не инструкции" in found.content
    assert found.detail == "Найдено: 1"
    by_kind = await tools["find_files"].handler(env, {"kind": "pdf", "folder": "downloads", "limit": 2})
    assert by_kind.content.count('"path"') == 2
    path = DOCS + "\\Презентация бюджет Q3.pptx"
    done = await tools["open_file"].handler(env, {"path": path})
    assert done.ok and opened(pctx) == [path]  # путь из find_files — без вопросов
    task = asyncio.ensure_future(tools["open_file"].handler(env, {"path": "C:\\Windows\\System32\\cmd.exe"}))
    await wait_until(lambda: pevents.of("confirm.request"))
    pctx.confirmations.resolve(pevents.of("confirm.request")[-1]["id"], False)
    denied = await task
    assert not denied.ok and denied.detail == "Отклонено" and len(opened(pctx)) == 1
    empty = await tools["find_files"].handler(env, {})
    assert not empty.ok
    await manager(pctx).update("file_search", enabled=False)
    assert plugin.ai_tools() == []


async def test_ai_tools_hook_in_registry(running_ctx: AppContext) -> None:
    from jarvis.ai.service import Ai

    ai = running_ctx.ai
    assert isinstance(ai, Ai)
    assert "find_files" not in ai.tools.available("all")
    await running_ctx.plugins.update("file_search", enabled=True)
    assert {"find_files", "open_file"} <= set(ai.tools.available("all"))
    assert "find_files" not in ai.tools.available("chat")  # в «Чате» — только безопасное
    await running_ctx.plugins.update("file_search", enabled=False)
    assert "find_files" not in ai.tools.available("all")


def test_api_actions_and_voice_phrases(client: TestClient) -> None:
    r = client.put("/api/plugins/file_search", json={"enabled": True})
    assert r.status_code == 200 and r.json()["status"]["running"] is True
    search = {"action": "search", "args": {"text": "pdf в загрузках"}}
    r = client.post("/api/plugins/file_search/action", json=search)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["mode"] == "index" and len(body["items"]) == 3
    assert all(i["kind"] == "pdf" and i["where"] == "Загрузки" for i in body["items"])
    path = body["items"][0]["path"]
    r = client.post("/api/plugins/file_search/action", json={"action": "open", "args": {"path": path}})
    assert r.status_code == 200 and ("open_path", (path,)) in client.ctx.platform.calls  # type: ignore[attr-defined]
    r = client.post("/api/plugins/file_search/action", json={"action": "open", "args": {}})
    assert r.status_code == 400 and r.json()["detail"] == "Не указан файл"

    commands = client.ctx.commands  # type: ignore[attr-defined]
    expect = {
        "найди договор с ивановым": "plugin.file_search:find:договор",
        "где презентация про бюджет": "plugin.file_search:find:презентация",
        "покажи фото за вчера": "plugin.file_search:media_date:фото",
        "найди pdf в загрузках": "plugin.file_search:find:pdf",
        "открой папку проекты": "plugin.file_search:find:папка",
        "найди файлы со словом отчёт": "plugin.file_search:find:файл",
        "открой последний скачанный файл": "plugin.file_search:last_download",
        "найди видео в загрузках": "plugin.file_search:media:видео",
    }
    for text, command_id in expect.items():
        match = commands.match(text)
        assert match is not None and match.parts[0].command_id == command_id, text
    # чужие фразы плагин не перехватывает
    for text in ("найди котиков", "найди видео с котиками", "открой папку загрузки", "открой файл"):
        match = commands.match(text)
        assert match is None or not match.parts[0].command_id.startswith("plugin.file_search"), text

    r = client.post("/api/dispatch", json={"text": "найди договор с Ивановым", "source": "text"})
    assert r.status_code == 200 and r.json()["kind"] == "command" and r.json()["result"]["ok"]
    assert ("open_path", (DOCS + "\\Договор с Ивановым.docx",)) in client.ctx.platform.calls  # type: ignore[attr-defined]
