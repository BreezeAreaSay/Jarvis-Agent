"""exe без C:\\Jarvis: конфиг — <data>\\jarvis.toml (README, NIGHT §4). start_hands.cmd из бандла при первом
запуске рук делает `mkdir C:\\Jarvis\\logs` — после этого config_path() молча переключается на
C:\\Jarvis\\jarvis.toml (его нет) и настройки владельца (в том числе mode = "local") пропадают."""

import os
import sys
from pathlib import Path

import pytest

from jarvis import config
from pc import settings


@pytest.fixture
def frozen_without_dev_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dev = tmp_path / "C_Jarvis"  # «C:\Jarvis» — пока нет
    monkeypatch.setattr(settings, "DEV_CONFIG", dev / "jarvis.toml")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    return dev


def test_config_path_does_not_flip_after_hands_log_dir_created(frozen_without_dev_dir: Path) -> None:
    user_cfg = settings.data_dir() / "jarvis.toml"
    assert settings.config_path() == user_cfg
    user_cfg.write_text('mode = "local"\n[ui]\nhotkey = "ctrl+alt+j"\n', encoding="utf-8")
    assert config.load().mode == "local"

    (frozen_without_dev_dir / "logs").mkdir(parents=True)  # start_hands.cmd: mkdir C:\Jarvis\logs
    os.utime(user_cfg, ns=(1, 2))

    assert settings.config_path() == user_cfg, "конфиг переехал в C:\\Jarvis\\jarvis.toml"
    assert config.load().mode == "local"


def test_private_paths_survive_hands_start(frozen_without_dev_dir: Path) -> None:
    """Следствие для приватности: private_paths из <data>\\jarvis.toml перестают скрываться от GPT."""
    from pc import paths

    user_cfg = settings.data_dir() / "jarvis.toml"
    user_cfg.write_text("[pc]\nprivate_paths = ['C:\\\\Users\\\\me\\\\Тайное']\n", encoding="utf-8")
    secret = "C:\\Users\\me\\Тайное\\план.docx"
    assert not paths.check(secret, "brain", "read", is_dir=False).ok

    (frozen_without_dev_dir / "logs").mkdir(parents=True)  # start_hands.cmd: mkdir C:\Jarvis\logs
    os.utime(user_cfg, ns=(1, 2))

    assert not paths.check(secret, "brain", "read", is_dir=False).ok, "закрытая папка открылась для GPT"
