"""pc.paths: канонизация, зоны, типы файлов. Unit-тесты не зависят от ОС (фейковые пути Windows);
junction, 8.3 и symlink — на настоящем Windows (CI)."""

import ctypes
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from pc import paths

ORIG_ENV = dict(os.environ)
REAL_RESOLVE = paths._resolve
REAL_KNOWN_FOLDER = paths._known_folder

WIN_ENV = {
    "SystemRoot": r"C:\Windows",
    "windir": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files",
    "ProgramData": r"C:\ProgramData",
    "USERPROFILE": r"C:\Users\me",
    "APPDATA": r"C:\Users\me\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
}


def _clear_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture(autouse=True)
def fake_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Фейковое окружение Windows; _resolve — без изменений, известных папок ОС нет."""
    _clear_env(monkeypatch, *paths._ENV_NAMES)
    for name, value in WIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JARVIS_DATA_DIR", r"C:\JarvisData")
    monkeypatch.setattr(paths, "_resolve", lambda p: p)
    monkeypatch.setattr(paths, "_known_folder", lambda name: None)


def write_private(config_file, *private: str) -> None:
    body = ", ".join(json.dumps(p, ensure_ascii=False) for p in private)
    config_file(f"[pc]\nprivate_paths = [{body}]\n")


def fake_links(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> list[str]:
    """Подменить _resolve: префикс-ссылка → цель (как junction/8.3). Возвращает журнал вызовов."""
    calls: list[str] = []

    def resolve(p: str) -> str:
        calls.append(p)
        for src, dst in mapping.items():
            if p.casefold() == src.casefold() or p.casefold().startswith(src.casefold() + "\\"):
                return dst + p[len(src) :]
        return p

    monkeypatch.setattr(paths, "_resolve", resolve)
    return calls


def denied(path: str, caller: str = "user", op: str = "read") -> paths.PathCheck:
    res = paths.check(path, caller, op, is_dir=False)  # type: ignore[arg-type]
    assert not res.ok, f"{path!r} для {caller} должен быть закрыт"
    assert res.reason
    return res


def allowed(path: str, caller: str = "user", op: str = "read") -> paths.PathCheck:
    res = paths.check(path, caller, op, is_dir=False)  # type: ignore[arg-type]
    assert res.ok, f"{path!r} для {caller}: {res.reason}"
    return res


# --- canonical ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:/Users/me//Documents/./x/../отчёт.docx", r"C:\Users\me\Documents\отчёт.docx"),
        ("c:\\Users\\me\\", r"C:\Users\me"),
        (r"C:\Users\me\Documents\..\..\..\..\Windows", r"C:\Windows"),
        (r"C:\..\..\Users\me", r"C:\Users\me"),
        (r"C:/Users\me/Desktop\\план.txt", r"C:\Users\me\Desktop\план.txt"),
        (r'"C:\Users\me\Desktop\план.txt"', r"C:\Users\me\Desktop\план.txt"),
        ("  C:\\Users\\me\\x.txt  ", r"C:\Users\me\x.txt"),
        (r"C:\Windows.\System32", r"C:\Windows\System32"),
        (r"C:\Windows \System32", r"C:\Windows\System32"),
        (r"C:\Windows. .\System32", r"C:\Windows\System32"),
        (r"C:\Users\me\file.txt.", r"C:\Users\me\file.txt"),
        (r"C:\Users\me\file.txt . .", r"C:\Users\me\file.txt"),
        ("C:\\", "C:\\"),
        ("d:/", "D:\\"),
        (r"C:\Users\me\com10.txt", r"C:\Users\me\com10.txt"),
        (r"C:\Users\me\console.log", r"C:\Users\me\console.log"),
        (r"C:\Users\me\CONFIG", r"C:\Users\me\CONFIG"),
        (r"C:\Users\me\...txt", r"C:\Users\me\...txt"),
        (PureWindowsPath(r"C:\Users\me\Музыка"), r"C:\Users\me\Музыка"),
    ],
)
def test_canonical_normalizes(raw: str, expected: str) -> None:
    assert paths.canonical(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "Пустой"),
        ("   ", "Пустой"),
        ('""', "Пустой"),
        (None, "строкой"),
        (5, "строкой"),
        (r"Documents\x.txt", "полный путь"),
        (r".\x.txt", "полный путь"),
        (r"..\x.txt", "полный путь"),
        (r"~\Documents", "полный путь"),
        (r"%APPDATA%\x", "полный путь"),
        (r"\Windows\System32", "полный путь"),
        (r"\??\C:\Windows", "полный путь"),
        ("Д:\\x", "полный путь"),
        ("C:", "C:файл"),
        (r"C:Windows\System32", "C:файл"),
        ("c:file.txt", "C:файл"),
        (r"\\server\share\x", "UNC"),
        ("//server/share/x", "UNC"),
        (r"\\localhost\C$\Windows", "UNC"),
        ("\\/server/share", "UNC"),
        (r"\\?\C:\Windows", "\\\\?\\"),
        (r"\\.\C:\Windows", "\\\\?\\"),
        ("//?/C:/Windows", "\\\\?\\"),
        ("//./PhysicalDrive0", "\\\\?\\"),
        (r"\\?\UNC\server\share\x", "\\\\?\\"),
        (r"\\?\GLOBALROOT\Device\HarddiskVolume1\x", "\\\\?\\"),
        (r"\\.\pipe\jarvis", "\\\\?\\"),
        (r"C:\Users\me\file.txt:secret", "Двоеточие"),
        (r"C:\Users\me\file.txt::$DATA", "Двоеточие"),
        (r"C:\Users\me\file.txt:Zone.Identifier:$DATA", "Двоеточие"),
        (r"C:\Users\me\dir:x\file.txt", "Двоеточие"),
        (r"C:\Users\me\CON", "устройств"),
        (r"C:\Users\me\nul.txt", "устройств"),
        (r"C:\Users\me\COM1.log", "устройств"),
        (r"C:\Users\me\lpt9", "устройств"),
        (r"C:\Users\me\AUX.tar.gz", "устройств"),
        (r"C:\Users\me\con\x.txt", "устройств"),
        ("C:\\Users\\me\\PRN ", "устройств"),
        (r"C:\Users\me\nul .txt", "устройств"),
        ("C:\\Users\\me\\COM¹", "устройств"),
        (r"C:\Users\me\a*.txt", "символы"),
        (r"C:\Users\me\x?", "символы"),
        (r"C:\Users\me\a<b", "символы"),
        (r"C:\Users\me\a|b", "символы"),
        ('C:\\Users\\me\\a"b', "символы"),
        ("C:\\Users\\me\\a\x00b", "символы"),
        ("C:\\Users\\me\\a\nb", "символы"),
        (r"C:\Users\me\...\x", "точки"),
        (r"C:\Users\me\.. \x", "точки"),
        ("C:\\Users\\me\\ \\x", "точки"),
    ],
)
def test_canonical_denied(raw: object, reason: str) -> None:
    with pytest.raises(paths.PathDenied, match=re.escape(reason)):
        paths.canonical(raw)  # type: ignore[arg-type]
    res = paths.check(raw, "user", "open")  # type: ignore[arg-type]
    assert not res.ok and res.path == "" and reason in res.reason


@pytest.mark.parametrize(
    "target",
    [r"\\server\share\x", r"\\?\C:\Windows\x", r"C:\Users\me\x.txt:evil", r"\\?\UNC\server\share"],
)
def test_form_checked_again_after_resolve(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    # junction или ссылка может вести на сетевой путь — проверка формы повторяется
    monkeypatch.setattr(paths, "_resolve", lambda p: target)
    with pytest.raises(paths.PathDenied):
        paths.canonical(r"C:\Users\me\link\x")
    assert not paths.check(r"C:\Users\me\link\x", "user", "open").ok


def test_link_into_zone_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_links(monkeypatch, {r"C:\Users\me\link": r"C:\Windows\System32"})
    assert paths.canonical(r"C:\Users\me\link\cmd.exe") == r"C:\Windows\System32\cmd.exe"
    res = denied(r"C:\Users\me\link\cmd.exe", "user", "open")
    assert res.path == r"C:\Windows\System32\cmd.exe"
    assert "системная папка Windows" in res.reason


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows _resolve обращается к диску")
def test_resolve_is_identity_off_windows() -> None:
    assert REAL_RESOLVE(r"C:\PROGRA~1\App") == r"C:\PROGRA~1\App"
    assert REAL_KNOWN_FOLDER("Profile") is None


# --- is_within ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        (r"C:\Users\me", r"C:\Users\me", True),
        (r"C:\Users\me\x", r"C:\Users\me", True),
        (r"C:\Users\meow", r"C:\Users\me", False),
        (r"C:\Users\meow\x", r"C:\Users\me", False),
        (r"C:\Users\me", r"C:\Users\meow", False),
        ("c:/USERS/ME/x", r"C:\Users\me", True),
        (r"C:\Users", r"C:\Users\me", False),
        (r"D:\Users\me", r"C:\Users\me", False),
        (r"C:\Program Files (x86)\App", r"C:\Program Files", False),
        (r"C:\Program Files\App", r"C:\Program Files (x86)", False),
        (r"C:\Windows.\System32", r"C:\Windows", True),
        (r"C:\x", "", False),
        (r"C:\x", "x", False),
        (r"\\server\share\x", r"\\server\share", False),
        (r"C:\Users\me\Ёлка\x", r"c:\users\ME\ёлка", True),
    ],
)
def test_is_within(path: str, root: str, expected: bool) -> None:
    assert paths.is_within(path, root) is expected


# --- зоны всем ----------------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["user", "brain"])
@pytest.mark.parametrize(
    "path",
    [
        r"C:\Windows\System32\cmd.exe",
        r"c:\WINDOWS\notepad.exe",
        "C:/Windows/System32",
        r"C:\Windows.\System32\cmd.exe",
        r"C:\Windows \System32\cmd.exe",
        r"C:\Users\me\Documents\..\..\..\Windows\System32",
        r"C:\Program Files\App\app.exe",
        r"C:\Program Files.\App\app.exe",
        r"C:\Program Files (x86)\App\app.exe",
        r"C:\Program Files (x86) \App",
        r"C:\ProgramData\App\x.txt",
        r"C:\ProgramData. . .\App",
        r"C:\Users\me\AppData\Roaming\Microsoft\Windows\Start Menu",
        r"C:\Users\me\.ssh\id_ed25519",
        r"C:\USERS\ME\.SSH",
        r"C:\Users\me\.codex\auth.json",
    ],
)
def test_zones_closed_for_everyone(path: str, caller: str) -> None:
    res = denied(path, caller, "open")
    assert "закрытой зоне" in res.reason


@pytest.mark.parametrize("caller", ["user", "brain"])
@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\meow\.ssh\id_rsa.pub",
        r"C:\Program Files Extra\x.txt",
        r"C:\WindowsApps2\x.txt",
        r"C:\ProgramDataBackup\x.txt",
        r"C:\Users\me\Documents\Windows\x.txt",
        r"C:\Users\me\Documents\отчёт.docx",
        r"D:\Музыка\песня.mp3",
    ],
)
def test_neighbours_of_zones_allowed(path: str, caller: str) -> None:
    allowed(path, caller, "open")


def test_program_files_and_x86_are_separate_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch, "ProgramFiles(x86)", "ProgramW6432")
    denied(r"C:\Program Files (x86)\App\x.txt")  # умолчание, переменной нет
    monkeypatch.setenv("ProgramFiles(x86)", r"D:\Apps32")
    denied(r"D:\Apps32\App\x.txt")
    denied(r"C:\Program Files\App\x.txt")
    allowed(r"C:\Program Files (x86)\App\x.txt")  # не выдумываем: зона — из переменной


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch, *paths._ENV_NAMES)
    for path in (
        r"C:\Windows\x.txt",
        r"C:\Program Files\x.txt",
        r"C:\Program Files (x86)\x.txt",
        r"C:\ProgramData\x.txt",
    ):
        denied(path)
    # профиля нет ни в переменных, ни у ОС — зоны профиля не выдумываются
    allowed(r"C:\Users\me\.ssh\id_rsa")
    allowed(r"C:\Users\me\AppData\Roaming\Telegram Desktop\x", "brain")


def test_env_values_are_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    monkeypatch.setenv("windir", r"E:\Win")
    monkeypatch.setenv("ProgramW6432", r"F:\Programs")
    monkeypatch.setenv("ProgramData", r"G:\Data")
    for path in (r"D:\Win\x", r"E:\Win\x", r"F:\Programs\x", r"G:\Data\x"):
        denied(path)
    allowed(r"C:\Windows\x.txt")  # переменные заданы — умолчание не добавляется


def test_appdata_derived_from_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch, "APPDATA", "LOCALAPPDATA")
    denied(r"C:\Users\me\AppData\Roaming\Microsoft\Credentials\x")
    denied(r"C:\Users\me\AppData\Roaming\Telegram Desktop\tdata", "brain")
    denied(r"C:\Users\me\AppData\Local\Google\Chrome\User Data", "brain")
    allowed(r"C:\Users\me\AppData\Local\Google\Chrome\User Data", "user")


def test_known_folders_when_env_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # MCP-процесс мозга стартует с очищенным окружением: зоны берутся у ОС
    _clear_env(monkeypatch, *paths._ENV_NAMES)
    folders = {
        "Profile": r"C:\Users\me",
        "RoamingAppData": r"C:\Users\me\AppData\Roaming",
        "LocalAppData": r"C:\Users\me\AppData\Local",
        "Windows": r"D:\Windows",
    }
    monkeypatch.setattr(paths, "_known_folder", folders.get)
    denied(r"C:\Users\me\.ssh\id_rsa")
    denied(r"D:\Windows\System32")
    denied(r"C:\Users\me\AppData\Local\x", "brain")
    denied(r"C:\Users\me\.aws\credentials", "brain")


# --- зоны мозга ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (r"C:\Users\me\AppData\Roaming\Telegram Desktop\tdata\key_datas", "данные программ"),
        (r"C:\Users\me\AppData\Local\Google\Chrome\User Data\Default\Login Data", "данные программ"),
        (r"C:\Users\me\appdata\local", "данные программ"),
        (r"C:\JarvisData", "данные Jarvis"),
        (r"C:\JarvisData\pipe.key", "данные Jarvis"),
        (r"c:\jarvisdata\codex-home\auth.json", "данные Jarvis"),
        (r"C:\Users\me\.config\gh\hosts.yml", "скрытая папка"),
        (r"C:\Users\me\.aws\credentials", "скрытая папка"),
        (r"C:\Users\me\.gitconfig", "скрытая папка"),
        (r"C:\Users\me\Documents\proj\.env", "секрет"),
        (r"C:\Users\me\Documents\proj\.ENV.local", "секрет"),
        (r"C:\Users\me\Documents\vault.KDBX", "секрет"),
        (r"C:\Users\me\Documents\cert.pem", "секрет"),
        (r"C:\Users\me\Documents\cert.pfx", "секрет"),
        (r"C:\Users\me\Documents\cert.p12", "секрет"),
        (r"C:\Users\me\Documents\server.key", "секрет"),
        (r"D:\backup\auth.json", "секрет"),
        (r"D:\repo\.git-credentials", "секрет"),
        (r"D:\repo\.env.", "секрет"),
    ],
)
def test_brain_zones(path: str, label: str) -> None:
    res = denied(path, "brain")
    assert "закрыт для GPT" in res.reason and label in res.reason
    assert paths.hidden_for_brain(path)
    allowed(path, "user")


def test_data_dir_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", r"D:\Мои данные\Jarvis")
    denied(r"D:\Мои данные\Jarvis\journal\2026-10-10.jsonl", "brain")
    denied(r"d:\мои данные\JARVIS", "brain")
    allowed(r"D:\Мои данные\Jarvis\journal\2026-10-10.jsonl", "user")
    allowed(r"D:\Мои данные\Jarvis2\x.txt", "brain")
    allowed(r"C:\JarvisData\notes.txt", "brain")  # старое значение больше не зона


def test_config_file_closed_for_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG", r"C:\Jarvis\jarvis.toml")
    denied(r"C:\Jarvis\jarvis.toml", "brain")
    allowed(r"C:\Jarvis\jarvis.toml", "user")
    allowed(r"C:\Jarvis\README.md", "brain")


def test_hidden_folder_rule_is_per_profile() -> None:
    allowed(r"C:\Users\me\Documents\.notes\x.md", "brain")  # точка не сразу после профиля
    allowed(r"C:\Users\meow\.config\x", "brain")


def test_hidden_for_brain() -> None:
    for path in (r"\\server\share\x", r"C:\x\file.txt:s", "relative\\x", r"C:\Windows", r"C:\Users\me\.ssh"):
        assert paths.hidden_for_brain(path)
    for path in (r"C:\Users\me\Documents\отчёт.docx", r"D:\Музыка\a.mp3", r"C:\Users\me\Downloads\setup.exe"):
        assert not paths.hidden_for_brain(path)


# --- private_paths ------------------------------------------------------------------------------


def test_private_paths(config_file) -> None:
    write_private(
        config_file, r"D:\Личное", r"C:\Users\me\Documents\Тайное", r"relative\x", r"\\nas\share", ""
    )
    for path in (
        r"D:\Личное\фото.jpg",
        r"d:\личное",
        r"D:\ЛИЧНОЕ\x",
        r"D:\Личное.\x",
        "D:/Личное/папка/../x.txt",
        r"C:\Users\me\Documents\Тайное\план.docx",
    ):
        for caller in ("user", "brain"):
            res = denied(path, caller)
            assert "закрытая папка из настроек" in res.reason
            assert "Личное" not in res.reason and "Тайное" not in res.reason
    allowed(r"D:\Личное2\x.txt")
    allowed(r"D:\Лич\x.txt")
    allowed(r"C:\Users\me\Documents\Тайное2.docx")


def test_private_paths_reloaded(config_file) -> None:
    write_private(config_file, r"D:\Старое")
    denied(r"D:\Старое\x.txt")
    write_private(config_file, r"D:\Новое")
    allowed(r"D:\Старое\x.txt")
    denied(r"D:\Новое\x.txt")


def test_broken_config_closes_paths_for_brain(config_file) -> None:
    config_file("[pc\nprivate_paths = [")
    res = denied(r"C:\Users\me\Documents\отчёт.docx", "brain")
    assert "jarvis.toml" in res.reason
    allowed(r"C:\Users\me\Documents\отчёт.docx", "user")


# --- 8.3 и канонизация зон через _resolve -------------------------------------------------------


def test_short_names(monkeypatch: pytest.MonkeyPatch) -> None:
    res = denied(r"C:\PROGRA~1\App\x.txt", "brain")
    assert "8.3" in res.reason
    denied(r"C:\Users\me\DOCUME~1\x.txt", "brain")
    denied(r"C:\Users\me\Documents\OTCHE~12.DOC", "brain")
    # user: развернуть нельзя — работаем с тем, что есть
    assert allowed(r"C:\PROGRA~1\App\x.txt", "user").path == r"C:\PROGRA~1\App\x.txt"
    allowed(r"C:\Users\me\file~name.txt", "brain")
    allowed(r"C:\Users\me\very-long~1.txt", "brain")


def test_short_names_expanded_by_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_links(monkeypatch, {r"C:\PROGRA~1": r"C:\Program Files"})
    res = denied(r"C:\PROGRA~1\App\app.exe", "user", "open")
    assert res.path == r"C:\Program Files\App\app.exe"


def test_zones_canonicalized_through_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    # на раннере CI %TEMP% = C:\Users\RUNNER~1\…: сравниваются пути, канонизированные с обеих сторон
    monkeypatch.setenv("ProgramFiles", r"C:\PROGRA~1")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\RUNNER~1")
    _clear_env(monkeypatch, "APPDATA", "LOCALAPPDATA", "ProgramW6432")
    fake_links(
        monkeypatch, {r"C:\PROGRA~1": r"C:\Program Files", r"C:\Users\RUNNER~1": r"C:\Users\runneradmin"}
    )
    denied(r"C:\Program Files\App\x.txt")
    denied(r"C:\Users\runneradmin\.ssh\id_rsa")
    denied(r"C:\Users\RUNNER~1\.ssh\id_rsa")
    denied(r"C:\Users\runneradmin\AppData\Local\Temp\x.txt", "brain")


def test_zone_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_links(monkeypatch, {})
    paths.check(r"C:\Users\me\x.txt", "user", "read")
    first = len(calls)
    assert first > 1  # зоны канонизированы
    paths.check(r"C:\Users\me\y.txt", "user", "read")
    assert len(calls) == first + 1  # только сам путь, зоны — из кэша
    monkeypatch.setenv("ProgramData", r"D:\ProgramData")
    paths.check(r"C:\Users\me\z.txt", "user", "read")
    assert len(calls) > first + 2  # переменная изменилась — зоны пересобраны
    denied(r"D:\ProgramData\x")


# --- типы файлов и подтверждение ----------------------------------------------------------------

ALL_EXECUTABLE = sorted(paths.EXECUTABLE_SUFFIXES)


@pytest.mark.parametrize("suffix", ALL_EXECUTABLE + [s.upper() for s in ALL_EXECUTABLE])
@pytest.mark.parametrize("caller", ["user", "brain"])
def test_open_executable_needs_confirm(suffix: str, caller: str) -> None:
    res = allowed(rf"C:\Users\me\Downloads\file{suffix}", caller, "open")
    assert res.confirm


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\Downloads\setup",
        r"C:\Users\me\Downloads\SETUP.EXE",
        r"C:\Users\me\Downloads\setup.exe.",
        r"C:\Users\me\Downloads\setup.exe  ",
        r"C:\Users\me\Downloads\отчёт.pdf.exe",
        r"C:\Users\me\Desktop\Telegram.LNK",
        r"C:\Users\me\Desktop\.bashrc",
        r"C:\Users\me\Desktop\console.msc",
        r"C:\Users\me\Desktop\help.chm",
    ],
)
def test_open_confirm_cases(path: str) -> None:
    assert allowed(path, "user", "open").confirm
    assert paths.is_executable_type(path)


@pytest.mark.parametrize(
    ("path", "op", "is_dir"),
    [
        (r"C:\Users\me\Documents\отчёт.docx", "open", False),
        (r"C:\Users\me\Documents\отчёт.txt", "open", None),
        (r"C:\Users\me\Music\a.mp3", "open", False),
        (r"C:\Users\me\Downloads\setup.exe", "read", False),
        (r"C:\Users\me\Downloads\setup.exe", "list", False),
        (r"C:\Users\me\Downloads\setup.exe", "trash", False),
        (r"C:\Users\me\Projects\tool.exe", "open", True),
        (r"C:\Users\me\Projects", "open", True),
        ("D:\\", "open", None),
    ],
)
def test_open_without_confirm(path: str, op: str, is_dir: bool | None) -> None:
    res = paths.check(path, "user", op, is_dir=is_dir)  # type: ignore[arg-type]
    assert res.ok and not res.confirm


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\x\a.exe", True),
        (r"C:\x\a.Exe", True),
        (r"C:\x\a.appref-ms", True),
        (r"C:\x\a.SettingContent-ms", True),
        (r"C:\x\a", True),
        (r"C:\x\a.txt", False),
        (r"C:\x\a.exe.txt", False),
        (r"C:\x\a.jpeg", False),
        ("C:\\", False),
    ],
)
def test_is_executable_type(path: str, expected: bool) -> None:
    assert paths.is_executable_type(path) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\x\.env", True),
        (r"C:\x\.ENV", True),
        (r"C:\x\.env.production", True),
        (r"C:\x\db.kdbx", True),
        (r"C:\x\AUTH.JSON", True),
        (r"C:\x\.git-credentials", True),
        (r"C:\x\id.key ", True),
        (r"C:\x\environment.txt", False),
        (r"C:\x\my.env.txt", False),
        (r"C:\x\auth.json.bak", False),
        (r"C:\x\keys", False),
    ],
)
def test_is_secret_file(path: str, expected: bool) -> None:
    assert paths.is_secret_file(path) is expected


def test_check_returns_canonical_path() -> None:
    res = paths.check("c:/Users/me/Documents/../отчёт.docx", "user", "open", is_dir=False)
    assert res == paths.PathCheck(True, r"C:\Users\me\отчёт.docx", "", False)


def test_check_rejects_unknown_op_and_caller() -> None:
    with pytest.raises(ValueError):
        paths.check(r"C:\x.txt", "user", "write")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        paths.check(r"C:\x.txt", "admin", "read")  # type: ignore[arg-type]


def test_trash_never_takes_zones_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    denied(r"C:\Users\me", "user", "trash")  # внутри ~\.ssh
    denied(r"C:\Users", "user", "trash")
    denied("D:\\", "user", "trash")
    denied(r"C:\Users\me\AppData", "user", "trash")
    allowed(r"C:\Users\me\Documents\старое", "user", "trash")
    monkeypatch.setenv("JARVIS_DATA_DIR", r"D:\Data\Jarvis")
    denied(r"D:\Data", "brain", "trash")
    allowed(r"D:\Data", "user", "trash")


# --- настоящий Windows (CI) ---------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="нужен настоящий Windows")


@pytest.fixture
def real_os(monkeypatch: pytest.MonkeyPatch, _isolated_data: Path) -> None:
    """Вернуть настоящие _resolve, известные папки и переменные окружения."""
    for name in paths._ENV_NAMES:
        value = ORIG_ENV.get(name) or ORIG_ENV.get(name.upper())
        if value:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(_isolated_data))
    monkeypatch.setattr(paths, "_resolve", REAL_RESOLVE)
    monkeypatch.setattr(paths, "_known_folder", REAL_KNOWN_FOLDER)


def _short_path(path: str) -> str:
    from ctypes import wintypes

    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    n = fn(path, buf, len(buf))
    return buf.value if 0 < n < len(buf) else path


@windows_only
def test_win_junction_resolves_to_target(tmp_path: Path, real_os: None, config_file) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    (target / "note.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr
    write_private(config_file, str(target))
    via_link = str(link / "note.txt")
    assert paths.canonical(via_link) == paths.canonical(str(target / "note.txt"))
    res = paths.check(via_link, "user", "read")
    assert not res.ok and "закрытая папка" in res.reason
    assert not paths.check(str(link / "new.txt"), "user", "open").ok  # хвоста ещё нет
    assert paths.check(str(tmp_path / "other.txt"), "user", "open", is_dir=False).ok


@windows_only
def test_win_short_name_expanded(tmp_path: Path, real_os: None, config_file) -> None:
    long_dir = tmp_path / "LongFolderNameForJarvisTest"
    long_dir.mkdir()
    (long_dir / "file.txt").write_text("x", encoding="utf-8")
    short = _short_path(str(long_dir))
    if short == str(long_dir):
        pytest.skip("8.3-имена на этом томе выключены")
    assert paths.canonical(short + "\\file.txt") == paths.canonical(str(long_dir / "file.txt"))
    write_private(config_file, str(long_dir))
    assert not paths.check(short + "\\file.txt", "user", "read").ok


@windows_only
def test_win_symlink_into_private_folder(tmp_path: Path, real_os: None, config_file) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    link = tmp_path / "slink"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink без прав (нет режима разработчика)")
    write_private(config_file, str(target))
    assert not paths.check(str(link / "a.txt"), "user", "read").ok


@windows_only
def test_win_real_zones(real_os: None) -> None:
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    assert not paths.check(system_root + r"\System32\notepad.exe", "user", "open").ok
    assert not paths.check(os.environ["PROGRAMFILES"] + r"\x\app.exe", "user", "open").ok
    assert not paths.check(os.environ["APPDATA"] + r"\Telegram Desktop\x", "brain", "read").ok
    assert not paths.check(os.environ["USERPROFILE"] + r"\.ssh\id_rsa", "user", "read").ok
    temp = os.environ["TEMP"]  # на раннере CI — C:\Users\RUNNER~1\…
    assert "~" not in paths.canonical(temp)
    assert paths.canonical(temp) == paths.canonical(str(Path(temp).resolve()))


@windows_only
def test_win_known_folders(real_os: None) -> None:
    profile = REAL_KNOWN_FOLDER("Profile")
    assert (
        profile
        and paths.canonical(profile).casefold() == paths.canonical(os.environ["USERPROFILE"]).casefold()
    )
    roaming = REAL_KNOWN_FOLDER("RoamingAppData")
    assert (
        roaming and paths.canonical(roaming).casefold() == paths.canonical(os.environ["APPDATA"]).casefold()
    )
    assert REAL_KNOWN_FOLDER("Windows")


@windows_only
def test_win_resolve_strips_verbatim_prefix() -> None:
    resolved = REAL_RESOLVE(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    assert not resolved.startswith("\\\\")
