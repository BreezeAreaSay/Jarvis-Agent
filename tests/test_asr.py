"""jarvis.asr: файлы моделей, нормализация текста, обрезка тишины, распознаватель на фейковом sherpa-onnx."""

import array
import inspect
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jarvis import asr, config
from jarvis.mic import Recording


def make_model(name: str, files: tuple[str, ...] | None = None) -> Path:
    folder = asr.model_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    for f in files or ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (folder / f).write_bytes(b"x")
    return folder


def test_models_match_config() -> None:
    assert set(asr.MODELS) == set(config.VOICE_MODELS)
    assert config.VoiceConfig().model == asr.DEFAULT_MODEL


def test_model_files_missing_dir_hint() -> None:
    with pytest.raises(asr.AsrError, match=r"get_voice_models\.ps1 -Asr gigaam-v3"):
        asr.model_files("gigaam-v3")


def test_model_files_unknown() -> None:
    with pytest.raises(asr.AsrError, match="неизвестная модель"):
        asr.model_files("whisper")


def test_model_files_gigaam() -> None:
    folder = make_model("gigaam-v3")
    files = asr.model_files("gigaam-v3")
    assert files["encoder"] == folder / "encoder.int8.onnx"
    assert files["decoder"] == folder / "decoder.onnx"
    assert files["tokens"] == folder / "tokens.txt"


def test_model_files_prefers_int8_and_vosk_names() -> None:
    folder = make_model(
        "vosk-small",
        ("encoder.onnx", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt", "bpe.model"),
    )
    files = asr.model_files("vosk-small")
    assert files["encoder"] == folder / "encoder.int8.onnx"
    assert files["joiner"] == folder / "joiner.int8.onnx"


def test_model_files_missing_part() -> None:
    make_model("gigaam-v2", ("encoder.int8.onnx", "decoder.onnx", "tokens.txt"))
    with pytest.raises(asr.AsrError, match="нет joiner"):
        asr.model_files("gigaam-v2")
    make_model("gigaam-v3-punct", ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx"))
    with pytest.raises(asr.AsrError, match=r"нет tokens\.txt"):
        asr.model_files("gigaam-v3-punct")


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("открой телегу", "открой телегу"),
        ("  открой   телегу  ", "открой телегу"),
        ("Джарвис, открой телегу.", "открой телегу"),
        ("джарвис открой телегу", "открой телегу"),
        ("Jarvis — громкость 30%.", "громкость 30%"),
        ("Громкость 30%.", "громкость 30%"),
        ("Что ты умеешь?", "что ты умеешь?"),
        ("GPT, расскажи анекдот!", "GPT, расскажи анекдот"),
        ("Telegram открой", "telegram открой"),
        ("Джарвис", ""),
        ("", ""),
        ("Я", "я"),
        ("Джарис, открой телегу", "открой телегу"),
        ("джор виш что ты умеешь", "что ты умеешь"),
        ("жарвис громче", "громче"),
        ("шарвис что ты умеешь", "что ты умеешь"),
        ("шарф найди", "шарф найди"),
        ("джаз включи", "джаз включи"),
        ("джинсы найди", "джинсы найди"),
        ("дарвин открой", "дарвин открой"),
        ("жарко сегодня", "жарко сегодня"),
    ],
)
def test_normalize(raw: str, text: str) -> None:
    assert asr.normalize(raw) == text


def test_resample() -> None:
    assert asr.resample([0.1, 0.2], 16000) == [0.1, 0.2]
    out = asr.resample([float(i) for i in range(48)], 48000)
    assert len(out) == 16
    assert out[:3] == [0.0, 3.0, 6.0]
    assert asr.resample([], 48000) == []
    up = asr.resample([0.0, 1.0], 8000)
    assert len(up) == 4 and up[-1] == 1.0


# --- фейковый sherpa-onnx ------------------------------------------------------------------------


class FakeStream:
    def __init__(self, text: str) -> None:
        self.text = text
        self.waves: list[tuple[int, int]] = []
        self.finished = False
        self.result = types.SimpleNamespace(text=text)

    def accept_waveform(self, rate: int, samples: list[float]) -> None:
        self.waves.append((rate, len(samples)))

    def input_finished(self) -> None:
        self.finished = True


class FakeOffline:
    created: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, text: str) -> None:
        self.text = text
        self.streams: list[FakeStream] = []

    @classmethod
    def from_transducer(cls, **kw: Any) -> "FakeOffline":
        cls.created.append(kw)
        return cls(FakeSherpa.reply)

    def create_stream(self) -> FakeStream:
        stream = FakeStream(self.text)
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: FakeStream) -> None:
        pass


class FakeOnline(FakeOffline):
    created: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.steps = 0

    def is_ready(self, stream: FakeStream) -> bool:
        self.steps += 1
        return self.steps < 3

    def get_result(self, stream: FakeStream) -> str:
        assert stream.finished
        return stream.text


class FakeVad:
    """Речь — отсчёты с |x| > 0.1; сегменты отдаются после flush()."""

    def __init__(self, cfg: Any, buffer_size_in_seconds: float) -> None:
        self.cfg = cfg
        self.samples: list[float] = []
        self.segments: list[Any] = []
        self.calls = 0

    def reset(self) -> None:
        self.samples, self.segments = [], []

    def accept_waveform(self, samples: list[float]) -> None:
        assert len(samples) <= asr.VAD_WINDOW  # окнами, а не всем буфером
        self.calls += 1
        self.samples += samples

    def flush(self) -> None:
        idx = [i for i, x in enumerate(self.samples) if abs(x) > 0.1]
        if idx:
            start, end = idx[0], idx[-1] + 1
            self.segments = [types.SimpleNamespace(start=start, samples=[0.0] * (end - start))]

    def empty(self) -> bool:
        return not self.segments

    @property
    def front(self) -> Any:
        return self.segments[0]

    def pop(self) -> None:
        self.segments.pop(0)


class FakeSherpa(types.ModuleType):
    reply = "Джарвис, открой телегу."

    def __init__(self) -> None:
        super().__init__("sherpa_onnx")
        self.OfflineRecognizer = FakeOffline
        self.OnlineRecognizer = FakeOnline
        self.VoiceActivityDetector = FakeVad
        self.SileroVadModelConfig = lambda **kw: types.SimpleNamespace(**kw)
        self.VadModelConfig = lambda **kw: types.SimpleNamespace(**kw)


@pytest.fixture
def sherpa(monkeypatch: pytest.MonkeyPatch) -> FakeSherpa:
    fake = FakeSherpa()
    FakeOffline.created, FakeOnline.created = [], []
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


def pcm(seconds_silence: float, seconds_speech: float, tail: float) -> Recording:
    rate = 16000
    data = [0] * int(seconds_silence * rate) + [8000] * int(seconds_speech * rate) + [0] * int(tail * rate)
    return Recording(array.array("h", data).tobytes())


def test_recognizer_load_offline_and_recognize(sherpa: FakeSherpa) -> None:
    make_model("gigaam-v3")
    (asr.vad_path().parent).mkdir(parents=True)
    asr.vad_path().write_bytes(b"x")
    rec = asr.Recognizer("gigaam-v3", threads=4)
    rec.load()
    kw = FakeOffline.created[0]
    assert kw["model_type"] == "nemo_transducer" and kw["feature_dim"] == 64
    assert kw["num_threads"] == 4 and kw["sample_rate"] == 16000
    assert kw["decoding_method"] == "greedy_search"
    assert rec.ready and rec.load_ms >= 0
    assert rec._rec.streams[0].waves == [(16000, 16000)]  # прогрев секундой тишины

    result = rec.recognize(pcm(1.0, 2.0, 1.0))
    assert result.text == "открой телегу"
    assert result.raw == "Джарвис, открой телегу."
    assert result.audio_s == 4.0
    # речь 1,0–3,0 с плюс запас 0,25 до и 0,35 после
    assert result.speech_s == pytest.approx(2.6, abs=0.001)
    stream = rec._rec.streams[-1]
    assert stream.waves == [(16000, int(2.6 * 16000))]
    assert rec._vad.calls > 1


def test_recognizer_silence_skips_decoding(sherpa: FakeSherpa) -> None:
    make_model("gigaam-v3")
    asr.vad_path().parent.mkdir(parents=True)
    asr.vad_path().write_bytes(b"x")
    rec = asr.Recognizer("gigaam-v3")
    rec.load()
    decoded = len(rec._rec.streams)
    result = rec.recognize(pcm(1.0, 0.0, 0.0))
    assert result.text == "" and result.speech_s == 0.0
    assert len(rec._rec.streams) == decoded


def test_recognizer_without_vad_file_uses_whole_audio(sherpa: FakeSherpa) -> None:
    make_model("gigaam-v3")
    rec = asr.Recognizer("gigaam-v3")
    rec.load()
    assert rec._vad is None
    result = rec.recognize(pcm(0.5, 1.0, 0.5))
    assert result.speech_s == 2.0


def test_recognizer_online_model(sherpa: FakeSherpa) -> None:
    make_model("vosk-small", ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt"))
    FakeSherpa.reply = "громкость тридцать"
    try:
        rec = asr.Recognizer("vosk-small", vad=False)
        rec.load()
        kw = FakeOnline.created[0]
        assert kw["feature_dim"] == 80 and "model_type" not in kw
        result = rec.recognize(pcm(0.0, 1.0, 0.0))
        assert result.text == "громкость тридцать"
        waves = rec._rec.streams[-1].waves
        assert waves[-1] == (16000, int(asr.TAIL_PAD_S * 16000))  # хвост тишины для потоковой модели
    finally:
        FakeSherpa.reply = "Джарвис, открой телегу."


def test_recognizer_resamples_other_rate(sherpa: FakeSherpa) -> None:
    make_model("gigaam-v3")
    rec = asr.Recognizer("gigaam-v3", vad=False)
    rec.load()
    data = array.array("h", [1000] * 48000).tobytes()
    result = rec.recognize(Recording(data, 48000))
    assert result.audio_s == 1.0
    assert rec._rec.streams[-1].waves == [(16000, 16000)]


def test_recognizer_not_loaded() -> None:
    rec = asr.Recognizer("gigaam-v3")
    with pytest.raises(asr.AsrError, match="грузится"):
        rec.recognize(pcm(0.0, 1.0, 0.0))


def test_recognizer_load_missing_model(sherpa: FakeSherpa) -> None:
    rec = asr.Recognizer("gigaam-v3")
    with pytest.raises(asr.AsrError):
        rec.load()
    assert not rec.ready and "get_voice_models" in rec.error


def test_recognizer_load_native_error(sherpa: FakeSherpa, monkeypatch: pytest.MonkeyPatch) -> None:
    make_model("gigaam-v3")

    def boom(**kw: Any) -> None:
        raise RuntimeError("'vocab_size' does not exist in the metadata")

    monkeypatch.setattr(FakeOffline, "from_transducer", staticmethod(boom))
    rec = asr.Recognizer("gigaam-v3")
    with pytest.raises(asr.AsrError, match="vocab_size"):
        rec.load()


def test_unknown_model_falls_back() -> None:
    assert asr.Recognizer("whisper").model == asr.DEFAULT_MODEL


# --- контракт настоящего sherpa-onnx (без моделей) ------------------------------------------------


def test_real_sherpa_api_contract() -> None:
    """Имена и параметры, на которые опираются asr и tts, есть в установленном sherpa-onnx."""
    so = pytest.importorskip("sherpa_onnx")
    params = inspect.signature(so.OfflineRecognizer.from_transducer).parameters
    for name in ("encoder", "decoder", "joiner", "tokens", "num_threads", "feature_dim", "model_type"):
        assert name in params
    assert "feature_dim" in inspect.signature(so.OnlineRecognizer.from_transducer).parameters
    silero = so.SileroVadModelConfig()
    for name in ("model", "threshold", "min_silence_duration", "min_speech_duration", "window_size"):
        assert hasattr(silero, name)
    for name in ("accept_waveform", "flush", "empty", "front", "pop", "reset"):
        assert hasattr(so.VoiceActivityDetector, name)
    for name in ("OfflineTts", "OfflineTtsConfig", "OfflineTtsModelConfig", "OfflineTtsVitsModelConfig"):
        assert hasattr(so, name)
