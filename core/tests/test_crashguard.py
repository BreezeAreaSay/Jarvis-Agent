"""Защита от нативных вылетов при загрузке моделей и латинский путь для данных espeak-ng."""

import json
import os
import sys
from pathlib import Path

import pytest

from jarvis import crashguard
from jarvis.voice.engines import piper
from jarvis.voice.engines.base import TtsEngineError


def leftover(stage: str, pid: int = 1) -> None:
    """Метка, которую оставил бы процесс, умерший посреди загрузки."""
    marker = crashguard._marker(stage)
    marker.write_text(json.dumps({"stage": stage, "pid": pid, "ts": 0}), encoding="utf-8")


def test_two_crashes_in_a_row_block_the_stage_until_reset(data_dir: Path) -> None:
    crashguard._reload_for_tests()
    with crashguard.stage("tts-ruslan"):  # удачная загрузка метку убирает
        pass
    assert not list(crashguard._dir().glob("tts-*.json"))

    leftover("tts-ruslan")
    crashguard._reload_for_tests()  # «новый запуск»: метка от умершего процесса
    assert crashguard.crashed() == {"tts-ruslan": 1} and not crashguard.blocked("tts-ruslan")

    leftover("tts-ruslan")
    crashguard._reload_for_tests()
    assert crashguard.blocked("tts-ruslan")
    with pytest.raises(crashguard.StageBlocked), crashguard.stage("tts-ruslan"):
        raise AssertionError("заблокированный этап не выполняется")
    assert not crashguard.blocked("asr-gigaam-v3")  # другие этапы работают

    crashguard.reset()  # скачали модель заново — пробуем ещё раз
    assert not crashguard.blocked("tts-ruslan")
    with crashguard.stage("tts-ruslan"):
        pass
    assert crashguard.crashed() == {}


def test_exception_and_clean_exit_are_not_crashes(data_dir: Path) -> None:
    crashguard._reload_for_tests()
    with pytest.raises(ValueError), crashguard.stage("vad"):
        raise ValueError("модель битая")  # исключение — процесс жив, метки нет
    crashguard._reload_for_tests()
    assert crashguard.crashed() == {}

    # загрузку оборвал штатный выход ядра — метка этого процесса убирается
    leftover("asr-vosk-small", pid=os.getpid())
    crashguard.forget_process()
    crashguard._reload_for_tests()
    assert crashguard.crashed() == {}


def test_new_version_forgets_old_crashes(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crashguard._reload_for_tests()
    leftover("tts-ruslan")
    crashguard._reload_for_tests()
    assert crashguard.crashed() == {"tts-ruslan": 1}
    monkeypatch.setattr(crashguard, "__version__", "99.0.0")  # обновление могло всё исправить
    crashguard._reload_for_tests()
    assert crashguard.crashed() == {}


def test_espeak_data_gets_an_ascii_path_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "Сашка" / "models" / "tts-ruslan" / "espeak-ng-data"
    (src / "ru").mkdir(parents=True)
    for name, size in (("phontab", 10), ("phondata", 20), ("ru_dict", 30)):
        (src / name).write_bytes(b"x" * size)
    (src / "ru" / "voice").write_text("ru", encoding="utf-8")
    program_data = tmp_path / "ProgramData"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))

    first = piper.ascii_espeak_dir(src)
    assert str(first).isascii() and first.parent == program_data / "Jarvis"
    assert (first / "phondata").read_bytes() == b"x" * 20 and (first / "ru" / "voice").is_file()
    assert piper.ascii_espeak_dir(src) == first  # второй раз не копирует
    (first / "phontab").write_bytes(b"changed")
    assert (piper.ascii_espeak_dir(src) / "phontab").read_bytes() == b"changed"

    plain = tmp_path / "models" / "espeak-ng-data"
    plain.mkdir(parents=True)
    assert piper.ascii_espeak_dir(plain) == plain  # путь и так латиницей — как есть

    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "Данные"))
    monkeypatch.setenv("PUBLIC", str(tmp_path / "Общие"))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "Диск"))
    with pytest.raises(TtsEngineError, match="JARVIS_DATA_DIR"):
        piper.ascii_espeak_dir(src.with_name("другая"))


def test_espeak_path_is_untouched_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "Сашка" / "espeak-ng-data"
    src.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "linux")
    assert piper.ascii_espeak_dir(src) == src
