"""Распознавание речи на CPU (sherpa-onnx — лениво): Silero VAD, Vosk small (потоковая), GigaAM v3, Whisper.

- SileroVad — детектор речи для микрофона: блоки по 512 отсчётов (весь буфер одним вызовом sherpa-onnx 1.13.8
  разбирает неверно — проверено в jarvis-v1); чувствительность из настроек → порог и минимальная длина речи.
- StreamingAsr — Vosk small (zipformer, 80 признаков): текст по ходу речи и раннее «Джарвис».
- OfflineAsr — GigaAM v3 (nemo_transducer, 64 признака, greedy_search): итоговый текст фразы, ≈0,3 с на 3 с.
- WhisperAsr — многоязычная модель для transcribe(language != "ru"); куски ≤ 20 с (Whisper берёт до 30 с).
- Recognizers — все модели ядра: грузит по требованию (секунды — не на горячем пути), держит в памяти, каждая
  модель под своим замком (декодер не делят два потока).
"""

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from jarvis import crashguard
from jarvis import text as textops
from jarvis.voice import models as m
from jarvis.voice.dsp import FRAME, RATE

log = logging.getLogger(__name__)

THREADS = 2  # потоков onnxruntime на модель: на i5-12400 больше — не быстрее (замер jarvis-v1)
TAIL_PAD_S = 0.5  # потоковой модели — тишина в конце, чтобы она выдала последние слова
SEGMENT_PAD_S = 0.25  # запас вокруг куска речи при разбиении длинной записи
MAX_SEGMENT_S = 20.0


class AsrError(RuntimeError):
    """Модель не загрузилась; текст — для человека."""


@contextlib.contextmanager
def _guarded(stage: str, title: str) -> Iterator[None]:
    """Нативная загрузка модели: если на ней ядро уже падало — понятная ошибка вместо нового вылета."""
    try:
        with crashguard.stage(stage):
            yield
    except crashguard.StageBlocked:
        raise AsrError(
            f"{title} аварийно останавливала ядро — пропускаю её. Скачайте модель заново в настройках голоса"
        ) from None


def _so() -> Any:
    try:
        import sherpa_onnx
    except Exception as e:  # ImportError, OSError (нет библиотеки)
        raise AsrError(f"sherpa-onnx недоступен: {e}") from e
    return sherpa_onnx


def normalize_result(raw: str, *, keep_case: bool = False) -> str:
    """Текст модели → строка: одиночные пробелы; без пунктуационной модели — строчными."""
    t = textops.clean(" ".join(str(raw).split()))
    return t if keep_case else t.lower()


# --- детектор речи ----------------------------------------------------------------------------------


class Vad(Protocol):
    def accept(self, frame: np.ndarray) -> bool:
        """Блок FRAME отсчётов → идёт ли сейчас речь."""
        ...

    def reset(self) -> None: ...


END_SILENCE_S = 0.45  # пауза после речи, по которой фраза закончилась (voice.end_silence_ms)
PAUSE_SILENCE_S = 0.18  # VAD пауз: столько тишины — «речь стихла», итог фразы начинаем заранее


def vad_params(sensitivity: float, end_silence: float = END_SILENCE_S) -> dict[str, float]:
    """Чувствительность 0..1 → порог Silero и минимальная длина речи (выше — ловит тише и короче); пауза в
    конце фразы — из настроек (0,3–2 с: кто говорит с паузами, увеличивает её)."""
    s = max(0.0, min(1.0, float(sensitivity)))
    return {
        "threshold": round(0.65 - 0.3 * s, 3),  # 0 → 0,65; 0,5 → 0,5; 1 → 0,35
        "min_speech": round(0.35 - 0.2 * s, 3),  # 0 → 0,35 с; 0,5 → 0,25 с; 1 → 0,15 с
        "min_silence": round(max(0.3, min(2.0, float(end_silence))), 3),
    }


class SileroVad:
    def __init__(
        self,
        model: Path,
        *,
        threshold: float = 0.5,
        min_speech: float = 0.25,
        min_silence: float = 0.45,
        max_speech: float = 120.0,
    ) -> None:
        so = _so()
        self.min_silence = min_silence  # столько тишины после речи — «речь кончилась»
        silero = so.SileroVadModelConfig(
            model=str(model),
            threshold=threshold,
            min_silence_duration=min_silence,
            min_speech_duration=min_speech,
            window_size=FRAME,
            max_speech_duration=max_speech,
        )
        cfg = so.VadModelConfig(silero_vad=silero, sample_rate=RATE, num_threads=1)
        with _guarded("vad", "Модель детектора речи"):
            self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=max_speech + 10)

    def accept(self, frame: np.ndarray) -> bool:
        self._vad.accept_waveform(np.asarray(frame, dtype=np.float32))
        while not self._vad.empty():  # отрезки речи нам не нужны — границы ведёт слушатель
            self._vad.pop()
        return bool(self._vad.is_speech_detected())

    def reset(self) -> None:
        self._vad.reset()


def speech_segments(
    samples: np.ndarray, vad_model: Path | None, *, max_segment_s: float = MAX_SEGMENT_S
) -> list[tuple[int, int]]:
    """Где в записи речь: [(начало, конец)] в отсчётах 16 кГц с запасом по краям.

    Нет VAD — куски по max_segment_s подряд. Пустой список — речи нет.
    """
    n = len(samples)
    if n == 0:
        return []
    if vad_model is None:
        step = int(max_segment_s * RATE)
        return [(i, min(n, i + step)) for i in range(0, n, step)]
    so = _so()
    silero = so.SileroVadModelConfig(
        model=str(vad_model),
        threshold=0.5,
        min_silence_duration=0.3,
        min_speech_duration=0.2,
        window_size=FRAME,
        max_speech_duration=max_segment_s,
    )
    cfg = so.VadModelConfig(silero_vad=silero, sample_rate=RATE, num_threads=1)
    with _guarded("vad", "Модель детектора речи"):
        vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(60.0, n / RATE + 5))
    raw: list[tuple[int, int]] = []

    def drain() -> None:
        while not vad.empty():
            seg = vad.front
            start = int(seg.start)
            raw.append((start, start + len(seg.samples)))
            vad.pop()

    x = np.asarray(samples, dtype=np.float32)
    for i in range(0, n, FRAME):  # окнами по 512: целиком sherpa-onnx 1.13.8 режет неверно
        vad.accept_waveform(x[i : i + FRAME])
        drain()
    vad.flush()
    drain()
    pad = int(SEGMENT_PAD_S * RATE)
    out: list[tuple[int, int]] = []
    limit = int((max_segment_s + 2 * SEGMENT_PAD_S) * RATE)
    for a, b in raw:
        a, b = max(0, a - pad), min(n, b + pad)
        if out and a <= out[-1][1] and b - out[-1][0] <= limit:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# --- модели -----------------------------------------------------------------------------------------


class StreamSession:
    """Одна фраза потоковой модели: feed() по блокам, partial() — текст сейчас, finish() — итог."""

    def __init__(self, owner: "StreamingAsr") -> None:
        self.owner = owner
        self.stream = owner.rec.create_stream()
        self.finished = False

    def feed(self, frame: np.ndarray) -> None:
        rec = self.owner.rec
        self.stream.accept_waveform(RATE, np.asarray(frame, dtype=np.float32))
        while rec.is_ready(self.stream):
            rec.decode_stream(self.stream)

    def partial(self) -> str:
        return normalize_result(self.owner.rec.get_result(self.stream))

    def finish(self) -> str:
        if not self.finished:
            self.finished = True
            rec = self.owner.rec
            self.stream.accept_waveform(RATE, np.zeros(int(TAIL_PAD_S * RATE), dtype=np.float32))
            self.stream.input_finished()
            while rec.is_ready(self.stream):
                rec.decode_stream(self.stream)
        return self.partial()


class StreamingAsr:
    def __init__(self, folder: Path, threads: int = THREADS) -> None:
        files = m.transducer_files(folder)
        if files is None:
            raise AsrError(f"потоковая модель неполная: {folder}")
        so = _so()
        with _guarded(f"asr-{folder.name}", "Потоковая модель распознавания"):
            self.rec = so.OnlineRecognizer.from_transducer(
                tokens=str(files["tokens"]),
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                num_threads=threads,
                sample_rate=RATE,
                feature_dim=80,
                decoding_method="greedy_search",
            )

    def session(self) -> StreamSession:
        return StreamSession(self)

    def recognize(self, samples: np.ndarray) -> str:
        """Целая запись потоковой моделью (когда GigaAM нет)."""
        s = self.session()
        x = np.asarray(samples, dtype=np.float32)
        for i in range(0, len(x), RATE):
            s.feed(x[i : i + RATE])
        return s.finish()


class OfflineAsr:
    """GigaAM v3 (с пунктуацией или без) — итоговый текст фразы."""

    def __init__(self, folder: Path, *, punct: bool = False, threads: int = THREADS) -> None:
        files = m.transducer_files(folder)
        if files is None:
            raise AsrError(f"модель распознавания неполная: {folder}")
        so = _so()
        self.punct = punct
        self.lock = threading.Lock()
        with _guarded(f"asr-{folder.name}", "Модель распознавания"):
            self.rec = so.OfflineRecognizer.from_transducer(
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                tokens=str(files["tokens"]),
                num_threads=threads,
                sample_rate=RATE,
                feature_dim=64,
                decoding_method="greedy_search",
                model_type="nemo_transducer",
            )

    def recognize(self, samples: np.ndarray) -> str:
        x = np.asarray(samples, dtype=np.float32)
        if not len(x):
            return ""
        with self.lock:
            stream = self.rec.create_stream()
            stream.accept_waveform(RATE, x)
            self.rec.decode_stream(stream)
            raw = stream.result.text
        return normalize_result(raw, keep_case=self.punct)


class WhisperAsr:
    """Whisper (int8): распознаватель на язык — sherpa-onnx задаёт язык при создании; держим два последних."""

    def __init__(self, folder: Path, threads: int = THREADS) -> None:
        files = m.whisper_files(folder)
        if files is None:
            raise AsrError(f"модель Whisper неполная: {folder}")
        self.files = files
        self.folder = folder
        self.threads = threads
        self.lock = threading.Lock()
        self._recs: dict[str, Any] = {}

    def _rec(self, language: str) -> Any:
        rec = self._recs.get(language)
        if rec is None:
            so = _so()
            with _guarded(f"asr-{self.folder.name}", "Модель Whisper"):
                rec = so.OfflineRecognizer.from_whisper(
                    encoder=str(self.files["encoder"]),
                    decoder=str(self.files["decoder"]),
                    tokens=str(self.files["tokens"]),
                    language=language,
                    task="transcribe",
                    num_threads=self.threads,
                )
            if len(self._recs) >= 2:
                self._recs.pop(next(iter(self._recs)))
            self._recs[language] = rec
        return rec

    def recognize(self, samples: np.ndarray, language: str = "") -> str:
        lang = "" if language in ("", "auto") else language
        x = np.asarray(samples, dtype=np.float32)
        if not len(x):
            return ""
        with self.lock:
            rec = self._rec(lang)
            stream = rec.create_stream()
            stream.accept_waveform(RATE, x)
            rec.decode_stream(stream)
            raw = stream.result.text
        return normalize_result(raw, keep_case=True)


class Recognizers:
    """Модели распознавания ядра: ленивая загрузка, одна копия каждой, выгрузка при удалении модели."""

    def __init__(self, manager: m.ModelManager, threads: int = THREADS) -> None:
        self.manager = manager
        self.threads = threads
        self._lock = threading.RLock()
        self._load_locks: dict[str, threading.Lock] = {}
        self._offline: dict[str, OfflineAsr] = {}
        self._stream: StreamingAsr | None = None
        self._whisper: dict[str, WhisperAsr] = {}
        self.load_ms: dict[str, float] = {}

    def _load(
        self,
        model_id: str,
        factory: Callable[[], Any],
        store: Callable[[Any], None],
        cached: Callable[[], Any],
    ) -> Any:
        with self._lock:
            lock = self._load_locks.setdefault(model_id, threading.Lock())
        with lock:
            ready = cached()
            if ready is not None:  # пока ждали замок, другой поток уже загрузил
                return ready
            t = time.perf_counter()
            try:
                obj = factory()
            except AsrError:
                raise
            except Exception as e:
                raise AsrError(f"модель {model_id} не загрузилась: {type(e).__name__}: {e}") from e
            self.load_ms[model_id] = round((time.perf_counter() - t) * 1000, 1)
            log.info("голос: модель %s загружена за %.0f мс", model_id, self.load_ms[model_id])
            store(obj)
            return obj

    def vad_model(self) -> Path | None:
        if not self.manager.installed(m.VAD_ID):
            return None
        spec = self.manager.spec(m.VAD_ID)
        return self.manager.path(m.VAD_ID) / spec.filename

    def make_vad(self, sensitivity: float, end_silence: float = END_SILENCE_S) -> SileroVad:
        path = self.vad_model()
        if path is None:
            raise AsrError("нет модели детектора речи (Silero VAD)")
        return SileroVad(path, **vad_params(sensitivity, end_silence))

    def make_pause_vad(self, sensitivity: float) -> SileroVad:
        """Тот же VAD, но «речь стихла» — уже после PAUSE_SILENCE_S тишины: итог — заранее."""
        path = self.vad_model()
        if path is None:
            raise AsrError("нет модели детектора речи (Silero VAD)")
        return SileroVad(path, **(vad_params(sensitivity) | {"min_silence": PAUSE_SILENCE_S}))

    def streaming(self) -> StreamingAsr | None:
        if self._stream is not None:
            return self._stream
        if not self.manager.installed(m.STREAM_ID):
            return None

        def store(obj: Any) -> None:
            self._stream = obj

        return self._load(
            m.STREAM_ID,
            lambda: StreamingAsr(self.manager.path(m.STREAM_ID), self.threads),
            store,
            lambda: self._stream,
        )

    def offline(self, model_id: str) -> OfflineAsr | None:
        cached = self._offline.get(model_id)
        if cached is not None:
            return cached
        if model_id not in m.ASR_IDS or not self.manager.installed(model_id):
            return None

        def make() -> OfflineAsr:
            asr = OfflineAsr(self.manager.path(model_id), punct=model_id == m.PUNCT_ID, threads=self.threads)
            asr.recognize(np.zeros(RATE // 2, dtype=np.float32))  # прогрев: первое декодирование дольше
            return asr

        def store(obj: Any) -> None:
            self._offline[model_id] = obj

        return self._load(model_id, make, store, lambda: self._offline.get(model_id))

    def pick_offline(self, preferred: str, *, punct: bool = False) -> OfflineAsr | None:
        """Модель для итогового текста: с пунктуацией (если просили и есть) → из настроек → любая GigaAM."""
        order: list[str] = []
        if punct:
            order.append(m.PUNCT_ID)
        if preferred in m.ASR_IDS:
            order.append(preferred)
        order += [i for i in m.ASR_IDS if i not in order]
        if preferred == m.STREAM_ID and not punct:
            return None  # в настройках выбрана потоковая — GigaAM не нужна
        for model_id in order:
            if self.manager.installed(model_id):
                return self.offline(model_id)
        return None

    def whisper(self) -> WhisperAsr | None:
        for model_id in m.WHISPER_IDS:
            cached = self._whisper.get(model_id)
            if cached is not None:
                return cached
        for model_id in m.WHISPER_IDS:
            if self.manager.installed(model_id):

                def store(obj: Any, model_id: str = model_id) -> None:
                    self._whisper[model_id] = obj

                return self._load(
                    model_id,
                    lambda model_id=model_id: WhisperAsr(self.manager.path(model_id), self.threads),
                    store,
                    lambda model_id=model_id: self._whisper.get(model_id),
                )
        return None

    def loaded(self) -> list[str]:
        out = list(self._offline)
        if self._stream is not None:
            out.append(m.STREAM_ID)
        out += list(self._whisper)
        return out

    def unload(self, model_id: str | None = None) -> None:
        """Выгрузить модель (перед удалением файлов) или все (None)."""
        with self._lock:
            if model_id is None or model_id == m.STREAM_ID:
                self._stream = None
            for store in (self._offline, self._whisper):
                for key in list(store):
                    if model_id is None or key == model_id:
                        store.pop(key, None)
