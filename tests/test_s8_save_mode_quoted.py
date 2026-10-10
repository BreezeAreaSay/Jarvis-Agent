"""config.save_mode (S6a п.2): меняется только строка верхнего уровня mode — файл остаётся валидным TOML."""

import os
import tomllib
from pathlib import Path

import pytest

from jarvis import config


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "jarvis.toml"))
    return tmp_path / "jarvis.toml"


@pytest.mark.parametrize("key", ['"mode"', "'mode'"])
def test_quoted_mode_key_is_replaced_not_duplicated(_isolated: Path, key: str) -> None:
    _isolated.write_text(f'{key} = "normal"   # normal | local\n\n[ui]\nhotkey = "ctrl+alt+space"\n', "utf-8")
    config.save_mode("local")
    data = tomllib.loads(_isolated.read_text("utf-8"))  # сейчас: TOMLDecodeError — ключ mode дважды
    assert data["mode"] == "local"
    assert data["ui"]["hotkey"] == "ctrl+alt+space"
    os.utime(_isolated, ns=(1, 2))
    assert config.load().mode == "local"
