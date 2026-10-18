"""Подделки для тестов голоса: VAD по громкости, «распознавание» по сценарию, микрофон, динамики, движок.

Фраза сценария — блоки постоянной амплитуды 0,2 + 0,02·k (k — номер фразы): поддельные модели узнают по
амплитуде, какой текст «сказан». Так машина состояний проверяется без звука и моделей.
"""

import queue
import threading
import time
from typing import Any

import numpy as np

from jarvis.settings import VoiceSettings
from jarvis.voice.dsp import FRAME, RATE

BLOCK_S = FRAME / RATE


class FakeVad:
    """Речь — громкий блок; конец речи — end_frames тихих блоков подряд (как min_silence у Silero)."""

    def __init__(self, end_frames: int = 3) -> None:
        self.end_frames = end_frames
        self.speaking = False
        self.quiet = 0
        self.resets = 0

    def accept(self, frame: np.ndarray) -> bool:
        if float(np.max(np.abs(frame))) > 0.05:
            self.speaking, self.quiet = True, 0
        elif self.speaking:
            self.quiet += 1
            if self.quiet >= self.end_frames:
                self.speaking = False
        return self.speaking

    def reset(self) -> None:
        self.speaking, self.quiet = False, 0
        self.resets += 1


class Script:
    """Фразы сценария: utter(text) → блоки звука; text_for(звук) → что «сказано»."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.finals: list[tuple[str, bool]] = []  # (текст, просили ли пунктуацию)

    def utter(self, text: str, seconds: float = 1.0) -> list[np.ndarray]:
        k = len(self.texts)
        self.texts.append(text)
        amp = 0.2 + 0.02 * k
        return [np.full(FRAME, amp, dtype=np.float32) for _ in range(max(1, int(seconds / BLOCK_S)))]

    @staticmethod
    def silence(seconds: float = 0.5) -> list[np.ndarray]:
        return [np.zeros(FRAME, dtype=np.float32) for _ in range(max(1, int(seconds / BLOCK_S)))]

    def text_for(self, audio: np.ndarray) -> str:
        if not len(audio):
            return ""
        mag = np.abs(np.asarray(audio, dtype=np.float32))
        loud = mag[mag > 0.1]
        if not len(loud):
            return ""
        k = round((float(np.median(loud)) - 0.2) / 0.02)  # медиана: выбросы фильтров не сбивают
        return self.texts[k] if 0 <= k < len(self.texts) else ""

    def final(self, audio: np.ndarray, punct: bool) -> str:
        text = self.text_for(audio)
        self.finals.append((text, punct))
        return text

    def session(self) -> "FakeSession":
        return FakeSession(self)


class FakeSession:
    """Потоковая модель: каждые 3 блока «слышит» ещё одно слово фразы."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.peak = 0.0
        self.count = 0

    def feed(self, frame: np.ndarray) -> None:
        self.peak = max(self.peak, float(np.max(np.abs(frame))))
        if self.peak > 0.1:
            self.count += 1

    def _text(self) -> str:
        return self.script.text_for(np.array([self.peak], dtype=np.float32))

    def partial(self) -> str:
        words = self._text().split()
        return " ".join(words[: self.count // 3])

    def finish(self) -> str:
        return self._text()


class FakeHost:
    """Host слушателя: записывает события, команды, сигналы."""

    def __init__(self, **settings: Any) -> None:
        self.voice = VoiceSettings(**settings)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[str] = []
        self.earcons: list[str] = []
        self.activities: list[str] = []
        self.speaking = False
        self.silenced = 0

    def settings(self) -> VoiceSettings:
        return self.voice

    def publish(self, type: str, data: dict[str, Any]) -> None:
        self.events.append((type, data))

    def dispatch(self, command: str) -> None:
        self.commands.append(command)

    def tts_speaking(self) -> bool:
        return self.speaking

    def silence_tts(self) -> None:
        self.silenced += 1
        self.speaking = False

    def earcon(self, name: str) -> None:
        self.earcons.append(name)

    def activity(self, value: str) -> None:
        self.activities.append(value)

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [d for t, d in self.events if t == type_]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def feed(listener: Any, frames: list[np.ndarray], clock: Clock) -> None:
    for frame in frames:
        clock.t += BLOCK_S
        listener.process(frame)


class FakeMic:
    """Микрофон для сервиса: блоки из очереди; read() ждёт не дольше timeout."""

    def __init__(self, device: str = "") -> None:
        self.device = device
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.error = ""
        self.device_name = "Тестовый микрофон"
        self.closed = False

    def push(self, frames: list[np.ndarray]) -> None:
        for frame in frames:
            self.q.put(frame)

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        try:
            return self.q.get(timeout=min(timeout, 0.02))
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


class FakeRecognizers:
    """Recognizers для сервиса: итог и потоковая — по сценарию."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.unloaded: list[str | None] = []

    def pick_offline(self, preferred: str, *, punct: bool = False) -> Any:
        script = self.script

        class _Final:
            def recognize(self, audio: np.ndarray) -> str:
                return script.final(audio, punct)

        return _Final()

    def streaming(self) -> Any:
        script = self.script

        class _Stream:
            def session(self) -> FakeSession:
                return script.session()

            def recognize(self, audio: np.ndarray) -> str:
                return script.text_for(audio)

        return _Stream()

    def whisper(self) -> Any:
        return None

    def vad_model(self) -> None:
        return None

    def make_vad(self, sensitivity: float) -> FakeVad:
        return FakeVad()

    def loaded(self) -> list[str]:
        return []

    def unload(self, model_id: str | None = None) -> None:
        self.unloaded.append(model_id)


class InstalledModels:
    """Менеджер моделей, у которого всё «скачано» (или ничего)."""

    def __init__(self, installed: bool = True) -> None:
        from jarvis.voice.models import MODELS, ModelManager

        self.is_installed = installed
        self.specs = MODELS
        self.required_ids = ModelManager.required_ids
        self._listeners: list[Any] = []

    def installed(self, model_id: str) -> bool:
        return self.is_installed

    def missing(self, ids: list[str]) -> list[str]:
        return [] if self.is_installed else list(ids)

    def add_listener(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: None

    def catalog(self, required: list[str] | None = None) -> list[dict[str, Any]]:
        return []

    async def cancel_all(self) -> None:
        return None


class FakeSink:
    """Динамики: пишут блоки в список; realtime — спать столько, сколько звучит блок (× factor)."""

    def __init__(self, rate: int = 16000, factor: float = 0.0) -> None:
        self.rate = rate
        self.factor = factor
        self.blocks: list[np.ndarray] = []
        self.closed = 0
        self.aborted = 0
        self.lock = threading.Lock()

    def write(self, block: np.ndarray) -> None:
        with self.lock:
            self.blocks.append(np.array(block, copy=True))
        if self.factor:
            time.sleep(len(block) / self.rate * self.factor)

    def close(self, abort: bool = False) -> None:
        self.closed += 1
        self.aborted += int(abort)

    @property
    def samples(self) -> int:
        with self.lock:
            return sum(len(b) for b in self.blocks)


class FakeEngine:
    """Движок синтеза: тон 220 Гц, 0,3 с на фразу; fail — ошибка (как «нет сети»)."""

    def __init__(self, name: str = "piper", *, fail: str = "", rate: int = 16000, delay: float = 0.0) -> None:
        self.name = name
        self.title = f"Движок {name}"
        self.fail = fail
        self.rate = rate
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.ru = True

    def available(self) -> str:
        return ""

    def voice(self, settings: Any) -> str:
        return "v1"

    def russian(self, voice: str) -> bool:
        return self.ru

    async def synthesize(self, text: str, opts: Any) -> tuple[np.ndarray, int]:
        import asyncio

        from jarvis.voice.engines import TtsEngineError

        self.calls.append((text, opts.voice))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise TtsEngineError(self.fail)
        t = np.arange(int(0.3 * self.rate)) / self.rate
        return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), self.rate

    async def voices(self) -> list[dict[str, Any]]:
        return [{"id": "v1", "name": "Голос 1", "lang": "ru-RU", "gender": "male"}]

    def close(self) -> None:
        return None
