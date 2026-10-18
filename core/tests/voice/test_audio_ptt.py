"""Микрофон без звуковой карты (подмена слоя sounddevice) и клавиша «держи и говори»."""

import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from jarvis.voice import audio
from jarvis.voice.audio import AudioError, MicInput, PreRoll, find_device, list_devices
from jarvis.voice.dsp import FRAME
from jarvis.voice.ptt import PttHook, key_label, key_matches, parse_key


def dev(name: str, hostapi: int, inputs: int, outputs: int, rate: int = 48000) -> dict[str, Any]:
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": rate,
    }


DEVICES = [
    dev("Microsoft Sound Mapper - Input", 0, 2, 0, 44100),
    dev("Микрофон (USB Audio Device With", 0, 1, 0),  # MME обрезает имя до 31 символа
    dev("Динамики (Realtek(R) Audio)", 0, 0, 2),
    dev("Микрофон (USB Audio Device With Long Name)", 1, 1, 0),
    dev("Динамики (Realtek(R) Audio)", 1, 0, 2),
]


class FakeStream:
    def __init__(
        self, owner: "FakeApi", rate: int, callback: Callable[..., None], finished: Callable[[], None]
    ):
        self.owner = owner
        self.rate = rate
        self.callback = callback
        self.finished = finished
        self.closed = False

    def emit(self, seconds: float) -> None:
        n = int(self.rate * seconds)
        t = np.arange(n) / self.rate
        block = (0.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32).reshape(-1, 1)
        self.callback(block, n, None, None)

    def close(self, abort: bool = False) -> None:
        self.closed = True


class FakeApi:
    """Слой SoundDevice: устройство не умеет 16 кГц (как WASAPI), открытия можно ломать."""

    def __init__(self) -> None:
        self.broken = False
        self.opened: list[tuple[int | None, int, int]] = []
        self.streams: list[FakeStream] = []
        self.refreshed = 0
        self.lock = threading.RLock()

    def devices(self) -> list[dict[str, Any]]:
        return [dict(d, index=i) for i, d in enumerate(DEVICES)]

    def hostapis(self) -> list[dict[str, Any]]:
        return [
            {"name": "MME", "devices": [0, 1, 2], "default_input_device": 0, "default_output_device": 2},
            {
                "name": "Windows WASAPI",
                "devices": [3, 4],
                "default_input_device": 3,
                "default_output_device": 4,
            },
        ]

    def default_hostapi(self) -> int:
        return 0

    def default_index(self, kind: str) -> int | None:
        return 0 if kind == "input" else 2

    def refresh(self) -> bool:
        self.refreshed += 1
        return True

    def open_input(
        self, index: int | None, rate: int, channels: int, blocksize: int, callback: Any, finished: Any
    ):
        self.opened.append((index, rate, channels))
        if self.broken:
            raise OSError("Device unavailable")
        if rate == 16000:
            raise OSError("Invalid sample rate")
        stream = FakeStream(self, rate, callback, finished)
        self.streams.append(stream)
        return stream


def test_find_device_handles_truncated_mme_names() -> None:
    api: Any = FakeApi()
    assert find_device(api, "", "input") is None
    assert find_device(api, "USB Audio Device With Long Name", "input") == 1  # MME (по умолчанию) — первым
    assert find_device(api, "usb audio", "input") == 1
    assert find_device(api, "Realtek", "output") == 2
    assert find_device(api, "нет такого", "input") is None


def test_list_devices_prefers_full_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio.sys, "platform", "win32")
    out = list_devices(FakeApi())  # type: ignore[arg-type]
    assert out["inputs"] == [
        {
            "id": "Микрофон (USB Audio Device With Long Name)",
            "name": "Микрофон (USB Audio Device With Long Name)",
            "default": True,
        }
    ]
    assert out["outputs"][0]["name"] == "Динамики (Realtek(R) Audio)" and out["outputs"][0]["default"]


def test_mic_resamples_native_rate_to_16k_frames() -> None:
    api = FakeApi()
    mic = MicInput("USB", api_factory=lambda: api)  # type: ignore[arg-type,return-value]
    assert mic.read(0.01) is None  # открылся, звука ещё нет
    assert api.opened[:2] == [(1, 16000, 1), (1, 48000, 1)]  # 16 кГц не умеет — родная частота
    assert mic.rate == 48000 and mic.error == ""
    api.streams[0].emit(0.5)
    frames = []
    while (frame := mic.read(0.01)) is not None:
        frames.append(frame)
    assert all(len(f) == FRAME and f.dtype == np.float32 for f in frames)
    assert 14 <= len(frames) <= 16  # 0,5 с по 32 мс
    spec = np.abs(np.fft.rfft(np.concatenate(frames)))
    freq = np.fft.rfftfreq(len(frames) * FRAME, 1 / 16000)[int(np.argmax(spec))]
    assert abs(freq - 300) < 20
    mic.close()
    assert api.streams[0].closed


def test_mic_reconnects_after_stall_and_errors() -> None:
    api = FakeApi()
    api.broken = True
    now = [100.0]
    mic = MicInput("", api_factory=lambda: api, stall_s=1.0, clock=lambda: now[0])  # type: ignore[arg-type,return-value]
    assert mic.read(0.0) is None and "не открылся" in mic.error
    tries = len(api.opened)
    now[0] += 0.5
    assert mic.read(0.0) is None and len(api.opened) == tries  # пауза перед повтором — 1 с
    now[0] += 0.6
    assert mic.read(0.0) is None and len(api.opened) > tries and mic.error  # повтор — снова ошибка
    tries = len(api.opened)
    now[0] += 1.5
    mic.read(0.0)
    assert len(api.opened) == tries  # пауза выросла до 2 с
    api.broken = False
    now[0] += 0.6
    mic.read(0.0)
    assert mic.active and mic.error == ""
    assert api.refreshed >= 1  # список устройств перечитан перед повтором
    now[0] += 2.0  # колбэки перестали приходить — устройство пропало
    assert mic.read(0.0) is None
    assert not mic.active and "переподключаюсь" in mic.error and mic.restarts == 1
    api.streams[-1].finished()  # поздний сигнал старого потока не мешает новому
    now[0] += 2.0
    mic.read(0.0)
    assert mic.active and mic.error == ""
    api.streams[-1].finished()  # поток сам закончился (устройство выдернули)
    assert mic.read(0.0) is None and not mic.active and "отключился" in mic.error


def test_mic_no_portaudio() -> None:
    def broken() -> Any:
        raise AudioError("звук недоступен: PortAudio library not found")

    mic = MicInput("", api_factory=broken)
    assert mic.read(0.0) is None and "PortAudio" in mic.error


def test_preroll_ring() -> None:
    ring = PreRoll(seconds=0.1)  # 4 блока по 32 мс
    for i in range(10):
        ring.push(np.full(FRAME, i, dtype=np.float32))
    taken = ring.take()
    assert [int(f[0]) for f in taken] == [6, 7, 8, 9] and ring.take() == []


@pytest.mark.parametrize(
    ("name", "spec"),
    [
        ("right ctrl", "ctrl_r"),
        ("RightCtrl", "ctrl_r"),
        ("правый ctrl", "ctrl_r"),
        ("rctrl", "ctrl_r"),
        ("left alt", "alt_l"),
        ("alt gr", "alt_gr"),
        ("F9", "f9"),
        ("caps lock", "caps_lock"),
        ("Scroll Lock", "scroll_lock"),
        ("`", "char:`"),
    ],
)
def test_parse_key(name: str, spec: str) -> None:
    assert parse_key(name) == spec


def test_parse_key_errors_and_labels() -> None:
    with pytest.raises(ValueError, match="сторону"):
        parse_key("ctrl")
    with pytest.raises(ValueError, match="неизвестная"):
        parse_key("super duper")
    assert key_label("ctrl_r") == "правый Ctrl" and key_label("f9") == "F9" and key_label("char:q") == "Q"


class Key:
    def __init__(self, name: str | None = None, char: str | None = None) -> None:
        self.name = name
        self.char = char


def test_ptt_hook_press_release_autorepeat_and_combo() -> None:
    log: list[str] = []
    hook = PttHook(
        "right ctrl", lambda: log.append("down"), lambda: log.append("up"), lambda: log.append("other")
    )
    hook.press(Key("ctrl_r"))
    hook.press(Key("ctrl_r"))  # автоповтор
    hook.press(Key(char="c"))  # правый Ctrl+C — сочетание
    hook.release(Key("ctrl_r"))
    hook.press(Key(char="c"))  # без PTT — не наше дело
    assert log == ["down", "other", "up"]
    assert key_matches("alt_r", Key("alt_gr")) and key_matches("char:x", Key(char="X"))
    assert not key_matches("ctrl_r", Key("ctrl_l"))
