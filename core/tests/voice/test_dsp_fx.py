"""Числа звука: передискретизация, громкость, WAV/PCM, «электроника» в голосе, сигналы."""

import numpy as np
import pytest

from jarvis.voice import dsp, fx


def tone(freq: float, rate: int, seconds: float = 1.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_freq(x: np.ndarray, rate: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(np.argmax(spec))])


@pytest.mark.parametrize("src", [48000, 44100, 22050, 8000])
def test_resample_keeps_length_and_pitch(src: int) -> None:
    x = tone(440, src)
    y = dsp.resample(x, src, 16000)
    assert len(y) == 16000
    assert abs(peak_freq(y, 16000) - 440) < 3
    assert y.dtype == np.float32


def test_resample_downsampling_filters_aliasing() -> None:
    x = tone(12000, 48000)  # выше 8 кГц — в 16 кГц не помещается, должен исчезнуть, а не «завернуться»
    y = dsp.resample(x, 48000, 16000)
    assert dsp.rms(y[200:-200]) < 0.05 * dsp.rms(x)


def test_streaming_resampler_matches_whole() -> None:
    x = tone(300, 44100, 0.5)
    r = dsp.Resampler(44100, 16000)
    parts = [r.process(x[i : i + 1000]) for i in range(0, len(x), 1000)]
    streamed = np.concatenate(parts)
    whole = dsp.Resampler(44100, 16000).process(x)
    n = min(len(streamed), len(whole))
    assert n > 7000
    assert np.max(np.abs(streamed[:n] - whole[:n])) < 1e-5  # блоки стыкуются без щелчков


def test_as_mono_variants() -> None:
    stereo = np.stack([np.ones(10), -np.ones(10)], axis=1).astype(np.float32)
    assert np.allclose(dsp.as_mono(stereo), 0.0)
    pcm = np.array([16384, -16384], dtype=np.int16)
    assert np.allclose(dsp.as_mono(pcm), [0.5, -0.5])
    assert dsp.as_mono([0.1, 0.2]).dtype == np.float32


def test_level_mapping() -> None:
    assert dsp.level(np.zeros(512, dtype=np.float32)) == 0.0
    quiet = dsp.level(tone(200, 16000, 0.032, amp=0.001))
    speech = dsp.level(tone(200, 16000, 0.032, amp=0.05))
    loud = dsp.level(tone(200, 16000, 0.032, amp=0.9))
    assert 0.0 <= quiet < speech < loud <= 1.0
    assert loud == 1.0


def test_level_meter_throttles_and_smooths() -> None:
    meter = dsp.LevelMeter(rate_hz=15.0)
    out = [meter.feed(0.8, t * 0.032) for t in range(31)]  # ≈1 с блоков по 32 мс
    values = [v for v in out if v is not None]
    assert 13 <= len(values) <= 17
    assert values[0] < values[-1] <= 0.8  # быстрый, но плавный подъём
    silent = [meter.feed(0.0, 1.0 + t * 0.07) for t in range(80)]
    zeros = [v for v in silent if v == 0.0]
    assert len(zeros) == 1  # тишина: ноль отправлен один раз


def test_wav_roundtrip_and_pcm() -> None:
    x = tone(440, 22050, 0.2)
    wav = dsp.encode_wav(x, 22050)
    y, rate = dsp.decode_wav(wav)
    assert rate == 22050 and len(y) == len(x)
    assert np.max(np.abs(y - x)) < 1e-3
    assert np.allclose(dsp.pcm16_to_float(dsp.float_to_pcm16(x)), x, atol=1e-3)
    with pytest.raises(ValueError):
        dsp.decode_wav(b"not a wav")


def test_electronic_keeps_shape_without_clipping() -> None:
    rate = 22050
    t = np.arange(rate) / rate
    voice = (0.9 * np.sin(2 * np.pi * 150 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    for amount in (0.35, 1.0):
        y = fx.electronic(voice, rate, amount)
        assert len(y) == len(voice) and y.dtype == np.float32
        assert np.max(np.abs(y)) <= fx.PEAK + 1e-6  # без перегруза
        corr = float(np.corrcoef(voice, y)[0, 1])
        assert corr > 0.5  # форма речи узнаётся
    assert np.array_equal(fx.electronic(voice, rate, 0.0), voice)
    y = fx.electronic(voice, rate, 0.35)
    assert not np.array_equal(y, voice)
    assert fx.electronic(np.zeros(0, dtype=np.float32), rate, 0.5).size == 0


def test_electronic_cuts_low_rumble() -> None:
    rate = 22050
    rumble = tone(25, rate, 1.0, amp=0.5)
    y = fx.electronic(rumble, rate, 1.0)
    assert dsp.rms(y) < 0.5 * dsp.rms(rumble)


def test_earcons() -> None:
    for name in fx.EARCONS:
        x = fx.earcon(name, 24000)
        assert 0.1 < len(x) / 24000 < 0.4  # короткий
        assert 0.1 < np.max(np.abs(x)) <= 0.25  # тихий и без перегруза
        assert abs(x[0]) < 1e-3 and abs(x[-1]) < 1e-3  # без щелчков по краям
    wake = fx.earcon("wake")
    half = len(wake) // 2
    assert peak_freq(wake[half:], 24000) > peak_freq(wake[:half], 24000)  # «слушаю» — вверх
    with pytest.raises(KeyError):
        fx.earcon("nope")


def test_apply_volume_and_fade() -> None:
    x = np.ones(1000, dtype=np.float32)
    assert np.allclose(fx.apply_volume(x, 0.5), 0.5)
    faded = fx.fade(x, 1000, ms=10)
    assert faded[0] == 0.0 and faded[500] == 1.0 and faded[-1] == 0.0
