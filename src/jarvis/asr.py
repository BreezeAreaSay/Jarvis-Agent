"""Распознавание речи на CPU: sherpa-onnx, русские модели — локально, звук никуда не уходит.

Модели — в <data>\\models\\<папка> (скачивает scripts\\get_voice_models.ps1):
- gigaam-v3 (по умолчанию) — GigaAM v3 RNNT (Сбер, MIT), sherpa-onnx `nemo_transducer`, 64 мел-признака;
- gigaam-v3-punct — GigaAM v3 e2e: с пунктуацией, заглавными и числами цифрами («Громкость 30%.»);
- gigaam-v2 — GigaAM v2 RNNT;
- vosk-small — малая потоковая zipformer (alphacep, Apache-2.0) — запасная, если CPU слабый.
Тишина по краям срезается Silero VAD (<data>\\models\\vad\\silero_vad.onnx; нет файла — без обрезки).

Recognizer.load() грузит модель один раз (секунды) — в фоне после старта, не на горячем пути. recognize()
вызывается из одного рабочего потока; sherpa-onnx отпускает GIL на время декодирования.
"""

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from jarvis.mic import RATE, Recording
from pc import settings

log = logging.getLogger("jarvis")

DEFAULT_MODEL = "gigaam-v3"
VAD_DIR = "vad"
VAD_FILE = "silero_vad.onnx"
PAD_BEFORE_S = 0.25  # запас до первого слова и после последнего: VAD обрезает края звуков
PAD_AFTER_S = 0.35
TAIL_PAD_S = 0.5  # потоковой модели — тишина в конце, чтобы она выдала последние слова
WARMUP_S = 1.0
VAD_WINDOW = 512  # окно Silero VAD при 16 кГц, отсчётов


@dataclass(frozen=True)
class ModelSpec:
    folder: str
    kind: str  # "offline" — OfflineRecognizer (GigaAM), "online" — OnlineRecognizer (zipformer)
    feature_dim: int
    model_type: str = ""
    title: str = ""


MODELS: dict[str, ModelSpec] = {
    "gigaam-v3": ModelSpec("gigaam-v3", "offline", 64, "nemo_transducer", "GigaAM v3"),
    "gigaam-v3-punct": ModelSpec("gigaam-v3-punct", "offline", 64, "nemo_transducer", "GigaAM v3 e2e"),
    "gigaam-v2": ModelSpec("gigaam-v2", "offline", 64, "nemo_transducer", "GigaAM v2"),
    "vosk-small": ModelSpec("vosk-small", "online", 80, "", "Vosk small (zipformer)"),
}


class AsrError(RuntimeError):
    """Модель не найдена или не загрузилась."""


@dataclass(frozen=True)
class AsrResult:
    text: str  # нормализованный текст — как набранная команда
    raw: str  # как выдала модель
    asr_ms: float  # от вызова recognize() до текста: VAD + декодирование
    audio_s: float  # длина записи
    speech_s: float  # длина после обрезки тишины (0 — речи нет)
    vad_ms: float = 0.0


def models_dir() -> Path:
    return settings.data_dir() / "models"


def model_dir(name: str) -> Path:
    return models_dir() / MODELS[name].folder


def vad_path() -> Path:
    return models_dir() / VAD_DIR / VAD_FILE


def _pick(folder: Path, part: str) -> Path | None:
    """encoder/decoder/joiner: сначала *.int8.onnx, потом обычный *.onnx (имена у моделей разные)."""
    found = sorted(folder.glob(f"{part}*.onnx"))
    for p in found:
        if p.name.endswith(".int8.onnx"):
            return p
    return found[0] if found else None


def model_files(name: str) -> dict[str, Path]:
    """Файлы модели: encoder, decoder, joiner, tokens. Нет модели или файла — AsrError с подсказкой."""
    if name not in MODELS:
        raise AsrError(f"неизвестная модель распознавания: {name} (есть: {', '.join(MODELS)})")
    folder = model_dir(name)
    hint = f"скачай: pwsh -File scripts\\get_voice_models.ps1 -Asr {name}"
    if not folder.is_dir():
        raise AsrError(f"нет модели {name}: папки {folder} нет — {hint}")
    files: dict[str, Path] = {}
    for part in ("encoder", "decoder", "joiner"):
        path = _pick(folder, part)
        if path is None:
            raise AsrError(f"модель {name}: нет {part}*.onnx в {folder} — {hint}")
        files[part] = path
    tokens = folder / "tokens.txt"
    if not tokens.is_file():
        raise AsrError(f"модель {name}: нет tokens.txt в {folder} — {hint}")
    files["tokens"] = tokens
    return files


# --- нормализация текста ------------------------------------------------------------------------

WAKE_WORD = "джарвис"
WAKE_RATIO = 70  # «джарис», «джорвиш», «жарвис», «шарвис» — так модели слышат обращение
_TRAIL = re.compile(r"[\s.!…,;:]+$")
_EDGE = ",.!?…:;—–-"


def strip_wake(text: str) -> str:
    """Обращение «Джарвис» в начале — убрать: одно или два первых слова, нечётко (модель пишет его
    по-разному: «джарис», «шарвис», «джор виш»); слово должно начинаться на «дж», «ж», «ш» или «j»."""
    words = text.split()
    if not words or not words[0].casefold().startswith(("дж", "ж", "ш", "j")):
        return text
    for n in (1, 2):
        if len(words) < n:
            break
        head = "".join(words[:n]).casefold().strip(_EDGE)
        if head == "jarvis" or fuzz.ratio(head, WAKE_WORD) >= WAKE_RATIO:
            return " ".join(words[n:])
    return text


def normalize(raw: str) -> str:
    """Текст модели → как набранная команда: без «Джарвис» в начале, без точки в конце, строчная первая
    буква обычного слова; «?» остаётся (по нему роутер узнаёт вопрос)."""
    text = strip_wake(" ".join(str(raw).split()))
    text = _TRAIL.sub("", text).lstrip(" " + _EDGE)
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    elif len(text) == 1:
        text = text.lower()
    return text


# --- звук ---------------------------------------------------------------------------------------


def resample(samples: list[float], src: int, dst: int = RATE) -> list[float]:
    """Линейная передискретизация (запасной путь: микрофон не умеет 16 кГц, а VAD нужен 16 кГц)."""
    if src == dst or not samples:
        return samples
    n = max(1, int(len(samples) * dst / src))
    step = src / dst
    last = len(samples) - 1
    out = []
    for i in range(n):
        pos = i * step
        j = int(pos)
        if j >= last:
            out.append(samples[last])
            continue
        frac = pos - j
        out.append(samples[j] * (1.0 - frac) + samples[j + 1] * frac)
    return out


# --- распознаватель -----------------------------------------------------------------------------


class Recognizer:
    """Модель распознавания и VAD. load() — один раз (в фоне); recognize() — из одного рабочего потока."""

    def __init__(self, model: str = DEFAULT_MODEL, threads: int = 2, vad: bool = True) -> None:
        self.model = model if model in MODELS else DEFAULT_MODEL
        self.threads = max(1, int(threads))
        self.use_vad = vad
        self.ready = False
        self.error = ""
        self.load_ms = 0.0
        self._rec: Any = None
        self._vad: Any = None
        self._vad_cfg: Any = None

    @property
    def spec(self) -> ModelSpec:
        return MODELS[self.model]

    def load(self) -> None:
        """Загрузить модель (и VAD, если есть файл) и прогреть одним декодированием. Ошибка — AsrError."""
        t = time.perf_counter()
        try:
            files = model_files(self.model)
            import sherpa_onnx

            self._rec = self._create(sherpa_onnx, files)
            if self.use_vad:
                self._vad_cfg = self._vad_config(sherpa_onnx)
            self._decode([0.0] * int(WARMUP_S * RATE))
        except AsrError as e:
            self.error = str(e)
            raise
        except Exception as e:
            self.error = f"модель {self.model} не загрузилась: {type(e).__name__}: {e}"
            raise AsrError(self.error) from e
        self.load_ms = round((time.perf_counter() - t) * 1000, 1)
        self.ready, self.error = True, ""
        vad = "с VAD" if self._vad_cfg is not None else "без VAD"
        log.info(
            "голос: модель %s загружена за %.0f мс (%s, потоков %d)",
            self.model,
            self.load_ms,
            vad,
            self.threads,
        )

    def _create(self, so: Any, files: dict[str, Path]) -> Any:
        spec = self.spec
        common = {
            "encoder": str(files["encoder"]),
            "decoder": str(files["decoder"]),
            "joiner": str(files["joiner"]),
            "tokens": str(files["tokens"]),
            "num_threads": self.threads,
            "sample_rate": RATE,
            "feature_dim": spec.feature_dim,
            "decoding_method": "greedy_search",
        }
        if spec.kind == "online":
            return so.OnlineRecognizer.from_transducer(**common)
        return so.OfflineRecognizer.from_transducer(**common, model_type=spec.model_type)

    def _vad_config(self, so: Any) -> Any:
        path = vad_path()
        if not path.is_file():
            log.warning("голос: нет %s — тишина по краям не обрезается", path)
            return None
        silero = so.SileroVadModelConfig(
            model=str(path),
            threshold=0.5,
            min_silence_duration=0.25,
            min_speech_duration=0.1,
            window_size=VAD_WINDOW,
            max_speech_duration=120,
        )
        cfg = so.VadModelConfig(silero_vad=silero, sample_rate=RATE, num_threads=1)
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=130)
        return cfg

    def speech_bounds(self, samples: list[float]) -> tuple[int, int] | None:
        """(начало, конец) речи в отсчётах с запасом по краям; речи нет — None; VAD нет — вся запись."""
        if self._vad is None:
            return 0, len(samples)
        vad = self._vad
        vad.reset()
        # окнами по VAD_WINDOW: весь буфер одним вызовом sherpa-onnx 1.13.8 разбирает неверно (проверено)
        for i in range(0, len(samples), VAD_WINDOW):
            vad.accept_waveform(samples[i : i + VAD_WINDOW])
        vad.flush()
        first, last = -1, -1
        while not vad.empty():
            seg = vad.front
            start, length = int(seg.start), len(seg.samples)
            if first < 0:
                first = start
            last = max(last, start + length)
            vad.pop()
        if first < 0:
            return None
        a = max(0, first - int(PAD_BEFORE_S * RATE))
        b = min(len(samples), last + int(PAD_AFTER_S * RATE))
        return a, b

    def _decode(self, samples: list[float]) -> str:
        rec = self._rec
        if self.spec.kind == "online":
            stream = rec.create_stream()
            stream.accept_waveform(RATE, samples)
            stream.accept_waveform(RATE, [0.0] * int(TAIL_PAD_S * RATE))
            stream.input_finished()
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            return str(rec.get_result(stream))
        stream = rec.create_stream()
        stream.accept_waveform(RATE, samples)
        rec.decode_stream(stream)
        return str(stream.result.text)

    def recognize(self, recording: Recording) -> AsrResult:
        """Запись → текст. Модель не загружена — AsrError."""
        if not self.ready:
            raise AsrError(self.error or "модель распознавания ещё грузится")
        t0 = time.perf_counter()
        samples = recording.samples()
        if recording.rate != RATE:
            samples = resample(samples, recording.rate, RATE)
        audio_s = len(samples) / RATE
        t_vad = time.perf_counter()
        bounds = self.speech_bounds(samples) if samples else None
        vad_ms = (time.perf_counter() - t_vad) * 1000
        if bounds is None:
            return AsrResult(
                "", "", round((time.perf_counter() - t0) * 1000, 1), audio_s, 0.0, round(vad_ms, 1)
            )
        a, b = bounds
        speech = samples[a:b]
        raw = self._decode(speech).strip()
        asr_ms = round((time.perf_counter() - t0) * 1000, 1)
        return AsrResult(
            normalize(raw), raw, asr_ms, round(audio_s, 3), round(len(speech) / RATE, 3), round(vad_ms, 1)
        )
