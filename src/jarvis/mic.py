"""Звук для голоса: запись с микрофона в память (push-to-talk) и вывод на динамики (озвучка ответов).

sounddevice (PortAudio) — за слоем `_api` (создаётся лениво api()): импорт sounddevice инициализирует
PortAudio и перечисляет устройства (на Windows — до сотен мс), поэтому только в фоне при старте (`warmup()`)
или в doctor; на Linux без PortAudio импорт падает OSError — тесты подменяют `mic._api` фейком. numpy
не нужен: RawInputStream/RawOutputStream работают с байтами.

Запись: 16 кГц, моно, int16, блоки по 50 мс; на каждый блок — уровень громкости 0..1 (для сферы, ~20 раз
в секунду); предел — max_s секунд (дальше блоки не копятся, `full` = True). Поток PortAudio только копирует
байты и считает уровень — без блокировок и ввода-вывода.
"""

import array
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("jarvis")

RATE = 16000
BLOCK_S = 0.05  # 20 блоков в секунду — столько раз в секунду обновляется уровень
LEVEL_FLOOR_DB = -60.0  # тише — уровень 0
LEVEL_CEIL_DB = -12.0  # громче — уровень 1


class MicError(RuntimeError):
    """Микрофон не открылся (нет устройства, занято, нет PortAudio)."""


@dataclass(frozen=True)
class Recording:
    """Запись: PCM int16 моно, little-endian."""

    pcm: bytes
    rate: int = RATE

    @property
    def frames(self) -> int:
        return len(self.pcm) // 2

    @property
    def duration_s(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    def samples(self) -> list[float]:
        """Отсчёты в [-1, 1] — так их принимает sherpa-onnx (≈2 мс на 3 с звука)."""
        data = array.array("h")
        data.frombytes(self.pcm[: self.frames * 2])
        scale = 1.0 / 32768.0
        return [x * scale for x in data]


def level(block: bytes) -> float:
    """Уровень громкости блока int16: RMS в дБ полной шкалы → 0..1 (−60 дБ → 0, −12 дБ → 1)."""
    data = array.array("h")
    data.frombytes(block[: len(block) // 2 * 2])
    if not data:
        return 0.0
    rms = math.sqrt(sum(x * x for x in data) / len(data))
    if rms < 1.0:
        return 0.0
    db = 20.0 * math.log10(rms / 32768.0)
    return min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)))


# --- слой sounddevice ---------------------------------------------------------------------------


class _SoundDevice:
    """Тонкий слой над sounddevice; импорт — при создании (в фоне при старте, в doctor)."""

    def __init__(self) -> None:
        import sounddevice

        self.sd = sounddevice

    def devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.sd.query_devices()]

    def hostapi_name(self, index: int) -> str:
        return str(self.sd.query_hostapis(index)["name"])

    def default_device(self, kind: str) -> int | None:
        """Номер устройства по умолчанию: kind = "input" | "output"; нет — None."""
        try:
            value = self.sd.default.device[0 if kind == "input" else 1]
        except Exception:
            return None
        return int(value) if isinstance(value, int) and value >= 0 else None

    def open_input(self, device: int | None, rate: int, block: int, callback: Callable[..., None]) -> Any:
        stream = self.sd.RawInputStream(
            samplerate=rate,
            blocksize=block,
            device=device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=callback,
        )
        stream.start()
        return stream

    def open_output(self, device: int | None, rate: int) -> Any:
        stream = self.sd.RawOutputStream(
            samplerate=rate, device=device, channels=1, dtype="float32", latency="low"
        )
        stream.start()
        return stream

    def refresh(self) -> None:
        """Перечитать список устройств (подключили новый микрофон): PortAudio заново."""
        self.sd._terminate()
        self.sd._initialize()


_api: Any = None
_api_lock = threading.Lock()


def api() -> Any:
    """Слой sounddevice; первый вызов импортирует его (инициализация PortAudio). Нет — MicError."""
    global _api
    with _api_lock:
        if _api is None:
            try:
                _api = _SoundDevice()
            except Exception as e:  # OSError: нет PortAudio; ImportError
                raise MicError(f"звук недоступен: {e}") from e
        return _api


def warmup() -> None:
    """Импорт sounddevice и список устройств — заранее, в фоне при старте `jarvis run`."""
    t = time.perf_counter()
    try:
        api().devices()
    except Exception as e:
        log.warning("звук: PortAudio не готов: %s", e)
        return
    log.info("звук: PortAudio готов за %.0f мс", (time.perf_counter() - t) * 1000)


def find_device(name: str, kind: str = "input") -> int | None:
    """Устройство по части имени (без учёта регистра); "" или не найдено — None (по умолчанию Windows).

    Среди совпадений — сначала устройство API по умолчанию (MME: имя может быть обрезано до 31 символа).
    """
    query = name.strip().casefold()
    if not query:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    sd = api()
    found = [
        (i, d)
        for i, d in enumerate(sd.devices())
        if d.get(key, 0) > 0 and query in str(d.get("name", "")).casefold()
    ]
    if not found:
        log.warning("звук: устройство «%s» не найдено — беру устройство по умолчанию", name)
        return None
    default = sd.default_device(kind)
    default_api = sd.devices()[default].get("hostapi") if default is not None else None
    found.sort(key=lambda item: item[1].get("hostapi") != default_api)
    return found[0][0]


def device_name(index: int | None, kind: str = "input") -> str:
    """Имя устройства (None — по умолчанию); ошибка — пусто."""
    try:
        sd = api()
        if index is None:
            index = sd.default_device(kind)
        if index is None:
            return ""
        return str(sd.devices()[index].get("name", ""))
    except Exception:
        return ""


def _default_rate(sd: Any, index: int | None) -> int:
    try:
        if index is None:
            index = sd.default_device("input")
        return int(sd.devices()[index].get("default_samplerate") or 0) if index is not None else 0
    except Exception:
        return 0


# --- запись -------------------------------------------------------------------------------------


class Recorder:
    """Одна запись за раз: start() → блоки копятся в памяти → stop() → Recording; abort() — выбросить.

    on_level(value) зовётся из потока PortAudio на каждый блок (≈20 раз в секунду) — должен быть быстрым
    (сигнал Qt, присваивание). Ошибка открытия — MicError.
    """

    def __init__(
        self,
        device: str = "",
        max_s: float = 30.0,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.device = device
        self.max_s = max_s
        self.on_level = on_level
        self.rate = RATE  # частота текущей записи: 16 кГц, если устройство умеет
        self.full = False  # набрано max_s секунд — дальше не пишется
        self.overflows = 0
        self._chunks: list[bytes] = []
        self._frames = 0
        self._max_frames = int(max_s * RATE)
        self._stream: Any = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._stream is not None

    @property
    def seconds(self) -> float:
        return self._frames / self.rate

    def start(self) -> None:
        """Открыть микрофон и начать запись (десятки мс). Повторный start() без stop() — ошибка."""
        with self._lock:
            if self._stream is not None:
                raise MicError("запись уже идёт")
            self._chunks, self._frames, self.full, self.overflows = [], 0, False, 0
            sd = api()
            try:
                index = find_device(self.device, "input")
                try:
                    self._open(sd, index, RATE)
                except Exception as e:  # устройство не умеет 16 кГц (WASAPI) — его родная частота
                    rate = _default_rate(sd, index)
                    if not rate or rate == RATE:
                        raise
                    log.info("звук: 16 кГц не открылось (%s) — пишу на %d Гц", e, rate)
                    self._open(sd, index, rate)
            except MicError:
                raise
            except Exception as e:
                raise MicError(f"микрофон не открылся: {e}") from e

    def _open(self, sd: Any, index: int | None, rate: int) -> None:
        self.rate = rate
        self._max_frames = int(self.max_s * rate)
        self._stream = sd.open_input(index, rate, int(rate * BLOCK_S), self._on_audio)

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Колбэк PortAudio: копия блока и уровень. Без исключений наружу."""
        try:
            if status:
                self.overflows += 1
            if self._frames >= self._max_frames:
                self.full = True
                return
            block = bytes(indata)
            room = (self._max_frames - self._frames) * 2
            if len(block) > room:
                block = block[:room]
                self.full = True
            self._chunks.append(block)
            self._frames += len(block) // 2
            if self.on_level is not None:
                self.on_level(level(block))
        except Exception:  # колбэк PortAudio не должен падать
            log.debug("звук: ошибка в колбэке записи", exc_info=True)

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as e:
            log.debug("звук: stop: %s", e)
        try:
            stream.close()
        except Exception as e:
            log.debug("звук: close: %s", e)

    def stop(self) -> Recording:
        """Остановить запись и отдать её."""
        with self._lock:
            self._close()
            pcm = b"".join(self._chunks)
            self._chunks = []
        return Recording(pcm, self.rate)

    def abort(self) -> None:
        """Остановить и выбросить записанное."""
        with self._lock:
            self._close()
            self._chunks, self._frames = [], 0
