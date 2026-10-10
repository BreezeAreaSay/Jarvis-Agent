"""pc.settings: каталог данных, путь конфига, чтение TOML с кэшем по mtime, секции [pc] и [aliases]."""

import os
import sys
from pathlib import Path

import pytest

from pc import settings


def _touch_later(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))


# --- каталог данных и конфиг --------------------------------------------------------------------


def test_data_dir_from_env(_isolated_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.data_dir() == _isolated_data
    monkeypatch.setenv("JARVIS_DATA_DIR", f"  {_isolated_data}  ")
    assert settings.data_dir() == _isolated_data


def test_data_dir_from_localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", "   ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert settings.data_dir() == tmp_path / "Jarvis"
    monkeypatch.delenv("JARVIS_DATA_DIR")
    assert settings.data_dir() == tmp_path / "Jarvis"


def test_data_dir_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_DATA_DIR")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert settings.data_dir() == Path.home() / "AppData" / "Local" / "Jarvis"


def test_data_file_creates_parent(_isolated_data: Path) -> None:
    path = settings.data_file("logs", "jarvis.log")
    assert path == _isolated_data / "logs" / "jarvis.log"
    assert path.parent.is_dir() and not path.exists()


def test_config_path_from_env(tmp_path: Path) -> None:
    assert settings.config_path() == tmp_path / "jarvis.toml"


def test_config_path_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG")
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert settings.config_path() == settings.DEV_CONFIG
    assert str(settings.DEV_CONFIG).replace("/", "\\") == r"C:\Jarvis\jarvis.toml"


def test_config_path_frozen_without_jarvis_dir(
    tmp_path: Path, _isolated_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JARVIS_CONFIG")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings, "DEV_CONFIG", tmp_path / "Jarvis" / "jarvis.toml")
    assert settings.is_frozen()
    assert settings.config_path() == _isolated_data / "jarvis.toml"
    (tmp_path / "Jarvis").mkdir()
    assert settings.config_path() == tmp_path / "Jarvis" / "jarvis.toml"


def test_app_root_and_install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not settings.is_frozen()
    assert settings.app_root() == settings.REPO_ROOT
    assert (settings.app_root() / "jarvis.example.toml").is_file()
    assert settings.install_dir() == settings.REPO_ROOT
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert settings.app_root() == tmp_path
    assert settings.install_dir() == Path(sys.executable).resolve().parent


# --- load_toml ----------------------------------------------------------------------------------


def test_load_toml_missing_file() -> None:
    assert settings.load_toml() == {}
    assert settings.config_error() == ""


def test_load_toml_cached_by_mtime(config_file) -> None:
    config_file('mode = "local"\n')
    first = settings.load_toml()
    assert first == {"mode": "local"}
    assert settings.load_toml() is first  # файл не менялся — тот же объект из кэша
    config_file('mode = "aaaaa"\n')
    assert settings.load_toml() == {"mode": "aaaaa"}
    config_file('mode = "bbbbb"\n')  # тот же размер, другой mtime
    assert settings.load_toml() == {"mode": "bbbbb"}


def test_load_toml_broken_file(config_file) -> None:
    path = config_file('[pc\nprivate_paths = ["D:\\x"]\n')
    assert settings.load_toml() == {}
    error = settings.config_error()
    assert error and str(path) in error
    assert settings.load_pc_settings() == settings.PcSettings()
    config_file('mode = "local"\n')
    assert settings.load_toml() == {"mode": "local"}
    assert settings.config_error() == ""


def test_load_toml_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.toml"
    path.write_bytes(b'\xef\xbb\xbfmode = "local"\n')
    _touch_later(path)
    assert settings.load_toml() == {"mode": "local"}


def test_load_toml_bad_encoding(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.toml"
    path.write_bytes(b'mode = "\xff\xfe"\n')
    _touch_later(path)
    assert settings.load_toml() == {}
    assert settings.config_error()


def test_load_toml_directory_instead_of_file(tmp_path: Path) -> None:
    (tmp_path / "jarvis.toml").mkdir()
    assert settings.load_toml() == {}
    assert settings.config_error()


def test_load_toml_follows_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other.toml"
    other.write_text('mode = "local"\n', encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG", str(other))
    assert settings.load_toml() == {"mode": "local"}


# --- [pc] и [aliases] ---------------------------------------------------------------------------


def test_pc_settings_defaults() -> None:
    assert settings.load_pc_settings() == settings.PcSettings("", (), {})


def test_pc_settings_values(config_file) -> None:
    config_file(
        "[pc]\n"
        "es_path = 'D:\\Tools\\es.exe'\n"
        "private_paths = ['D:\\Личное', 5, '   ', 'E:\\Проекты']\n"
        "[aliases]\n"
        '"Телега" = "Telegram"\n'
        '"ХРОМ" = "Google Chrome"\n'
        '"число" = 5\n'
    )
    pc = settings.load_pc_settings()
    assert pc.es_path == r"D:\Tools\es.exe"
    assert pc.private_paths == (r"D:\Личное", r"E:\Проекты")
    assert pc.aliases == {"телега": "Telegram", "хром": "Google Chrome"}


@pytest.mark.parametrize(
    "text",
    [
        "pc = 5\naliases = 'x'\n",
        "[pc]\nprivate_paths = 'D:\\\\x'\n",
        "[pc]\nprivate_paths = {a = 1}\n",
        "[aliases]\n",
    ],
)
def test_pc_settings_wrong_types(config_file, text: str) -> None:
    config_file(text)
    pc = settings.load_pc_settings()
    assert pc.private_paths == () and pc.aliases == {}


@pytest.mark.parametrize("value", ["5", "true", "['a']"])
def test_pc_settings_es_path_wrong_type(config_file, value: str) -> None:
    config_file(f"[pc]\nes_path = {value}\n")
    assert settings.load_pc_settings().es_path == ""


def test_es_path(tmp_path: Path, config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(settings, "install_dir", lambda: install)
    assert str(settings.es_path()).replace("/", "\\") == r"C:\Jarvis\bin\es.exe"
    bundled = install / "bin" / "es.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"")
    assert settings.es_path() == bundled
    config_file("[pc]\nes_path = 'D:\\Tools\\es.exe'\n")
    assert settings.es_path() == Path(r"D:\Tools\es.exe")
