"""Голоса Microsoft Edge (edge-tts): бесплатно, нужен интернет. MP3 → float32 через miniaudio.

Скорость и тон — параметры самого сервиса (rate «+10%», pitch «-8Hz»), без передискретизации.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp, gender_name

if TYPE_CHECKING:
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

DEFAULT_VOICE = "ru-RU-DmitryNeural"
TIMEOUT_S = 30.0
VOICES_TTL_S = 24 * 3600
FALLBACK_VOICES = [
    {"id": "ru-RU-DmitryNeural", "name": "Дмитрий (Edge)", "lang": "ru-RU", "gender": "male"},
    {"id": "ru-RU-SvetlanaNeural", "name": "Светлана (Edge)", "lang": "ru-RU", "gender": "female"},
    {"id": "en-US-AndrewMultilingualNeural", "name": "Andrew", "lang": "en-US", "gender": "male"},
    {"id": "en-US-BrianMultilingualNeural", "name": "Brian", "lang": "en-US", "gender": "male"},
    {"id": "en-GB-RyanNeural", "name": "Ryan (UK)", "lang": "en-GB", "gender": "male"},
]


def decode_mp3(data: bytes, rate: int = 24000) -> tuple[np.ndarray, int]:
    """MP3 → (float32 моно, частота) через miniaudio."""
    try:
        import miniaudio
    except Exception as e:
        raise TtsEngineError(f"декодер MP3 недоступен: {e}") from e
    try:
        decoded = miniaudio.decode(
            data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=rate
        )
    except Exception as e:
        raise TtsEngineError(f"звук не декодировался: {e}") from e
    samples = np.frombuffer(decoded.samples, dtype=np.float32).copy()
    return samples, int(decoded.sample_rate)


class EdgeEngine:
    name = "edge"
    title = "Голоса Microsoft Edge (интернет)"
    cacheable = True  # короткие фразы Tts помнит: повтор не ждёт сеть

    def __init__(self, communicate: Any = None, list_voices: Any = None) -> None:
        self._communicate = communicate  # подмена в тестах: (text, voice, rate=, pitch=) → объект со stream()
        self._list_voices = list_voices
        self._voices: list[dict[str, Any]] = []
        self._voices_at = 0.0

    def available(self) -> str:
        return ""

    def voice(self, settings: "Settings") -> str:
        return settings.tts.edge_voice or DEFAULT_VOICE

    def russian(self, voice: str) -> bool:
        return (voice or DEFAULT_VOICE).casefold().startswith("ru-")

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        voice = opts.voice or DEFAULT_VOICE
        rate = f"{round((clamp(opts.speed, 0.5, 2.0) - 1.0) * 100):+d}%"
        pitch = f"{round((clamp(opts.pitch, 0.8, 1.2) - 1.0) * 100):+d}Hz"
        try:
            factory = self._communicate
            if factory is None:
                import edge_tts

                factory = edge_tts.Communicate
            comm = factory(text, voice, rate=rate, pitch=pitch)
            data = bytearray()
            async with asyncio.timeout(TIMEOUT_S):
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        data.extend(chunk["data"])
        except TtsEngineError:
            raise
        except TimeoutError as e:
            raise TtsEngineError("Edge TTS не ответил вовремя — проверьте интернет") from e
        except ValueError as e:
            raise TtsEngineError(f"неверный голос Edge: {voice}") from e
        except Exception as e:
            raise TtsEngineError(f"Edge TTS недоступен: {e}") from e
        if not data:
            raise TtsEngineError("Edge TTS не вернул звук")
        return decode_mp3(bytes(data))

    async def voices(self) -> list[dict[str, Any]]:
        if self._voices and time.monotonic() - self._voices_at < VOICES_TTL_S:
            return self._voices
        try:
            lister = self._list_voices
            if lister is None:
                import edge_tts

                lister = edge_tts.list_voices
            async with asyncio.timeout(10):
                raw = await lister()
        except Exception as e:
            log.info("голос: список голосов Edge не получен (%s) — показываю основные", e)
            return list(FALLBACK_VOICES)
        items = []
        for v in raw:
            short = str(v.get("ShortName", ""))
            if not short:
                continue
            friendly = str(v.get("FriendlyName") or short)
            name = friendly.replace("Microsoft ", "").replace(" Online (Natural)", "")
            items.append(
                {
                    "id": short,
                    "name": name,
                    "lang": str(v.get("Locale", "")),
                    "gender": gender_name(v.get("Gender")),
                }
            )
        items.sort(key=lambda i: (not i["lang"].startswith("ru"), not i["lang"].startswith("en"), i["id"]))
        self._voices, self._voices_at = items, time.monotonic()
        return items

    def close(self) -> None:
        return None
