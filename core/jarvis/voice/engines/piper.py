"""Локальный голос Jarvis: piper (VITS) через sherpa-onnx OfflineTts — без сети, ≈0,1–0,15 с на фразу.

Голоса — модели piper-<имя> (ru_RU medium, fp32: int8-версии медленнее). Паузы — GenerationConfig
с silence_scale = 1,0: старый generate(text, sid, speed) в sherpa-onnx 1.13.8 берёт 0,2 и сжимает паузы
на запятых в 5 раз (замер jarvis-v1). Тон ниже (pitch < 1): синтез быстрее в 1/pitch раза, вывод на частоте
×pitch — длительность та же, голос ниже, без передискретизации.
Синтез — в своём потоке (один движок, по одной фразе).
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice import models as m
from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp

if TYPE_CHECKING:
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

SILENCE_SCALE = 1.0
WARMUP_TEXT = "Готов."
VOICES = {
    "ruslan": ("Руслан", "male"),
    "denis": ("Денис", "male"),
    "dmitri": ("Дмитрий", "male"),
    "irina": ("Ирина", "female"),
}


class PiperEngine:
    name = "piper"
    title = "Голос Jarvis (локально)"

    def __init__(self, models: m.ModelManager, threads: int = 2) -> None:
        self.models = models
        self.threads = threads
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="piper")
        self._lock = threading.Lock()
        self._tts: Any = None
        self._loaded = ""

    def available(self) -> str:
        return "" if self.pick("") else "голос piper не скачан"

    def voice(self, settings: "Settings") -> str:
        return settings.tts.piper_voice

    def russian(self, voice: str) -> bool:
        return True

    def installed(self, voice: str) -> bool:
        return self.models.installed(m.piper_id(voice))

    def pick(self, wanted: str) -> str:
        """Голос для синтеза: нужный, если скачан; иначе первый скачанный; нет — ""."""
        if wanted and self.installed(wanted):
            return wanted
        return next((v for v in m.PIPER_VOICES if self.installed(v)), "")

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        voice = self.pick(opts.voice)
        if not voice:
            raise TtsEngineError("голос piper не скачан — скачайте его в настройках голоса")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._synth, text, voice, opts.speed, opts.pitch)

    def preload(self, voice: str) -> None:
        """Загрузить голос и прогреть (секунда-две) — в фоне при старте, не на первой фразе."""
        picked = self.pick(voice)
        if picked:
            self._executor.submit(self._load, picked)

    def _load(self, voice: str) -> Any:
        with self._lock:
            if self._tts is not None and self._loaded == voice:
                return self._tts
            files = m.piper_files(self.models.path(m.piper_id(voice)))
            if files is None:
                raise TtsEngineError(f"голос {voice} неполный — скачайте его заново")
            try:
                import sherpa_onnx as so
            except Exception as e:
                raise TtsEngineError(f"sherpa-onnx недоступен: {e}") from e
            try:
                vits = so.OfflineTtsVitsModelConfig(
                    model=str(files["model"]), tokens=str(files["tokens"]), data_dir=str(files["espeak"])
                )
                cfg = so.OfflineTtsConfig(
                    model=so.OfflineTtsModelConfig(vits=vits, num_threads=self.threads), max_num_sentences=1
                )
                tts = so.OfflineTts(cfg)
                tts.generate(WARMUP_TEXT, _generation_config(so, 1.0))
            except Exception as e:
                raise TtsEngineError(f"голос {voice} не загрузился: {type(e).__name__}: {e}") from e
            self._tts, self._loaded = tts, voice
            log.info("голос: piper %s загружен", voice)
            return tts

    def _synth(self, text: str, voice: str, speed: float, pitch: float) -> tuple[np.ndarray, int]:
        tts = self._load(voice)
        import sherpa_onnx as so

        pitch = clamp(pitch, 0.8, 1.2)
        with self._lock:
            audio = tts.generate(text, _generation_config(so, clamp(speed, 0.5, 2.0) / pitch))
        samples = np.asarray(audio.samples, dtype=np.float32)
        rate = max(8000, round(int(audio.sample_rate) * pitch))
        return samples, rate

    async def voices(self) -> list[dict[str, Any]]:
        out = []
        for voice_id in m.PIPER_VOICES:
            title, gender = VOICES.get(voice_id, (voice_id, ""))
            out.append(
                {
                    "id": voice_id,
                    "name": title,
                    "lang": "ru-RU",
                    "gender": gender,
                    "installed": self.installed(voice_id),
                    "model_id": m.piper_id(voice_id),
                }
            )
        return out

    def unload(self) -> None:
        with self._lock:
            self._tts, self._loaded = None, ""

    def close(self) -> None:
        self.unload()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _generation_config(so: Any, speed: float) -> Any:
    config = so.GenerationConfig()
    config.sid = 0
    config.speed = speed
    config.silence_scale = SILENCE_SCALE
    return config
