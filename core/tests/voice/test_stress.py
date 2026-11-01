"""Словарь ударений и «ё» Jarvis: ставится голосу только с теми же фонемными таблицами, исходный сохраняется и
возвращается (выключили, таблицы другие, голос со словарём ронял ядро); знак ударения — только голосу, который
его понимает; в ответе голосом знак уходит в речь, но не на экран."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from jarvis import crashguard
from jarvis.context import AppContext
from jarvis.voice import stress
from jarvis.voice.textprep import prepare
from jarvis.voice.tts import Tts
from tests.voice.helpers import FakeEngine, FakeSink, InstalledModels

ORIGINAL = b"espeak ru_dict " * 100
OURS = b"jarvis ru_dict " * 150
TABLES = {"phontab": b"tab", "phonindex": b"index", "phondata": b"data"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def voice(tmp_path: Path, data_dir: Path) -> Path:
    """espeak-ng-data голоса: фонемные таблицы и исходный русский словарь (метки crashguard — в data_dir)."""
    espeak = tmp_path / "tts-ruslan" / "espeak-ng-data"
    espeak.mkdir(parents=True)
    for name, data in TABLES.items():
        (espeak / name).write_bytes(data)
    (espeak / "ru_dict").write_bytes(ORIGINAL)
    return espeak


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Словарь Jarvis в составе программы (как кладёт сборка установщика)."""
    folder = tmp_path / "resources" / "voice"
    folder.mkdir(parents=True)
    (folder / "ru_dict").write_bytes(OURS)
    info = {
        "sha256": _sha(OURS),
        "size": len(OURS),
        "phoneme_tables": {name: _sha(data) for name, data in TABLES.items()},
    }
    (folder / "ru_dict.json").write_text(json.dumps(info), encoding="utf-8")
    return folder


def test_install_keeps_original_and_switches_back(voice: Path, bundle: Path) -> None:
    assert stress.install(voice, True, source=bundle) == "jarvis"
    assert (voice / "ru_dict").read_bytes() == OURS and (voice / "ru_dict.orig").read_bytes() == ORIGINAL
    assert stress.install(voice, True, source=bundle) == "jarvis"  # уже стоит — без копирования
    assert (voice / "ru_dict.orig").read_bytes() == ORIGINAL  # исходный не затёрт словарём Jarvis
    assert stress.install(voice, False, source=bundle) == "espeak"  # выключили — исходный обратно
    assert (voice / "ru_dict").read_bytes() == ORIGINAL
    assert not list(voice.glob("*.tmp"))


def test_install_refuses_other_phoneme_tables(voice: Path, bundle: Path) -> None:
    assert stress.install(voice, True, source=bundle) == "jarvis"
    (voice / "phondata").write_bytes(b"other data")  # голос скачали заново с другими таблицами
    assert stress.install(voice, True, source=bundle) == "espeak"
    assert (voice / "ru_dict").read_bytes() == ORIGINAL


def test_install_without_bundle_or_after_crash(voice: Path, bundle: Path, tmp_path: Path) -> None:
    assert stress.install(voice, True, source=tmp_path / "нет") == "espeak"
    assert (voice / "ru_dict").read_bytes() == ORIGINAL and not (voice / "ru_dict.orig").exists()
    (bundle / "ru_dict.json").write_text("{битый", encoding="utf-8")
    assert stress.bundled(bundle) is None
    assert stress.install(voice, True, source=bundle) == "espeak"


def test_crash_with_dictionary_turns_it_off(ctx: AppContext, voice: Path, bundle: Path) -> None:
    assert stress.install(voice, True, source=bundle) == "jarvis"
    # ядро упало, пока голос грузился со словарём: метка этапа осталась от «прошлого процесса»
    marker = crashguard._marker(stress.STAGE)
    marker.write_text(json.dumps({"stage": stress.STAGE, "pid": -1}), encoding="utf-8")
    crashguard._state = None
    assert crashguard.crashed().get(stress.STAGE) == 1
    assert stress.install(voice, True, source=bundle) == "espeak"  # дальше — с исходным словарём
    assert (voice / "ru_dict").read_bytes() == ORIGINAL
    crashguard.reset(stress.STAGE)
    assert stress.install(voice, True, source=bundle) == "jarvis"


def test_accents_only_for_voices_that_understand_them() -> None:
    assert stress.strip_accents("Это за́мок.") == "Это замок."
    assert prepare("Это за́мок.") == "Это за́мок."
    assert prepare("Это за́мок.", accents=False) == "Это замок."


class AccentEngine(FakeEngine):
    accents = True


async def test_tts_passes_accents_to_piper_with_dictionary_only(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    engines: dict[str, Any] = {"piper": AccentEngine("piper"), "edge": FakeEngine("edge")}
    tts = Tts(
        ctx,
        sink_factory=lambda: FakeSink(rate=16000, factor=0.05),
        engines=engines,
        models=InstalledModels(),  # type: ignore[arg-type]
        auto_download=False,
    )
    await tts.start()
    try:
        await tts.say("Это замо́к, сэр.", wait=True)
        assert engines["piper"].calls[-1][0] == "Это замо́к, сэр."
        ctx.settings.patch({"tts": {"engine": "edge"}})
        await tts.say("Это за́мок.", wait=True)
        assert engines["edge"].calls[-1][0] == "Это замок."  # другой голос знак не понимает — без него
        engines["edge"].ru = False
        await tts.say("Castle за́мок.", wait=True)
        assert engines["edge"].calls[-1][0] == "Castle замок."
    finally:
        await tts.stop()


def test_settings_default_and_status(ctx: AppContext) -> None:
    assert ctx.settings.value.tts.stress_dict is True
    status = ctx.tts.status()  # type: ignore[attr-defined]
    assert status["stress_dict"] == {"bundled": stress.bundled() is not None, "loaded": ""}
