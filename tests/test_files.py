"""pc.files: поиск через es.exe, известные папки, open_target (единственный os.startfile), корзина, чтение."""

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from pc import apps, confirm_client, files, paths, privacy, settings, subproc
from pc.result import Caller

SRC = Path(__file__).resolve().parents[1] / "src"
ES = r"C:\Jarvis\bin\es.exe"
ME = r"C:\Users\me"
EXEC_EXT = {
    *(".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi"),
    *(".msp", ".scr", ".pif", ".cpl", ".jar", ".reg", ".lnk", ".url", ".appref-ms", ".settingcontent-ms"),
    *(".library-ms", ".search-ms"),
}


# --- фейки ---------------------------------------------------------------------------------------


def fake_check(path: str, caller: Caller, op: str, is_dir: bool | None = None) -> paths.PathCheck:
    """Упрощённые правила pc.paths: системные зоны — всем; AppData, data_dir и ~\\.* — мозгу."""
    p = PureWindowsPath(path)
    low = str(p).casefold()
    if not re.match(r"^[a-z]:\\", low):
        return paths.PathCheck(False, path, "Нужен полный путь")
    if low.startswith((r"c:\windows", r"c:\program files", r"c:\programdata")):
        return paths.PathCheck(False, str(p), "Системная папка — нельзя")
    if caller == "brain":
        data = str(settings.data_dir()).casefold()
        if (
            "\\appdata\\" in low
            or low.startswith(r"c:\jarvisdata")
            or low.startswith(data)
            or "\\.ssh" in low
        ):
            return paths.PathCheck(False, str(p), "Закрытая зона — нельзя")
    confirm = op == "open" and not is_dir and (p.suffix.casefold() in EXEC_EXT or not p.suffix)
    return paths.PathCheck(True, str(p), "", confirm)


def fake_is_executable(path: str) -> bool:
    p = PureWindowsPath(path)
    return p.suffix.casefold() in EXEC_EXT or not p.suffix


def fake_redact_text(text: str) -> str:
    return text.replace("секрет", "(скрыто)")


def fake_redact(value: Any) -> Any:
    if isinstance(value, str):
        return fake_redact_text(value)
    if isinstance(value, list):
        return [fake_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: fake_redact(v) for k, v in value.items()}
    return value


def fake_redact_path(path: str) -> str | None:
    return None if "\\appdata\\" in path.casefold() else path


class FakeFilesApi:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.dirs: set[str] = set()
        self.existing: set[str] = set()
        self.trashed: list[str] = []
        self.foreground_allowed = 0
        self.start_error: Exception | None = None
        self.folders = {
            "{374DE290-123F-4565-9164-39C4925E467B}": ME + r"\Downloads",
            "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}": ME + r"\Documents",
            "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}": ME + r"\Desktop",
            "{33E28130-4E1E-4676-835A-98395C3BC3BB}": ME + r"\Pictures",
            "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}": ME + r"\Videos",
            "{4BD8D571-6D19-48D3-BE97-422220080E43}": ME + r"\Music",
        }
        self.dirs |= {p.casefold() for p in self.folders.values()}

    def startfile(self, target: str) -> None:
        if self.start_error:
            raise self.start_error
        self.started.append(target)

    def allow_set_foreground(self) -> None:
        self.foreground_allowed += 1

    def is_dir(self, path: str) -> bool:
        return path.casefold() in self.dirs

    def exists(self, path: str) -> bool:
        return path.casefold() in self.existing or self.is_dir(path)

    def known_folder(self, folder_id: str) -> str:
        if folder_id not in self.folders:
            raise OSError("нет такой папки")
        return self.folders[folder_id]

    def send_to_trash(self, path: str) -> None:
        self.trashed.append(path)


class Confirms:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, summary: str, details: str, caller: Caller) -> bool:
        self.asked.append((summary, details, caller))
        return self.answer


@pytest.fixture
def fs(monkeypatch: pytest.MonkeyPatch, apps_fixture: list[dict[str, str]]) -> FakeFilesApi:
    fake = FakeFilesApi()
    monkeypatch.setattr(files, "_api", fake)
    monkeypatch.setattr(paths, "check", fake_check)
    monkeypatch.setattr(paths, "is_executable_type", fake_is_executable)
    monkeypatch.setattr(privacy, "redact_text", fake_redact_text)
    monkeypatch.setattr(privacy, "redact", fake_redact)
    monkeypatch.setattr(privacy, "redact_path", fake_redact_path)
    inventory = {a["name"].casefold(): apps.App(a["name"], a["app_id"]) for a in apps_fixture}

    def resolve(name: str) -> tuple[apps.App | None, float]:
        app = inventory.get(name.casefold())
        return (app, 100.0) if app else (None, 30.0)

    sta_calls: list[tuple[Any, tuple[Any, ...]]] = []

    def run_sta(fn: Any, *args: Any) -> Any:
        sta_calls.append((fn, args))
        return fn(*args)

    monkeypatch.setattr(apps, "resolve", resolve)
    monkeypatch.setattr(apps, "run_sta", run_sta)
    fake.sta_calls = sta_calls  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def confirms() -> Confirms:
    c = Confirms(True)
    confirm_client.set_confirm_handler(c)
    return c


# --- единственный os.startfile -------------------------------------------------------------------


def test_startfile_called_only_once_in_files() -> None:
    calls: list[str] = []
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"startfile\(", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line = text[line_start : text.find("\n", m.start())]
            if re.match(r"\s*def startfile\(", line):
                continue
            calls.append(f"{py.relative_to(SRC)}: {line.strip()}")
    assert len(calls) == 1, calls
    assert calls[0].startswith("pc/files.py: os.startfile(") or calls[0].startswith(
        "pc\\files.py: os.startfile("
    )


# --- open_target: отказы -------------------------------------------------------------------------

DENIED_TARGETS = [
    "cmd",
    "cmd.exe",
    "cmd /c calc",
    "cmd.exe /c del C:\\Users\\me\\x",
    "powershell",
    "powershell -Command Get-Process",
    "pwsh -c whoami",
    "mshta vbscript:Execute(1)",
    "rundll32 shell32.dll,Control_RunDLL",
    "calc.exe",
    "notepad && calc",
    "Telegram | calc",
    "%COMSPEC%",
    "shell:startup",
    "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
    "file:///C:/Users/me/x.txt",
    "file://server/share/x",
    "search-ms:query=отчёт",
    "ms-msdt:/id PCWDiagnostic",
    "ms-settings:display",
    "javascript:alert(1)",
    "vbscript:msgbox(1)",
    "ftp://example.com/x",
    "mailto:me@example.com",
]


@pytest.mark.parametrize("target", DENIED_TARGETS)
@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_target_denies_commands_and_schemes(fs: FakeFilesApi, target: str, caller: Caller) -> None:
    r = files.open_target(target, None, caller)
    assert not r.ok
    assert fs.started == []


@pytest.mark.parametrize("kind", ["app", "file", "folder", "url"])
@pytest.mark.parametrize(
    "target", ["shell:startup", "ms-settings:display", "file:///C:/x.txt", "search-ms:x"]
)
def test_schemes_denied_for_any_kind(fs: FakeFilesApi, kind: str, target: str) -> None:
    r = files.open_target(target, kind, "user")  # type: ignore[arg-type]
    assert not r.ok
    assert fs.started == []


@pytest.mark.parametrize(
    "target", ["отчёт.docx", r"..\отчёт.docx", "\\\\server\\share\\x.docx", "Documents\\a.txt"]
)
def test_open_file_requires_full_path(fs: FakeFilesApi, target: str) -> None:
    r = files.open_target(target, "file", "user")
    assert not r.ok
    assert fs.started == []


def test_open_target_control_chars_denied(fs: FakeFilesApi) -> None:
    assert not files.open_target("Telegram\x00calc", None, "user").ok
    assert not files.open_target("https://example.com/\x07", "url", "user").ok
    assert not files.open_target("Telegram\u202e", None, "user").ok
    assert fs.started == []


def test_open_target_empty(fs: FakeFilesApi) -> None:
    assert not files.open_target("  ", None, "user").ok


# --- open_target: приложения ---------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_app_uses_apps_folder_in_sta(fs: FakeFilesApi, caller: Caller) -> None:
    r = files.open_target("Telegram", None, caller)
    assert r.ok
    assert r.text == "Открыл Telegram"
    assert fs.started == ["shell:AppsFolder\\TelegramDesktop.TelegramDesktop"]
    assert fs.foreground_allowed == 1
    fn, args = fs.sta_calls[0]  # type: ignore[attr-defined]
    assert fn == fs.startfile
    assert args == ("shell:AppsFolder\\TelegramDesktop.TelegramDesktop",)


def test_open_app_with_explicit_kind(fs: FakeFilesApi) -> None:
    r = files.open_target("Блокнот", "app", "user")
    assert r.ok
    assert fs.started == ["shell:AppsFolder\\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"]


def test_open_app_not_found(fs: FakeFilesApi) -> None:
    r = files.open_target("Фотошоп", None, "user")
    assert not r.ok
    assert r.text == "Не нашёл приложение «Фотошоп»"
    assert fs.started == []


def test_open_app_launch_error(fs: FakeFilesApi) -> None:
    fs.start_error = OSError("ShellExecute failed")
    r = files.open_target("Telegram", "app", "user")
    assert not r.ok
    assert r.text == "Не удалось открыть Telegram"


def test_app_names_with_punctuation_are_not_commands(fs: FakeFilesApi) -> None:
    for name in ("AMD Software: Adrenalin Edition", "7-Zip File Manager", "Outlook (classic)", "Notepad++"):
        assert files.open_target(name, None, "user").ok, name
    assert len(fs.started) == 4


# --- open_target: файлы и папки ------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_regular_file_without_confirm(fs: FakeFilesApi, confirms: Confirms, caller: Caller) -> None:
    path = ME + r"\Documents\отчёт.docx"
    fs.existing.add(path.casefold())
    r = files.open_target(path, None, caller)
    assert r.ok
    assert r.text == "Открыл «отчёт.docx»"
    assert fs.started == [path]
    assert confirms.asked == []


@pytest.mark.parametrize(
    "name",
    ["setup.exe", "run.bat", "script.ps1", "Telegram.lnk", "сайт.url", "README", "x.settingcontent-ms"],
)
@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_executable_asks(fs: FakeFilesApi, confirms: Confirms, name: str, caller: Caller) -> None:
    path = ME + "\\Downloads\\" + name
    fs.existing.add(path.casefold())
    r = files.open_target(path, None, caller)
    assert r.ok
    assert confirms.asked == [(f"Открыть исполняемый файл «{name}»?", path, caller)]
    assert fs.started == [path]


def test_open_executable_declined(fs: FakeFilesApi, confirms: Confirms) -> None:
    confirms.answer = False
    path = ME + r"\Downloads\setup.exe"
    fs.existing.add(path.casefold())
    r = files.open_target(path, "file", "user")
    assert not r.ok
    assert fs.started == []


def test_open_executable_without_handler_denied(fs: FakeFilesApi) -> None:
    path = ME + r"\Downloads\setup.exe"
    fs.existing.add(path.casefold())
    assert not files.open_target(path, "file", "brain").ok
    assert fs.started == []


def test_open_missing_file(fs: FakeFilesApi) -> None:
    r = files.open_target(ME + r"\Documents\нет.docx", "file", "user")
    assert not r.ok
    assert "Не нашёл файл" in r.text


@pytest.mark.parametrize(
    "path",
    [
        ME + r"\AppData\Roaming\Telegram Desktop\tdata\key_datas",
        ME + r"\AppData\Local\Google\Chrome\User Data\Default\Cookies",
        ME + r"\.ssh\id_ed25519",
        r"C:\JarvisData\pipe.key",
    ],
)
def test_brain_closed_zones(fs: FakeFilesApi, confirms: Confirms, path: str) -> None:
    fs.existing.add(path.casefold())
    fs.dirs.add(str(PureWindowsPath(path).parent).casefold())
    assert not files.open_target(path, None, "brain").ok
    assert not files.open_target(str(PureWindowsPath(path).parent), "folder", "brain").ok
    assert fs.started == []
    assert confirms.asked == []


def test_brain_data_dir_closed(fs: FakeFilesApi, confirms: Confirms, monkeypatch: pytest.MonkeyPatch) -> None:
    data = r"D:\Где-то\JarvisData"
    monkeypatch.setenv("JARVIS_DATA_DIR", data)  # каталог данных закрыт мозгу, где бы он ни лежал
    home = data + r"\codex-home"
    fs.dirs.add(home.casefold())
    assert not files.open_target(home, "folder", "brain").ok
    assert fs.started == []
    assert files.open_target(home, "folder", "user").ok


def test_system_zone_denied_for_user(fs: FakeFilesApi) -> None:
    path = r"C:\Windows\System32\cmd.exe"
    fs.existing.add(path.casefold())
    assert not files.open_target(path, None, "user").ok
    assert not files.open_target(path, "file", "user").ok
    assert fs.started == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("загрузки", r"\Downloads"),
        ("папку загрузок", r"\Downloads"),
        ("Скачанное", r"\Downloads"),
        ("downloads", r"\Downloads"),
        ("документы", r"\Documents"),
        ("рабочий стол", r"\Desktop"),
        ("рабочем столе", r"\Desktop"),
        ("картинки", r"\Pictures"),
        ("изображения", r"\Pictures"),
        ("видео", r"\Videos"),
        ("музыку", r"\Music"),
        ("«Музыка»", r"\Music"),
    ],
)
def test_known_folder(fs: FakeFilesApi, name: str, expected: str) -> None:
    assert files.known_folder(name) == ME + expected


def test_known_folder_unknown_or_error(fs: FakeFilesApi) -> None:
    assert files.known_folder("отчёт") is None
    assert files.known_folder("документ отчёт") is None
    fs.folders.clear()
    assert files.known_folder("загрузки") is None


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_known_folder(fs: FakeFilesApi, caller: Caller) -> None:
    r = files.open_target("загрузки", None, caller)
    assert r.ok
    assert r.text == "Открыл папку «Downloads»"
    assert fs.started == [ME + r"\Downloads"]


def test_open_folder_by_path(fs: FakeFilesApi, confirms: Confirms) -> None:
    folder = ME + r"\Projects\site.exe"  # папка с «исполняемым» именем — не спрашиваем
    fs.dirs.add(folder.casefold())
    r = files.open_target(folder, None, "user")
    assert r.ok
    assert fs.started == [folder]
    assert confirms.asked == []


def test_open_drive_root(fs: FakeFilesApi) -> None:
    fs.dirs.add("d:\\")
    assert files.open_target("D:", None, "user").ok
    assert fs.started == ["D:\\"]


def test_app_kind_with_path_is_a_file(fs: FakeFilesApi, confirms: Confirms) -> None:
    path = ME + r"\Downloads\tool.exe"
    fs.existing.add(path.casefold())
    assert files.open_target(path, "app", "user").ok
    assert confirms.asked == [("Открыть исполняемый файл «tool.exe»?", path, "user")]
    assert fs.started == [path]


def test_open_folder_unknown_name(fs: FakeFilesApi) -> None:
    r = files.open_target("мои проекты", "folder", "user")
    assert not r.ok
    assert fs.started == []


# --- open_target: ссылки -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("https://example.com/path?q=1#x", "https://example.com/path?q=1#x"),
        ("http://example.com", "http://example.com"),
        ("HTTPS://example.com/a", "https://example.com/a"),
        ("https://пример.рф/страница", "https://пример.рф/страница"),
    ],
)
def test_open_url_user(fs: FakeFilesApi, confirms: Confirms, target: str, expected: str) -> None:
    r = files.open_target(target, None, "user")
    assert r.ok, r.text
    assert fs.started == [expected]
    assert confirms.asked == []


def test_open_url_without_scheme_when_kind_url(fs: FakeFilesApi) -> None:
    assert files.open_target("example.com/docs", "url", "user").ok
    assert fs.started == ["https://example.com/docs"]


def test_open_url_brain_asks_with_full_url(fs: FakeFilesApi, confirms: Confirms) -> None:
    url = "https://example.com/very/long/path?token=abc&x=1"
    r = files.open_target(url, "url", "brain")
    assert r.ok
    summary, details, caller = confirms.asked[0]
    assert url in summary
    assert details == url
    assert caller == "brain"


def test_open_url_brain_declined(fs: FakeFilesApi, confirms: Confirms) -> None:
    confirms.answer = False
    assert not files.open_target("https://example.com", None, "brain").ok
    assert fs.started == []


@pytest.mark.parametrize(
    "target",
    [
        "http://user:pass@example.com/",
        "https://example.com@evil.example/",
        "https://exa mple.com/",
        "https:///nohost",
        "http://",
        "https:example.com",
        "https://example.com\\@evil.example",
        "https://example.com:99999/",
        "example",
    ],
)
def test_open_url_rejected(fs: FakeFilesApi, confirms: Confirms, target: str) -> None:
    assert not files.open_target(target, "url", "user").ok
    assert fs.started == []


def test_normalize_url() -> None:
    assert files.normalize_url("https://Example.com/a b") is None
    assert files.normalize_url("https://example.com/") == "https://example.com/"
    assert files.normalize_url("javascript:alert(1)") is None


# --- find ----------------------------------------------------------------------------------------


class FakeEs:
    def __init__(self, *replies: tuple[int, bytes]) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], timeout: float, cwd: Any = None, env: Any = None
    ) -> subproc.Completed:
        self.calls.append([str(a) for a in argv])
        assert timeout == 5.0
        code, out = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return subproc.Completed(code, out, b"")


def es_csv(*rows: tuple[str, str]) -> bytes:
    lines = "".join(f'"{p}","{d}"\r\n' for p, d in rows)
    return "\ufeff".encode() + lines.encode("utf-8")


@pytest.fixture
def es_conf(config_file: Any) -> None:
    config_file(f"[pc]\nes_path = '{ES}'\n")


def expected_argv(query: str, attr: list[str], limit: int = 20) -> list[str]:
    head = [ES, "-argv", "-cp", "65001", "-timeout", "3000", "-n", str(limit)]
    tail = ["-dm", "-date-format", "1", "-csv", "-no-header", "-no-folder-append-path-separator"]
    return [*head, "-sort", "date-modified-descending", *attr, *tail, "-search", query]


@pytest.mark.parametrize(("kind", "attr"), [("any", []), ("file", ["/a-d"]), ("folder", ["/ad"])])
def test_find_argv_exact(
    fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch, kind: str, attr: list[str]
) -> None:
    fake = FakeEs((0, b""))
    monkeypatch.setattr(subproc, "run", fake)
    files.find("-отчёт", kind, "user")  # type: ignore[arg-type]
    assert fake.calls == [expected_argv("-отчёт", attr)]


def test_find_limit(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeEs((0, b""))
    monkeypatch.setattr(subproc, "run", fake)
    files.find("отчёт", "any", "user", limit=5)
    assert fake.calls[0] == expected_argv("отчёт", [], 5)


def test_find_cyrillic_output(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = es_csv(
        (ME + r"\Documents\Отчёт за ёлку.docx", "2026-10-01T12:00:00"),
        (ME + r"\Desktop\отчёт, черновик.txt", "2026-09-30T08:00:00"),
    )
    monkeypatch.setattr(subproc, "run", FakeEs((0, out)))
    r = files.find("отчёт", "file", "user")
    assert r.ok
    assert r.data == [ME + r"\Documents\Отчёт за ёлку.docx", ME + r"\Desktop\отчёт, черновик.txt"]
    assert r.text == "Нашёл 2: Отчёт за ёлку.docx, отчёт, черновик.txt"


def test_find_date_column_first(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = '"2026-10-01T12:00:00","C:\\Users\\me\\Музыка\\трек.mp3"\r\n'.encode()
    monkeypatch.setattr(subproc, "run", FakeEs((0, out)))
    assert files.find("трек", "any", "user").data == [ME + r"\Музыка\трек.mp3"]


def test_find_retries_code_7_once(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeEs((7, b""), (0, es_csv((ME + r"\a.txt", "2026-10-01T12:00:00"))))
    monkeypatch.setattr(subproc, "run", fake)
    r = files.find("a", "any", "user")
    assert r.ok
    assert len(fake.calls) == 2
    fake = FakeEs((7, b""))
    monkeypatch.setattr(subproc, "run", fake)
    r = files.find("a", "any", "user")
    assert not r.ok
    assert len(fake.calls) == 2


def test_find_everything_not_running(
    fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subproc, "run", FakeEs((8, b"")))
    r = files.find("a", "any", "user")
    assert not r.ok
    assert r.text == "Everything не запущен — запусти Everything"


@pytest.mark.parametrize("code", [4, 6])
def test_find_argument_error_is_logged_not_parsed(
    fs: FakeFilesApi,
    es_conf: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: int,
) -> None:
    helptext = b"ES 1.1.0.38\r\nC:\\Users\\me\\help.txt\r\n-search <text>\r\n"
    monkeypatch.setattr(subproc, "run", FakeEs((code, helptext)))
    with caplog.at_level("ERROR", logger="jarvis"):
        r = files.find("a", "any", "user")
    assert not r.ok
    assert r.data is None
    assert any("es.exe" in rec.getMessage() for rec in caplog.records)


def test_find_es_missing(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> subproc.Completed:
        raise FileNotFoundError(ES)

    monkeypatch.setattr(subproc, "run", missing)
    r = files.find("a", "any", "user")
    assert not r.ok
    assert ES in r.text


def test_find_timeout(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> subproc.Completed:
        raise subprocess.TimeoutExpired("es.exe", 5)

    monkeypatch.setattr(subproc, "run", slow)
    assert not files.find("a", "any", "user").ok


@pytest.mark.parametrize(
    "query",
    [
        "content:пароль",
        "utf8content:token",
        "regex:^a.*$",
        "отчёт CONTENT : пароль",
        "ext:txt  regex :x",
        "ansicontent:x",
    ],
)
def test_find_brain_content_functions_denied(
    fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    fake = FakeEs((0, b""))
    monkeypatch.setattr(subproc, "run", fake)
    assert not files.find(query, "any", "brain").ok
    assert fake.calls == []
    assert files.find(query, "any", "user").ok
    assert len(fake.calls) == 1


def test_find_filters_paths(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    out = es_csv(
        (ME + r"\Documents\отчёт.docx", "2026-10-01T12:00:00"),
        (r"C:\Windows\System32\отчёт.dll", "2026-10-01T12:00:00"),
        (ME + r"\AppData\Roaming\Telegram Desktop\отчёт.txt", "2026-10-01T12:00:00"),
    )
    monkeypatch.setattr(subproc, "run", FakeEs((0, out)))
    user = files.find("отчёт", "any", "user")
    assert user.data == [ME + r"\Documents\отчёт.docx", ME + r"\AppData\Roaming\Telegram Desktop\отчёт.txt"]
    brain = files.find("отчёт", "any", "brain")
    assert brain.data == [ME + r"\Documents\отчёт.docx"]
    assert brain.text == "Нашёл 1: отчёт.docx"


def test_find_nothing(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subproc, "run", FakeEs((0, b"")))
    r = files.find("нет такого", "any", "user")
    assert r.ok
    assert r.data == []
    assert r.text == "Ничего не нашёл по «нет такого»"


def test_find_empty_query(fs: FakeFilesApi, es_conf: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeEs((0, b""))
    monkeypatch.setattr(subproc, "run", fake)
    assert not files.find("  \n ", "any", "user").ok
    assert fake.calls == []


# --- корзина -------------------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_trash_asks_and_moves(fs: FakeFilesApi, confirms: Confirms, caller: Caller) -> None:
    path = ME + r"\Documents\отчёт.docx"
    fs.existing.add(path.casefold())
    r = files.trash(path, caller)
    assert r.ok
    assert confirms.asked == [("Переместить в корзину «отчёт.docx»?", path, caller)]
    assert fs.trashed == [path]


def test_trash_declined(fs: FakeFilesApi, confirms: Confirms) -> None:
    confirms.answer = False
    path = ME + r"\Documents\отчёт.docx"
    fs.existing.add(path.casefold())
    assert not files.trash(path, "user").ok
    assert fs.trashed == []


def test_trash_without_handler_denied(fs: FakeFilesApi) -> None:
    path = ME + r"\Documents\отчёт.docx"
    fs.existing.add(path.casefold())
    assert not files.trash(path, "user").ok
    assert fs.trashed == []


def test_trash_rules(fs: FakeFilesApi, confirms: Confirms) -> None:
    secret = ME + r"\AppData\Roaming\Telegram Desktop\tdata"
    fs.existing.add(secret.casefold())
    assert not files.trash(secret, "brain").ok
    assert not files.trash(r"C:\Windows\notepad.exe", "user").ok
    assert not files.trash("отчёт.docx", "user").ok
    assert not files.trash(ME + r"\нет.txt", "user").ok
    assert fs.trashed == []
    assert confirms.asked == []


def test_trash_error(fs: FakeFilesApi, confirms: Confirms, monkeypatch: pytest.MonkeyPatch) -> None:
    path = ME + r"\Documents\отчёт.docx"
    fs.existing.add(path.casefold())

    def broken(p: str) -> None:
        raise OSError("занято")

    monkeypatch.setattr(fs, "send_to_trash", broken)
    r = files.trash(path, "user")
    assert not r.ok
    assert "корзину" in r.text


# --- чтение текста -------------------------------------------------------------------------------


@pytest.fixture
def real_file(fs: FakeFilesApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Путь в стиле Windows, за которым стоит настоящий файл во временной папке."""
    target = tmp_path / "файл.txt"

    def check(path: str, caller: Caller, op: str, is_dir: bool | None = None) -> paths.PathCheck:
        r = fake_check(path, caller, op, is_dir)
        return paths.PathCheck(r.ok, str(target) if r.ok else r.path, r.reason, r.confirm)

    monkeypatch.setattr(paths, "check", check)

    def write(data: bytes) -> Path:
        target.write_bytes(data)
        return target

    return write


WIN_PATH = ME + r"\Documents\файл.txt"


def test_read_text_user_denied(real_file: Any, confirms: Confirms) -> None:
    real_file("привет".encode())
    r = files.read_text(WIN_PATH, "user")
    assert not r.ok
    assert confirms.asked == []


def test_read_text_brain_asks(real_file: Any, confirms: Confirms) -> None:
    real_file("привет, ёж".encode())
    r = files.read_text(WIN_PATH, "brain")
    assert r.ok
    assert r.data["text"] == "привет, ёж"
    assert r.data["truncated"] is False
    assert confirms.asked[0][0] == "GPT просит прочитать файл «файл.txt» (уйдёт в облако)"


def test_read_text_declined(real_file: Any, confirms: Confirms) -> None:
    confirms.answer = False
    real_file(b"x")
    assert not files.read_text(WIN_PATH, "brain").ok


@pytest.mark.parametrize(
    "data",
    [
        "\ufeffпривет".encode(),
        "привет".encode("utf-16"),
        b"\xfe\xff" + "привет".encode("utf-16-be"),
        "привет".encode("cp1251"),
    ],
)
def test_read_text_encodings(real_file: Any, confirms: Confirms, data: bytes) -> None:
    real_file(data)
    assert files.read_text(WIN_PATH, "brain").data["text"] == "привет"


def test_read_text_truncated_utf8_boundary(real_file: Any, confirms: Confirms) -> None:
    real_file(("ё" * 100).encode())  # 200 байт
    r = files.read_text(WIN_PATH, "brain", max_bytes=51)
    assert r.ok
    assert r.data["truncated"] is True
    assert r.data["text"] == "ё" * 25 + "\n(обрезано)"
    assert "(обрезано)" in r.text


def test_read_text_max_64k(real_file: Any, confirms: Confirms) -> None:
    real_file(b"a" * 70000)
    r = files.read_text(WIN_PATH, "brain", max_bytes=10**6)
    assert r.data["text"] == "a" * 65536 + "\n(обрезано)"


def test_read_text_binary(real_file: Any, confirms: Confirms) -> None:
    real_file(b"MZ\x90\x00\x03\x00")
    assert not files.read_text(WIN_PATH, "brain").ok


def test_read_text_redacted_for_brain(real_file: Any, confirms: Confirms) -> None:
    real_file("пароль: секрет".encode())
    assert files.read_text(WIN_PATH, "brain").data["text"] == "пароль: (скрыто)"


def test_read_text_closed_zone(fs: FakeFilesApi, confirms: Confirms) -> None:
    assert not files.read_text(ME + r"\AppData\Roaming\x.txt", "brain").ok
    assert not files.read_text(r"C:\Windows\win.ini", "brain").ok
    assert confirms.asked == []


# --- настоящий Windows (CI) ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="нужен Windows")
def test_real_known_folders() -> None:
    api = files._Api()
    for key, folder_id in files.FOLDER_IDS.items():
        path = api.known_folder(folder_id)
        assert PureWindowsPath(path).is_absolute(), key
    assert os.path.isdir(api.known_folder(files.FOLDER_IDS["documents"]))
    with pytest.raises(OSError):
        api.known_folder("{00000000-0000-0000-0000-000000000001}")
    api.allow_set_foreground()
