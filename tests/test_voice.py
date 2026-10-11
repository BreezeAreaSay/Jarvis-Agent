"""jarvis.voice: машина состояний push-to-talk на фейковых хуке, микрофоне и распознавателе."""

import sys
import threading
import time
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from jarvis import asr, voice
from jarvis.events import Done, TextChunk
from jarvis.mic import Recording


class FakeHook:
    def __init__(self, vk: int, events: Any, fail: bool = False) -> None:
        self.vk = vk
        self.events = events
        self.down = False
        self.physical = True  # GetAsyncKeyState
        self.started = self.stopped = False
        self.releases = 0
        self.fail = fail

    def start(self) -> None:
        if self.fail:
            raise RuntimeError("SetWindowsHookEx: отказано")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def key_down(self) -> bool:
        return self.physical

    def release(self) -> None:
        self.down = False
        self.releases += 1


class FakeRecorder:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.full = False
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        if self.fail:
            raise RuntimeError(self.fail)

    def stop(self) -> Recording:
        self.calls.append("stop")
        return Recording(b"\0\0" * 16000)

    def abort(self) -> None:
        self.calls.append("abort")


class FakeRecognizer:
    def __init__(self, text: str = "открой телегу", load_error: str = "") -> None:
        self.text = text
        self.load_error = load_error
        self.ready = False
        self.model = "gigaam-v3"
        self.threads = 2
        self.gate: threading.Event | None = None
        self.calls = 0

    def load(self) -> None:
        if self.load_error:
            raise asr.AsrError(self.load_error)
        self.ready = True

    def recognize(self, recording: Recording) -> asr.AsrResult:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        return asr.AsrResult(self.text, self.text, 123.4, recording.duration_s, 0.8, 5.0)


class Journal:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def cfg(**kw: Any) -> Any:
    base = {
        "ptt_key": "right ctrl",
        "min_hold_ms": 60,
        "max_record_s": 30.0,
        "device": "",
        "model": "gigaam-v3",
        "threads": 2,
        "vad": True,
    }
    return types.SimpleNamespace(**{**base, **kw})


class Rig:
    """Voice с фейками; events — что ушло в окно."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
        monkeypatch.setattr(voice.mic, "warmup", lambda: None)
        self.events: list[Any] = []
        self.cond = threading.Condition()
        self.presses = 0
        self.journal = Journal()
        self.recorder = kw.pop("recorder", FakeRecorder())
        self.recognizer = kw.pop("recognizer", FakeRecognizer())
        hook_fail = kw.pop("hook_fail", False)
        self.hook: FakeHook | None = None

        def factory(vk: int, events: Any) -> FakeHook:
            self.hook = FakeHook(vk, events, fail=hook_fail)
            return self.hook

        self.voice = voice.Voice(
            cfg(**kw),
            self.emit,
            on_press=self.on_press,
            journal=self.journal,
            recognizer=self.recognizer,
            recorder=self.recorder,
            hook_factory=factory,
        )
        self.voice.start()
        if self.voice.status != "error":
            self.wait(lambda: self.voice.status == "ready")

    def emit(self, ev: Any) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def on_press(self) -> None:
        self.presses += 1

    def wait(self, cond: Any, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        with self.cond:
            while not cond():
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"не дождался; события: {self.events}")
                self.cond.wait(min(left, 0.02))

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.events if not isinstance(e, voice.Loudness)]

    def key(self, kind: str) -> None:
        assert self.hook is not None
        if kind == "down":
            self.hook.down = True
        elif kind == "up":
            self.hook.down = False
        self.voice._q.put((kind, time.monotonic()))

    def hold(self) -> None:
        self.key("down")
        self.wait(lambda: "Listening" in self.kinds())

    def close(self) -> None:
        self.voice.stop()


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    made: list[Rig] = []

    def make(**kw: Any) -> Rig:
        r = Rig(monkeypatch, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def test_short_tap_is_ignored(rig: Any) -> None:
    r = rig(min_hold_ms=300)
    r.key("down")
    r.key("up")
    time.sleep(0.4)
    assert r.kinds() == []
    assert r.recorder.calls == ["start", "abort"]  # запись шла с нажатия и выброшена
    assert r.presses == 1


def test_hold_listen_recognize(rig: Any) -> None:
    r = rig()
    r.hold()
    assert r.recorder.calls == ["start"]
    r.voice._on_level(0.5)  # колбэк PortAudio
    r.key("up")
    r.wait(lambda: "Heard" in r.kinds())
    assert r.kinds() == ["Listening", "Recognizing", "Heard"]
    heard = r.events[-1]
    assert heard.text == "открой телегу" and heard.asr_ms == 123.4 and heard.model == "gigaam-v3"
    assert any(isinstance(e, voice.Loudness) and e.value == 0.5 for e in r.events)
    [rec] = r.journal.records
    assert rec["kind"] == "voice" and rec["text"] == "открой телегу" and rec["asr_ms"] == 123.4
    assert rec["audio_s"] == 1.0 and rec["empty"] is False and rec["release_to_text_ms"] >= 0
    assert r.voice.state == "idle"


def test_loudness_only_while_listening(rig: Any) -> None:
    r = rig(min_hold_ms=500)
    r.key("down")
    r.voice._on_level(0.7)  # окно ещё не показано — уровень не нужен
    r.key("up")
    time.sleep(0.05)
    assert not any(isinstance(e, voice.Loudness) for e in r.events)


def test_chord_before_hold_is_silent(rig: Any) -> None:
    r = rig(min_hold_ms=300)
    r.key("down")
    r.key("other")  # правый Ctrl+C
    time.sleep(0.4)
    assert r.kinds() == []
    assert r.recorder.calls == ["start", "abort"]


def test_chord_while_listening_cancels(rig: Any) -> None:
    r = rig()
    r.hold()
    r.key("other")
    r.wait(lambda: "ListenCancelled" in r.kinds())
    assert r.events[-1].reason == "keys"
    r.key("up")
    time.sleep(0.1)
    assert r.kinds() == ["Listening", "ListenCancelled"]


def test_esc_while_listening(rig: Any) -> None:
    r = rig()
    r.hold()
    r.voice.cancel()
    r.wait(lambda: "ListenCancelled" in r.kinds())
    assert r.events[-1].reason == "esc"
    assert r.recorder.calls[-1] == "abort"


def test_global_escape_silences_and_cancels(rig: Any) -> None:
    r = rig()
    escapes: list[int] = []
    r.voice.on_escape = lambda: escapes.append(1)
    r.key("esc")  # Jarvis говорит, окно не в фокусе — Esc в любом окне глушит речь
    time.sleep(0.05)
    assert escapes == [1] and r.kinds() == []
    r.hold()
    r.key("esc")
    r.wait(lambda: "ListenCancelled" in r.kinds())
    assert escapes == [1, 1] and r.events[-1].reason == "esc"


def test_esc_while_recognizing_drops_result(rig: Any) -> None:
    rec = FakeRecognizer()
    rec.gate = threading.Event()
    r = rig(recognizer=rec)
    r.hold()
    r.key("up")
    r.wait(lambda: "Recognizing" in r.kinds())
    r.voice.cancel()
    r.wait(lambda: "ListenCancelled" in r.kinds())
    rec.gate.set()
    time.sleep(0.1)
    assert "Heard" not in r.kinds()
    assert r.journal.records == []


def test_new_press_while_recognizing_wins(rig: Any) -> None:
    rec = FakeRecognizer()
    rec.gate = threading.Event()
    r = rig(recognizer=rec)
    r.hold()
    r.key("up")
    r.wait(lambda: "Recognizing" in r.kinds())
    r.key("down")  # новая фраза; старый результат не нужен
    r.wait(lambda: r.kinds().count("Listening") == 2)
    rec.gate.set()
    r.key("up")
    r.wait(lambda: "Heard" in r.kinds())
    assert r.kinds().count("Heard") == 1
    assert r.presses == 2


def test_nothing_heard(rig: Any) -> None:
    r = rig(recognizer=FakeRecognizer(text=""))
    r.hold()
    r.key("up")
    r.wait(lambda: "Failed" in r.kinds())
    assert "правый Ctrl" in r.events[-1].text
    assert r.journal.records[0]["empty"] is True


def test_mic_error_after_hold_only(rig: Any) -> None:
    r = rig(recorder=FakeRecorder(fail="устройство занято"), min_hold_ms=200)
    r.key("down")
    r.key("up")  # короткое нажатие — молча
    time.sleep(0.3)
    assert r.kinds() == []
    r.key("down")
    r.wait(lambda: "Failed" in r.kinds())
    assert "устройство занято" in r.events[-1].text
    r.key("up")
    time.sleep(0.1)
    assert r.kinds() == ["Failed"]


def test_model_error_reported_on_hold(rig: Any) -> None:
    r = rig(recognizer=FakeRecognizer(load_error="нет модели gigaam-v3: папки нет"))
    r.wait(lambda: r.voice.status == "error")
    r.key("down")
    r.wait(lambda: "Failed" in r.kinds())
    assert "нет модели" in r.events[-1].text
    assert "start" not in r.recorder.calls  # писать незачем


def test_lost_release_detected_by_polling(rig: Any) -> None:
    r = rig()
    r.hold()
    assert r.hook is not None
    r.hook.physical = False  # отпустили, а событие не пришло (хук снят Windows)
    r.wait(lambda: "Heard" in r.kinds())
    assert r.hook.releases >= 1


def test_stuck_hook_flag_released_when_idle(rig: Any) -> None:
    r = rig(min_hold_ms=1000)
    r.key("down")
    r.key("other")  # сочетание — бросили; клавиша ещё нажата
    assert r.hook is not None
    r.hook.physical = False  # отпустили там, где хук не видит
    deadline = time.monotonic() + 2
    while r.hook.down and time.monotonic() < deadline:
        time.sleep(0.02)
    assert r.hook.down is False


def test_max_length_stops_recording(rig: Any) -> None:
    r = rig()
    r.hold()
    r.recorder.full = True
    r.wait(lambda: "Heard" in r.kinds())
    assert "stop" in r.recorder.calls


def test_resume_cancels_listening(rig: Any) -> None:
    r = rig()
    r.hold()
    r.voice._q.put(("resume", time.monotonic()))
    r.wait(lambda: "ListenCancelled" in r.kinds())
    assert r.events[-1].reason == "resume"


def test_hook_failure_sets_error(rig: Any) -> None:
    r = rig(hook_fail=True)
    assert r.voice.status == "error"
    assert "клавиша голоса не работает" in r.voice.error
    time.sleep(0.05)
    assert r.voice.status == "error"  # загрузка модели не перетирает ошибку хука


def test_no_ptt_key_no_hook(rig: Any) -> None:
    r = rig(ptt_key="")
    assert r.hook is None and r.voice.key == ""


def test_stop_releases_hook(rig: Any) -> None:
    r = rig()
    r.voice.stop()
    assert r.hook is not None and r.hook.stopped
    assert r.voice._threads == []


def test_enabled(_isolated_data: Any) -> None:
    assert not voice.enabled(cfg(ptt_key=""))
    assert not voice.enabled(cfg(ptt_key="ctrl"))
    assert not voice.enabled(cfg())  # модели нет
    folder = asr.model_dir("gigaam-v3")
    folder.mkdir(parents=True)
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (folder / name).write_bytes(b"x")
    assert voice.enabled(cfg())


# --- [voice] в конфиге ---------------------------------------------------------------------------


def test_config_voice_defaults(config_file: Any) -> None:
    from jarvis import config

    config_file('mode = "normal"\n')
    v = config.load().voice
    assert v.ptt_key == "right ctrl" and v.min_hold_ms == 250 and v.max_record_s == 30.0
    assert v.model == "gigaam-v3" and v.tts == "voice" and v.tts_voice == "denis" and v.vad is True


def test_config_voice_values_and_fixes(config_file: Any) -> None:
    from jarvis import config

    config_file(
        "[voice]\n"
        'ptt_key = "F9"\nmin_hold_ms = 10\nmax_record_s = 500\ndevice = "USB"\nmodel = "whisper"\n'
        'threads = 4\ntts = "loud"\ntts_voice = "Денис"\ntts_speed = 1.2\nvad = false\n'
    )
    v = config.load().voice
    assert (
        v.ptt_key == "F9" and v.device == "USB" and v.threads == 4 and v.tts_speed == 1.2 and v.vad is False
    )
    # вне пределов и неизвестное — умолчания
    assert v.min_hold_ms == 250 and v.max_record_s == 30.0 and v.model == "gigaam-v3"
    assert v.tts == "voice" and v.tts_voice == "denis"


def test_config_voice_bad_key_and_tts_bool(config_file: Any) -> None:
    from jarvis import config

    config_file('[voice]\nptt_key = "ctrl"\ntts = false\nmodel = "vosk-small"\n')
    v = config.load().voice
    assert v.ptt_key == "right ctrl" and v.tts == "off" and v.model == "vosk-small"
    config_file('[voice]\nptt_key = ""\ntts = true\n')
    v = config.load().voice
    assert v.ptt_key == "" and v.tts == "voice"


# --- журнал и stats ------------------------------------------------------------------------------


def test_stats_voice_and_tts_records() -> None:
    from jarvis import journal

    records = [
        {"ts": "2026-10-11T10:00:00", "level": "grammar", "total": 20.0, "grammar": 5.0, "ok": True},
        {"kind": "voice", "asr_ms": 200.0, "audio_s": 2.0, "empty": False},
        {"kind": "voice", "asr_ms": 300.0, "audio_s": 4.0, "empty": False},
        {"kind": "voice", "asr_ms": 900.0, "audio_s": 12.0, "empty": True},
        {"kind": "tts", "tts_ms": 150.0, "interrupted": False},
        {"kind": "tts", "tts_ms": 250.0, "interrupted": True},
    ]
    st = journal.stats(1, records)
    assert st["count"] == 1  # голосовые записи — не запросы
    assert st["levels"]["grammar"]["count"] == 1
    assert st["voice"]["count"] == 3 and st["voice"]["asr_p50"] == 300.0 and st["voice"]["asr_p95"] == 900.0
    assert st["voice"]["audio_s_avg"] == 6.0 and st["voice"]["empty"] == pytest.approx(1 / 3)
    assert st["tts"]["tts_p95"] == 250.0 and st["tts"]["interrupted"] == 0.5
    budgets = {b["name"]: b for b in st["budgets"]}
    assert (
        budgets["голос: распознавание"]["ok"] is False and budgets["голос: распознавание"]["limit"] == 400.0
    )
    assert budgets["озвучка: до первого звука"]["ok"] is True
    text = journal.format_stats(st)
    assert "Голос (3): распознавание p50 300 мс, p95 900 мс" in text
    assert "запись в среднем 6,0 с; не расслышал 33 %" in text
    assert "Озвучка (2): до первого звука p50 150 мс, p95 250 мс; перебито 50 %" in text
    assert "⚠ голос: распознавание ≤ 400 мс: 900 мс (n=3)" in text


def test_stats_without_voice_has_no_voice_lines() -> None:
    from jarvis import journal

    st = journal.stats(1, [{"ts": "2026-10-11T10:00:00", "level": "hands", "total": 500.0}])
    text = journal.format_stats(st)
    assert "Голос (" not in text and "Озвучка (" not in text
    assert "· голос: распознавание ≤ 400 мс: нет данных" in text


def test_journal_voice_text_dropped_without_store_text(config_file: Any, _isolated_data: Any) -> None:
    from jarvis import journal

    config_file("[journal]\nstore_text = false\n")
    j = journal.Journal()
    j.write({"kind": "voice", "asr_ms": 210.0, "text": "открой телегу", "ts": "2026-10-11T10:00:00+03:00"})
    j.close()
    [rec] = journal.read(1, today=date(2026, 10, 11))
    assert rec["kind"] == "voice" and rec["asr_ms"] == 210.0 and "text" not in rec


# --- doctor --------------------------------------------------------------------------------------


def doc(**voice_kw: Any) -> Any:
    from jarvis import config, doctor

    return doctor.Doctor(cfg=config.Config(voice=config.VoiceConfig(**voice_kw)))


def test_doctor_voice_ptt() -> None:
    from jarvis import doctor

    assert doctor.check_voice_ptt(doc(ptt_key="")).status == "skip"
    res = doctor.check_voice_ptt(doc())
    assert res.status == "ok" and "правый Ctrl" in res.text and "администратора" in res.text


def test_doctor_voice_asr_missing_model(_isolated_data: Any) -> None:
    from jarvis import doctor

    res = doctor.check_voice_asr(doc())
    assert res.status == "warn" and "get_voice_models" in res.hint
    assert doctor.check_voice_asr(doc(ptt_key="")).status == "skip"


def test_doctor_voice_asr_loads_and_times(monkeypatch: pytest.MonkeyPatch, _isolated_data: Any) -> None:
    from jarvis import doctor
    from test_asr import FakeSherpa, make_model

    fake = FakeSherpa()
    fake.__version__ = "1.13.8"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    make_model("gigaam-v3")
    res = doctor.check_voice_asr(doc())
    assert res.status == "warn" and "VAD нет" in res.text  # VAD включён, а файла нет
    asr.vad_path().parent.mkdir(parents=True)
    asr.vad_path().write_bytes(b"x")
    res = doctor.check_voice_asr(doc())
    assert res.status == "ok" and "sherpa-onnx 1.13.8" in res.text and "3 с звука" in res.text

    clock = iter(range(0, 10_000, 1))
    monkeypatch.setattr(doctor.time, "perf_counter", lambda: next(clock) * 0.5)
    res = doctor.check_voice_asr(doc())
    assert res.status == "warn" and "threads = 4" in res.hint


def test_doctor_voice_mic(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import doctor, mic
    from test_mic import FakeSd

    monkeypatch.setattr(mic, "_api", FakeSd())
    res = doctor.check_voice_mic(doc())
    assert res.status == "ok" and "Realtek" in res.text and "устройств ввода: 4" in res.text
    assert doctor.check_voice_mic(doc(device="USB Headset")).status == "ok"
    res = doctor.check_voice_mic(doc(device="нет такого"))
    assert res.status == "warn" and "не найден" in res.text

    def broken() -> Any:
        raise mic.MicError("звук недоступен: PortAudio library not found")

    monkeypatch.setattr(mic, "api", broken)
    assert doctor.check_voice_mic(doc()).status == "fail"


def test_doctor_voice_tts(monkeypatch: pytest.MonkeyPatch, _isolated_data: Any) -> None:
    from jarvis import doctor, tts

    assert doctor.check_voice_tts(doc(tts="off")).status == "skip"
    res = doctor.check_voice_tts(doc())
    assert res.status == "warn" and "скачаны: нет" in res.text

    folder = tts.voice_dir("denis")
    (folder / "espeak-ng-data").mkdir(parents=True)
    (folder / "ru_RU-denis-medium.onnx").write_bytes(b"x")
    (folder / "tokens.txt").write_text("a 0")

    class Engine:
        sample_rate = 22050

        def __init__(self, cfg: Any) -> None:
            self.cfg = cfg

        def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any:
            return types.SimpleNamespace(samples=[0.0] * 100, sample_rate=22050)

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineTtsVitsModelConfig = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    fake.OfflineTtsModelConfig = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    fake.OfflineTtsConfig = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    fake.OfflineTts = Engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    res = doctor.check_voice_tts(doc())
    assert res.status == "ok" and "голос denis" in res.text


# --- упаковка и скрипт моделей -------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_spec_bundles_voice_natives() -> None:
    spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    assert 'voice_binaries = collect_dynamic_libs("sherpa_onnx")' in spec  # onnxruntime и sherpa-onnx-c-api
    assert "binaries=cb_binaries + voice_binaries" in spec
    for name in ('"sherpa_onnx.lib._sherpa_onnx"', '"sounddevice"', '"_sounddevice_data"', '"_cffi_backend"'):
        assert name in spec, name
    assert '(os.path.join(ROOT, "scripts", "get_voice_models.ps1"), "scripts")' in spec
    assert ".onnx" not in spec.replace("onnxruntime", "")  # модели в бандл не кладём


def test_selftest_checks_voice() -> None:
    from jarvis import selftest

    assert ("голос: sherpa-onnx и звук", selftest.check_voice) in selftest.CHECKS
    for name in ("jarvis.ptt", "jarvis.mic", "jarvis.asr", "jarvis.voice", "jarvis.tts"):
        assert name in selftest.REQUIRED_MODULES


def test_selftest_voice_check_runs(tmp_path: Any) -> None:
    from jarvis import selftest

    text = selftest.check_voice(selftest.Ctx(tmp_path, offscreen=True))
    assert text.startswith("sherpa-onnx 1.13.8")


def test_get_voice_models_script() -> None:
    """Скрипт моделей: без кириллицы в коде, только pwsh 7, известные модели и голоса, проверка SHA-256."""
    path = ROOT / "scripts" / "get_voice_models.ps1"
    text = path.read_text(encoding="utf-8")
    assert text.isascii()
    assert text.startswith("#Requires -Version 7.0")
    from jarvis import asr

    for name in asr.MODELS:
        assert f"'{name}'" in text, name
    assert "'denis'" in text and "Get-FileHash -Algorithm SHA256" in text
    assert "System32\\tar.exe" in text  # bsdtar Windows, не GNU tar из Git
    assert "JARVIS_DATA_DIR" in text and "LOCALAPPDATA" in text


def test_voice_example_block_in_agents() -> None:
    """Секция [voice] примера слово в слово есть в блоке конфига AGENTS.md и разбирается в умолчания."""
    import tomllib

    from jarvis import config

    example = (ROOT / "jarvis.example.toml").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    block = example[example.index("[voice]") :].split("\n\n")[0].splitlines()
    assert len(block) == 11
    for line in block:
        assert line in agents, line
    assert config._voice(tomllib.loads(example)["voice"]) == config.VoiceConfig()


# --- голосовой запрос: подсказка мозгу и source в журнале -----------------------------------------


class SpokenBrain:
    def __init__(self) -> None:
        self.spoken: list[bool] = []

    def ask(self, text: str, ctx: Any, deep: bool = False, spoken: bool = False) -> Any:
        self.spoken.append(spoken)
        yield TextChunk("Потому что рассеяние света.")
        yield Done(ok=True, text="")

    def cancel(self) -> None:
        pass


def test_core_voice_source_reaches_brain_and_journal() -> None:
    from jarvis import config
    from jarvis.context import Context
    from jarvis.core import Core

    brain, journal = SpokenBrain(), Journal()
    core = Core(config.Config(), hands=None, brain=brain, journal=journal)
    events = list(core.handle("почему небо голубое", Context(), source="voice"))
    assert isinstance(events[-1], Done) and events[-1].ok
    assert brain.spoken == [True]
    assert journal.records[-1]["source"] == "voice"
    list(core.handle("почему небо голубое", Context()))
    assert brain.spoken == [True, False]
    assert "source" not in journal.records[-1]  # набранный — запись как раньше


def test_build_prompt_spoken_hint() -> None:
    from jarvis import brain

    spoken = brain.build_prompt("почему небо голубое", None, spoken=True)
    assert spoken.startswith(brain.SPOKEN_HINT) and spoken.endswith("почему небо голубое")
    assert "без списков, таблиц и кода" in brain.SPOKEN_HINT
    assert brain.build_prompt("почему небо голубое", None) == "почему небо голубое"


def test_press_during_speech_waits_for_echo_tail(rig: Any) -> None:
    r = rig()
    r.voice.on_press = lambda: True  # Narrator.cancel: речь звучала и прервана
    t0 = time.monotonic()
    r.hold()
    assert time.monotonic() - t0 >= voice.ECHO_GAP_S
    assert r.recorder.calls == ["start"]
