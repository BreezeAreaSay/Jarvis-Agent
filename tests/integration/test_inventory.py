"""Инвентарь (ADR 0030): приложения и известные папки компьютера — данные адаптера, а не ядра."""

import subprocess
from pathlib import Path

import pytest

from jarvis.adapters.inventory import PosixInventory, StaticInventory
from jarvis.adapters.inventory import posix as posix_module
from jarvis.adapters.inventory.names import aliases_for, slug, unique
from jarvis.domain.inventory import AppEntry, KnownFolder, name_key

DESKTOP = """[Desktop Entry]
Type=Application
Name={name}
Exec={exec}
{extra}
"""


def desktop(folder: Path, file: str, name: str, *, exec_: str = "app %U", extra: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file).write_text(DESKTOP.format(name=name, exec=exec_, extra=extra), encoding="utf-8")


def test_desktop_entries_become_apps_with_ids_and_aliases(tmp_path: Path) -> None:
    shared, local = tmp_path / "share", tmp_path / "home" / ".local" / "share" / "applications"
    desktop(shared, "org.telegram.desktop.desktop", "Telegram Desktop")
    desktop(shared, "code.desktop", "Visual Studio Code", extra="Name[ru]=Код")
    desktop(shared, "hidden.desktop", "Hidden", extra="NoDisplay=true")
    desktop(shared, "link.desktop", "Docs", extra="Type=Link")
    (shared / "broken.desktop").write_text("not an ini file", encoding="utf-8")
    desktop(local, "telegram-copy.desktop", "Telegram Desktop")
    inventory = PosixInventory(home=tmp_path / "home", application_dirs=[shared, local])

    found = {app.id: app for app in inventory.apps()}
    assert set(found) == {"telegram-desktop", "visual-studio-code", "telegram-desktop-2"}
    telegram = found["telegram-desktop"]
    assert telegram.kind == "desktop_entry"
    assert telegram.target == str(shared / "org.telegram.desktop.desktop")
    assert "телеграм" in telegram.aliases
    assert "Код" in found["visual-studio-code"].aliases


def test_known_folders_follow_user_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Загрузки").mkdir(parents=True)
    (home / "Documents").mkdir()
    (home / ".config").mkdir()
    (home / ".config" / "user-dirs.dirs").write_text(
        'XDG_DOWNLOAD_DIR="$HOME/Загрузки"\nXDG_MUSIC_DIR="$HOME/Музыка"\n', encoding="utf-8"
    )
    inventory = PosixInventory(home=home, application_dirs=[])
    assert inventory.known_folder(KnownFolder.HOME) == str(home)
    assert inventory.known_folder(KnownFolder.DOWNLOADS) == str(home / "Загрузки")
    assert inventory.known_folder(KnownFolder.DOCUMENTS) == str(home / "Documents")
    assert inventory.known_folder(KnownFolder.MUSIC) is None  # папки нет на диске
    assert inventory.known_folder(KnownFolder.VIDEOS) is None


def test_the_default_browser_comes_from_xdg_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = tmp_path / "share"
    desktop(shared, "firefox.desktop", "Firefox")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="firefox.desktop\n", stderr="")

    monkeypatch.setattr(posix_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(posix_module.subprocess, "run", run)
    inventory = PosixInventory(home=tmp_path, application_dirs=[shared])
    browser = inventory.default_browser()
    assert browser is not None
    assert browser.name == "Firefox"
    assert calls == [["/usr/bin/xdg-settings", "get", "default-web-browser"]]  # список аргументов, без shell


def test_without_xdg_settings_there_is_no_default_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(posix_module.shutil, "which", lambda name: None)
    assert PosixInventory(home=tmp_path, application_dirs=[]).default_browser() is None


def test_the_static_inventory_checks_its_default_browser() -> None:
    app = AppEntry(id="firefox", name="Firefox", aliases=[], target="/usr/bin/firefox", kind="executable")
    assert StaticInventory([app], default_browser="firefox").default_browser() == app
    with pytest.raises(ValueError, match="не найден"):
        StaticInventory([app], default_browser="chrome")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Telegram Desktop", "telegram-desktop"),
        ("Блокнот", "bloknot"),
        ("Python 3.12 (64-bit)", "python-3.12-64-bit"),
        ("™", "app"),
    ],
)
def test_ids_are_latin_slugs(name: str, expected: str) -> None:
    assert slug(name) == expected


def test_ids_are_unique() -> None:
    taken: set[str] = set()
    assert [unique("code", taken) for _ in range(3)] == ["code", "code-2", "code-3"]


def test_aliases_are_matched_by_the_whole_name() -> None:
    assert "хром" in aliases_for("Google Chrome®")
    assert aliases_for("Google Chrome Canary") == []


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Google Chrome™", "google chrome"),
        ("Ёлка", "елка"),
        ("Notepad++", "notepad++"),
        ("vs-code", "vs code"),
        ("«Telegram»", "telegram"),
    ],
)
def test_name_keys_ignore_case_marks_and_quotes(left: str, right: str) -> None:
    assert name_key(left) == name_key(right)


def test_a_backtick_is_not_a_quote() -> None:
    assert name_key("`calc`") != name_key("calc")
