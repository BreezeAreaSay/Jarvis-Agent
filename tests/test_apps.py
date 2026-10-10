"""pc.apps: resolve на общей фикстуре, разбор Get-StartApps, кэш, алиасы, автообновление, STA-поток."""

import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path

import pytest

from pc import apps, subproc
from pc.apps import App

# (фраза, ожидаемое имя; "" — приложения нет, счёт ниже порога)
CASES = [
    ("телеграм", "Telegram"),
    ("телега", "Telegram"),
    ("телегу", "Telegram"),
    ("телеги", "Telegram"),
    ("Telegram", "Telegram"),
    ("телграм", "Telegram"),
    ("тг", "Telegram"),
    ("приложение телеграм", "Telegram"),
    ("хром", "Google Chrome"),
    ("хрома", "Google Chrome"),
    ("хроме", "Google Chrome"),
    ("гугл хром", "Google Chrome"),
    ("chrom", "Google Chrome"),
    ("ворд", "Word"),
    ("ворде", "Word"),
    ("эксель", "Excel"),
    ("excell", "Excel"),
    ("вскод", "Visual Studio Code"),
    ("вс код", "Visual Studio Code"),
    ("VS Code", "Visual Studio Code"),
    ("visual studio", "Visual Studio Code"),
    ("параметры", "Параметры"),
    ("настройки", "Параметры"),
    ("settings", "Параметры"),
    ("проводник", "Проводник"),
    ("праводник", "Проводник"),
    ("explorer", "Проводник"),
    ("блокнот", "Блокнот"),
    ("блакнот", "Блокнот"),
    ("notepad", "Блокнот"),
    ("notepad++", "Notepad++"),
    ("дискорд", "Discord"),
    ("дискорт", "Discord"),
    ("стим", "Steam"),
    ("стима", "Steam"),
    ("обс", "OBS Studio"),
    ("спотифай", "Spotify"),
    ("калькулятор", "Калькулятор"),
    ("калькулятр", "Калькулятор"),
    ("kalkulyator", "Калькулятор"),
    ("диспетчер задач", "Диспетчер задач"),
    ("эдж", "Microsoft Edge"),
    ("файрфокс", "Mozilla Firefox"),
    ("пауэрпоинт", "PowerPoint"),
    ("аутлук", "Outlook (classic)"),
    ("зум", "Zoom Workplace"),
    ("ватсап", "WhatsApp"),
    ("яндекс музыку", "Яндекс Музыка"),
    ("почту", "Почта"),
    ("фотки", "Фотографии"),
    ("панели управления", "Панель управления"),
    ("командную строку", "Командная строка"),
    ("магазин", "Microsoft Store"),
    ("обсидиан", "Obsidian"),
    ("эверитинг", "Everything"),
    ("гимп", "GIMP 2.10"),
    ("торрент", "qBittorrent"),
    ("7zip", "7-Zip File Manager"),
    ("фотошоп", ""),
    ("автокад", ""),
    ("майнкрафт", ""),
    ("скайп", ""),
    ("тимс", ""),
    ("фигма", ""),
    ("нетфликс", ""),
    ("диск", ""),
    ("гугл диск", ""),
    ("диспетчер устройств", ""),
    ("media player", ""),
]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, "_inv", None)
    monkeypatch.setattr(apps, "_auto_refresh", False)
    monkeypatch.setattr(apps, "_last_auto_refresh", 0.0)


@pytest.fixture
def inv(apps_fixture: list[dict[str, str]]) -> list[App]:
    apps.set_inventory(apps_fixture)
    return apps.inventory()


def write_cache(items: list[dict[str, str]], bump_ns: int = 0) -> Path:
    path = apps.cache_path()
    path.write_text(
        json.dumps({"version": 1, "updated": 0, "apps": items}, ensure_ascii=False), encoding="utf-8"
    )
    if bump_ns:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))
    return path


def test_resolve_accuracy(inv: list[App]) -> None:
    misses = []
    for phrase, expected in CASES:
        app, score = apps.resolve(phrase)
        got = app.name if app else ""
        if got != expected:
            misses.append((phrase, expected, got, score))
        if app is None:
            assert score < apps.THRESHOLD
        else:
            assert score >= apps.THRESHOLD
    accuracy = 1 - len(misses) / len(CASES)
    assert len(CASES) >= 60
    assert accuracy >= 0.95, misses


def test_resolve_speed(inv: list[App]) -> None:
    phrases = [p for p, _ in CASES]
    for p in phrases[:20]:
        apps.resolve(p)
    times = []
    for i in range(200):
        t0 = time.perf_counter()
        apps.resolve(phrases[i % len(phrases)])
        times.append(time.perf_counter() - t0)
    limit_ms = 5.0 * (3 if os.environ.get("CI") else 1)
    assert statistics.median(times) * 1000 < limit_ms


def test_resolve_empty_inventory_and_blank() -> None:
    assert apps.inventory() == []
    assert apps.resolve("телега") == (None, 0.0)
    assert apps.resolve("   ") == (None, 0.0)
    assert apps.top("телега") == []


def test_top_dedupes_by_app_id(inv: list[App]) -> None:
    extra = [*inv, App("Telegram Desktop", "TelegramDesktop.TelegramDesktop")]
    apps.set_inventory(extra)
    found = apps.top("телеграм", 5)
    ids = [a.app_id for a, _ in found]
    assert ids[0] == "TelegramDesktop.TelegramDesktop"
    assert len(ids) == len(set(ids))
    assert found == sorted(found, key=lambda t: -t[1])


def test_aliases_from_config(inv: list[App], config_file) -> None:
    config_file('[aliases]\n"Мессенджер" = "Telegram"\n"рисовалка" = "paint"\n"пустышка" = "Нет такого"\n')
    assert apps.resolve("мессенджер") == (next(a for a in inv if a.name == "Telegram"), 100.0)
    app, _ = apps.resolve("Рисовалка")
    assert app is not None and app.name == "Paint"
    assert apps.resolve("пустышка")[0] is None
    assert apps.top("мессенджер", 1)[0][0].name == "Telegram"


def test_name_variants_and_app_exe(inv: list[App]) -> None:
    assert "telegram" in apps.name_variants("телега")
    assert "steam" in apps.name_variants("стима")
    assert "chrome" in apps.name_variants("хрома")
    by_name = {a.name: a for a in inv}
    assert apps.app_exe(by_name["Steam"]) == "steam.exe"
    assert apps.app_exe(by_name["Word"]) == "winword.exe"
    assert apps.app_exe(by_name["Discord"]) == "discord.exe"
    assert apps.app_exe(by_name["Telegram"]) == "telegram.exe"
    assert apps.app_exe(by_name["Google Chrome"]) == "chrome.exe"
    assert apps.app_exe(by_name["Командная строка"]) == "cmd.exe"
    assert apps.app_exe(by_name["Яндекс Музыка"]) is None


def test_normalize_and_latin() -> None:
    assert apps.normalize("  Ёлка: Notepad++ (x64)!") == "елка notepad plus plus x64"
    assert apps.latin("эксель") == apps.latin("Excel")
    assert apps.latin("дискорд") == apps.latin("Discord")


# --- Get-StartApps --------------------------------------------------------------------------------

PS_ITEMS = [
    {"Name": "Telegram", "AppID": "TelegramDesktop.TelegramDesktop"},
    {
        "Name": "Удалить Telegram",
        "AppID": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\Telegram Desktop\\unins000.exe",
    },
    {"Name": "Uninstall Steam", "AppID": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\Steam\\uninstall.exe"},
    {"Name": "Steam", "AppID": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\Steam\\steam.exe"},
    {"Name": "Справка OBS", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\obs-studio\\help.chm"},
    {"Name": "Readme", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\Tool\\readme.txt"},
    {"Name": "Example Website", "AppID": "https://example.com/"},
    {"Name": "Release Notes", "AppID": "{6D809377-6AF0-444B-8957-A3773F02200E}\\Tool\\notes.url"},
    {"Name": "Документация Tool", "AppID": "Tool.Docs"},
    {"Name": "Блокнот", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"Name": "Блокнот", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"Name": "", "AppID": "Empty.Name"},
]


def ps_bytes(value: object, bom: bool = False) -> bytes:
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def test_parse_start_apps_filters_junk() -> None:
    got = apps.parse_start_apps(ps_bytes(PS_ITEMS, bom=True))
    assert [a.name for a in got] == ["Telegram", "Steam", "Блокнот"]


def test_parse_start_apps_single_object_and_empty() -> None:
    one = {"Name": "Калькулятор", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"}
    assert apps.parse_start_apps(ps_bytes(one)) == [App("Калькулятор", one["AppID"])]
    assert apps.parse_start_apps(b"  \r\n") == []


def test_is_junk() -> None:
    assert apps.is_junk("Удаление программы", "X.App")
    assert apps.is_junk("Help and Support", "X.App")
    assert apps.is_junk("Tool", "{GUID}\\Tool\\unins001.exe")
    assert not apps.is_junk("Telegram", "TelegramDesktop.TelegramDesktop")
    assert not apps.is_junk("Helper Tool", "Helper.Tool")


class FakeShell:
    def __init__(self, names: list[tuple[str, str]] | Exception) -> None:
        self.names = names

    def shell_app_names(self) -> list[tuple[str, str]]:
        if isinstance(self.names, Exception):
            raise self.names
        return self.names


def fake_run(monkeypatch: pytest.MonkeyPatch, result: subproc.Completed | Exception) -> list[tuple]:
    calls: list[tuple] = []

    def run(argv, timeout, cwd=None, env=None):
        calls.append((list(argv), timeout))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(apps.subproc, "run", run)
    return calls


def test_refresh_writes_cache_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, subproc.Completed(0, ps_bytes(PS_ITEMS), b""))
    shell = [
        ("Telegram Desktop", "telegramdesktop.telegramdesktop"),
        ("Telegram", "TelegramDesktop.TelegramDesktop"),
        ("Чужое", "Other.App"),
        ("Удалить Steam", "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\Steam\\steam.exe"),
    ]
    monkeypatch.setattr(apps, "_api", FakeShell(shell))
    got = apps.refresh()
    assert calls == [(apps.REFRESH_ARGV, 30)]
    assert calls[0][0][0].endswith(r"WindowsPowerShell\v1.0\powershell.exe")
    assert "@(Get-StartApps | Select-Object Name,AppID)" in calls[0][0][-1]
    assert App("Telegram Desktop", "TelegramDesktop.TelegramDesktop") in got
    assert all(a.app_id != "Other.App" for a in got)
    raw = json.loads(apps.cache_path().read_text(encoding="utf-8"))
    assert raw["version"] == 1 and isinstance(raw["updated"], int)
    assert {"name": "Steam", "app_id": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\\Steam\\steam.exe"} in raw[
        "apps"
    ]
    assert list(apps.cache_path().parent.glob("*.tmp")) == []
    assert apps.inventory() == got
    assert apps.resolve("телеграм десктоп")[0] is not None


def test_refresh_without_display_names(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, subproc.Completed(0, ps_bytes(PS_ITEMS), b""))
    monkeypatch.setattr(apps, "_api", FakeShell(RuntimeError("COM недоступен")))
    assert [a.name for a in apps.refresh()] == ["Telegram", "Steam", "Блокнот"]


@pytest.mark.parametrize(
    "result",
    [
        subproc.Completed(1, b"", "Get-StartApps: ошибка".encode()),
        subproc.Completed(0, b"not json", b""),
        subproc.Completed(0, b"[]", b""),
        subprocess.TimeoutExpired("powershell", 30),
        FileNotFoundError("powershell.exe"),
    ],
)
def test_refresh_error_keeps_old_cache(monkeypatch: pytest.MonkeyPatch, result) -> None:
    path = write_cache([{"name": "Старое", "app_id": "Old.App"}])
    before = path.read_bytes()
    fake_run(monkeypatch, result)
    monkeypatch.setattr(apps, "_api", FakeShell([]))
    with pytest.raises(apps.RefreshError):
        apps.refresh()
    assert path.read_bytes() == before
    assert apps.inventory() == [App("Старое", "Old.App")]


def test_refresh_replace_failure_cleans_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_cache([{"name": "Старое", "app_id": "Old.App"}])
    fake_run(monkeypatch, subproc.Completed(0, ps_bytes(PS_ITEMS), b""))
    monkeypatch.setattr(apps, "_api", FakeShell([]))

    def broken_replace(src, dst):
        raise PermissionError("занято")

    monkeypatch.setattr(apps.os, "replace", broken_replace)
    with pytest.raises(PermissionError):
        apps.refresh()
    assert json.loads(path.read_text(encoding="utf-8"))["apps"][0]["name"] == "Старое"
    assert list(path.parent.glob("*.tmp")) == []


# --- кэш и инвентарь --------------------------------------------------------------------------------


def test_inventory_from_cache_and_reload_on_change() -> None:
    write_cache([{"name": "Telegram", "app_id": "TelegramDesktop.TelegramDesktop"}])
    assert apps.inventory() == [App("Telegram", "TelegramDesktop.TelegramDesktop")]
    write_cache(
        [{"name": "Telegram", "app_id": "TelegramDesktop.TelegramDesktop"}, {"name": "Steam", "app_id": "S"}],
        bump_ns=1_000_000,
    )
    assert [a.name for a in apps.inventory()] == ["Telegram", "Steam"]
    assert apps.resolve("стим")[0] == App("Steam", "S")


def test_inventory_bad_cache_is_empty() -> None:
    apps.cache_path().write_text("{испорчен", encoding="utf-8")
    assert apps.inventory() == []
    write_cache([{"name": "X", "app_id": ""}, {"name": 5, "app_id": "Y"}], bump_ns=1_000_000)
    assert apps.inventory() == []


def test_set_inventory_overrides_cache_and_none_restores() -> None:
    write_cache([{"name": "Steam", "app_id": "S"}])
    apps.set_inventory([App("Telegram", "T"), {"name": "Discord", "app_id": "D"}])
    assert [a.name for a in apps.inventory()] == ["Telegram", "Discord"]
    apps.set_inventory(None)
    assert [a.name for a in apps.inventory()] == ["Steam"]


def test_auto_refresh_on_miss_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    apps.set_inventory([App("Telegram", "T")])
    calls = []

    def fake_refresh() -> list[App]:
        calls.append(1)
        apps.set_inventory([App("Telegram", "T"), App("Obsidian", "md.obsidian")])
        return apps.inventory()

    monkeypatch.setattr(apps, "refresh", fake_refresh)
    assert apps.resolve("обсидиан")[0] is None  # по умолчанию выключено
    assert calls == []
    apps.enable_auto_refresh(True)
    assert apps.lookup("обсидиан")[0] is None  # lookup не обновляет никогда
    assert calls == []
    assert apps.resolve("обсидиан")[0] == App("Obsidian", "md.obsidian")
    assert apps.resolve("фотошоп")[0] is None
    assert calls == [1]  # второй промах в течение 10 минут — без обновления


def test_list_apps_result(inv: list[App]) -> None:
    r = apps.list_apps_result(None, "brain")
    assert r.ok and len(r.data) == len({a.name for a in inv})
    assert {"name": "Telegram"} in r.data
    r = apps.list_apps_result("телега", "user")
    assert r.ok and r.data[0] == {"name": "Telegram", "score": 100.0}
    assert len(r.data) <= 5 and all(set(d) == {"name", "score"} for d in r.data)
    r = apps.list_apps_result("фотошоп", "user")
    assert r.ok and "не найдено" in r.text


def test_list_apps_result_empty() -> None:
    r = apps.list_apps_result("", "user")
    assert not r.ok and "refresh" in r.text


# --- STA-поток ------------------------------------------------------------------------------------


def test_run_sta_result_and_thread() -> None:
    names = {apps.run_sta(lambda: threading.current_thread().name) for _ in range(3)}
    assert names == {"jarvis-sta"}
    assert apps.run_sta(lambda a, b: a + b, 2, 3) == 5
    # вложенный вызов из самого STA-потока не зависает
    assert apps.run_sta(lambda: apps.run_sta(lambda: 7)) == 7


def test_run_sta_propagates_exception() -> None:
    def boom() -> None:
        raise ValueError("сломалось")

    with pytest.raises(ValueError, match="сломалось"):
        apps.run_sta(boom)
    assert apps.run_sta(lambda: "жив") == "жив"


def test_run_sta_timeout_starts_new_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apps, "STA_TIMEOUT_S", 0.2)
    release = threading.Event()
    try:
        with pytest.raises(TimeoutError):
            apps.run_sta(release.wait, 10)
        assert apps.run_sta(lambda: "новый поток") == "новый поток"
    finally:
        release.set()
