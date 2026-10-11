"""jarvis.mic: запись в память, уровень, выбор устройства — на фейковом sounddevice."""

import array
import math
import sys
from collections.abc import Callable
from typing import Any

import pytest

from jarvis import mic


class FakeStream:
    def __init__(self, callback: Callable[..., None] | None = None) -> None:
        self.callback = callback
        self.stopped = self.closed = self.aborted = False
        self.written: list[bytes] = []

    def feed(self, block: bytes, status: int = 0) -> None:
        assert self.callback is not None
        self.callback(memoryview(block), len(block) // 2, None, status)

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))


DEVICES = [
    {"name": "Микрофон (Realtek Audio)", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0},
    {"name": "Динамики (Realtek Audio)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0},
    {"name": "Микрофон (USB Headset)", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 1},
    {"name": "Микрофон (USB Headset)", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0},
    {
        "name": "Микрофон (WASAPI only)",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "hostapi": 2,
        "default_samplerate": 48000.0,
    },
]


class FakeSd:
    def __init__(self, fail_rates: tuple[int, ...] = (), fail_all: bool = False) -> None:
        self.fail_rates = fail_rates
        self.fail_all = fail_all
        self.inputs: list[tuple[int | None, int, int, FakeStream]] = []

    def devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in DEVICES]

    def default_device(self, kind: str) -> int | None:
        return 0 if kind == "input" else 1

    def open_input(
        self, device: int | None, rate: int, block: int, callback: Callable[..., None]
    ) -> FakeStream:
        if self.fail_all or rate in self.fail_rates:
            raise RuntimeError(f"Invalid sample rate {rate}")
        stream = FakeStream(callback)
        self.inputs.append((device, rate, block, stream))
        return stream


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> FakeSd:
    fake = FakeSd()
    monkeypatch.setattr(mic, "_api", fake)
    return fake


def tone(n: int, amplitude: int) -> bytes:
    return array.array("h", [amplitude if i % 2 else -amplitude for i in range(n)]).tobytes()


def test_level_scale() -> None:
    assert mic.level(b"") == 0.0
    assert mic.level(bytes(1600)) == 0.0
    assert mic.level(tone(800, 32767)) == 1.0
    amp = int(32768 * 10 ** (-36 / 20))  # −36 дБ — середина шкалы −60…−12
    assert math.isclose(mic.level(tone(800, amp)), 0.5, abs_tol=0.02)
    assert mic.level(tone(800, 10)) == 0.0  # −70 дБ — тишина
    assert mic.level(tone(800, 1000) + b"\x01") > 0  # нечётный хвост не мешает


def test_recording_samples_and_duration() -> None:
    rec = mic.Recording(array.array("h", [0, 16384, -32768, 32767]).tobytes())
    assert rec.frames == 4
    assert rec.duration_s == 4 / 16000
    assert rec.samples() == [0.0, 0.5, -1.0, 32767 / 32768]


def test_recorder_collects_blocks_and_levels(sd: FakeSd) -> None:
    levels: list[float] = []
    rec = mic.Recorder(on_level=levels.append)
    rec.start()
    device, rate, block, stream = sd.inputs[0]
    assert (device, rate, block) == (None, 16000, 800)  # 50 мс блоки — 20 уровней в секунду
    stream.feed(tone(800, 1000))
    stream.feed(tone(800, 2000))
    assert rec.active and math.isclose(rec.seconds, 0.1)
    result = rec.stop()
    assert stream.stopped and stream.closed and not rec.active
    assert result.pcm == tone(800, 1000) + tone(800, 2000)
    assert result.rate == 16000
    assert len(levels) == 2 and levels[1] > levels[0] > 0


def test_recorder_abort_discards(sd: FakeSd) -> None:
    rec = mic.Recorder()
    rec.start()
    sd.inputs[0][3].feed(tone(800, 1000))
    rec.abort()
    assert not rec.active
    rec.start()  # после abort — снова можно
    assert rec.stop().pcm == b""


def test_recorder_limit(sd: FakeSd) -> None:
    rec = mic.Recorder(max_s=0.08)  # 1280 отсчётов
    rec.start()
    stream = sd.inputs[0][3]
    stream.feed(tone(800, 1000))
    assert not rec.full
    stream.feed(tone(800, 1000))
    assert rec.full
    stream.feed(tone(800, 1000))  # сверх предела — не копится
    assert rec.stop().frames == 1280


def test_recorder_callback_never_raises(sd: FakeSd) -> None:
    def bad_level(value: float) -> None:
        raise RuntimeError("сигнал Qt упал")

    rec = mic.Recorder(on_level=bad_level)
    rec.start()
    sd.inputs[0][3].feed(tone(800, 1000), status=1)
    assert rec.overflows == 1
    assert rec.stop().frames == 800


def test_recorder_double_start_is_error(sd: FakeSd) -> None:
    rec = mic.Recorder()
    rec.start()
    with pytest.raises(mic.MicError, match="уже идёт"):
        rec.start()
    rec.abort()


def test_recorder_open_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic, "_api", FakeSd(fail_all=True))
    with pytest.raises(mic.MicError, match="микрофон не открылся"):
        mic.Recorder().start()


def test_recorder_falls_back_to_device_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSd(fail_rates=(16000,))
    monkeypatch.setattr(mic, "_api", fake)
    rec = mic.Recorder(device="WASAPI only")
    rec.start()
    device, rate, block, _ = fake.inputs[0]
    assert (device, rate, block) == (4, 48000, 2400)
    assert rec.stop().rate == 48000


def test_find_device(sd: FakeSd) -> None:
    assert mic.find_device("") is None
    assert mic.find_device("realtek") == 0
    assert mic.find_device("usb headset") == 3  # сначала API устройства по умолчанию
    assert mic.find_device("динамики") is None  # это вывод, а не вход
    assert mic.find_device("динамики", "output") == 1
    assert mic.find_device("нет такого") is None


def test_device_name(sd: FakeSd) -> None:
    assert mic.device_name(None) == "Микрофон (Realtek Audio)"
    assert mic.device_name(None, "output") == "Динамики (Realtek Audio)"
    assert mic.device_name(99) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows PortAudio в колесе sounddevice")
def test_api_without_portaudio_is_mic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    if importlib.util.find_spec("sounddevice") is None:
        pytest.skip("sounddevice не установлен")
    try:
        import sounddevice  # noqa: F401
    except OSError:
        pass
    else:
        pytest.skip("PortAudio есть в системе")
    monkeypatch.setattr(mic, "_api", None)
    with pytest.raises(mic.MicError, match="звук недоступен"):
        mic.api()
    mic.warmup()  # не бросает
