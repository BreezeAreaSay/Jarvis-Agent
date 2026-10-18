"""Модели голоса: реестр, состояние, скачивание с проверкой SHA-256, безопасная распаковка, удаление.

Модели лежат в paths.models_dir()/<папка> — те же папки, что у jarvis-v1 (gigaam-v3, vad, tts-ruslan…),
поэтому уже скачанное прежней версией не качается заново. Источник — релизы k2-fsa/sherpa-onnx на GitHub
(размеры и SHA-256 проверены 2026-10-11 и 2026-10-18).

Скачивание: httpx потоком в <models>/.download/<id>.part (обрыв — докачка с того же места), проверка размера
и SHA-256, распаковка tar.bz2 во временную папку (пути наружу и абсолютные — отказ), затем атомарная замена
папки модели. Прогресс — события model.download {id, progress, done, error?} не чаще 4 раз в секунду.
"""

import asyncio
import contextlib
import hashlib
import inspect
import logging
import os
import posixpath
import re
import shutil
import tarfile
import tempfile
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from jarvis import crashguard, paths
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: str  # asr | stream | vad | tts | whisper
    title: str
    url: str
    size: int  # байт в скачиваемом файле
    sha256: str  # "" — не проверять (известен только размер)
    folder: str  # папка в models_dir()
    filename: str = ""  # одиночный файл (не архив): имя внутри папки
    description: str = ""

    @property
    def archive(self) -> bool:
        return not self.filename

    @property
    def size_mb(self) -> float:
        return round(self.size / 1_000_000, 1)


def _asr(model_id: str, kind: str, title: str, name: str, size: int, sha: str, desc: str) -> ModelSpec:
    url = f"{RELEASES}/asr-models/{name}.tar.bz2"
    return ModelSpec(model_id, kind, title, url, size, sha, model_id, "", desc)


def _piper(voice: str, title: str, size: int, sha: str, desc: str) -> ModelSpec:
    url = f"{RELEASES}/tts-models/vits-piper-ru_RU-{voice}-medium.tar.bz2"
    return ModelSpec(f"piper-{voice}", "tts", title, url, size, sha, f"tts-{voice}", "", desc)


SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "silero-vad",
        "vad",
        "Детектор речи (Silero VAD)",
        f"{RELEASES}/asr-models/silero_vad.onnx",
        643854,
        "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        "vad",
        "silero_vad.onnx",
        "Отличает речь от тишины и шума; распознавание работает только пока вы говорите. MIT.",
    ),
    _asr(
        "vosk-small",
        "stream",
        "Потоковая модель (Vosk small)",
        "sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16",
        24110855,
        "6ba68a01ff3c5445aaf2d61e9b97b026f1149dcc9049d11af3f44f55176341d8",
        "Быстрая: текст по ходу речи и раннее «Джарвис». Точность ниже GigaAM. Apache-2.0.",
    ),
    _asr(
        "gigaam-v3",
        "asr",
        "GigaAM v3 — распознавание команд",
        "sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16",
        167388020,
        "20a439491904b839f35eb8efaa3d99cbfaaad0c6dcba22d06ff218bb0056772d",
        "Точное распознавание русского (18–19 из 20 команд), ≈0,3 с на фразу. MIT.",
    ),
    _asr(
        "gigaam-v3-punct",
        "asr",
        "GigaAM v3 с пунктуацией — диктовка",
        "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16",
        170197019,
        "f9620a0099019c6afcee26525ef9ed3297fa50dd5691c1902af0c948fc1a470b",
        "Расставляет точки, запятые и заглавные, числа — цифрами. Для диктовки и голосовых. MIT.",
    ),
    _piper(
        "ruslan",
        "Голос «Руслан» (мужской)",
        67210684,
        "0690b1cad01f86e8db9ba988af24898bdc1af774e23cb2e46b9c730269b6fd83",
        "Голос Jarvis по умолчанию. Датасет CC BY-NC-SA 4.0 — для личного использования.",
    ),
    _piper(
        "denis",
        "Голос «Денис» (мужской)",
        67190991,
        "efa4c18e0b5e32b81d1b6df36b9d312831e5d545200e27848ef926a4cd930300",
        "Датасет CC0.",
    ),
    _piper(
        "dmitri",
        "Голос «Дмитрий» (мужской)",
        67188551,
        "c86d0803737de13d441923ff3b3f309482fab8d7af3ec85949942809eb9a3660",
        "Датасет CC0.",
    ),
    _piper("irina", "Голос «Ирина» (женский)", 67153308, "", "Женский голос."),
    _asr(
        "whisper-tiny",
        "whisper",
        "Whisper tiny — другие языки",
        "sherpa-onnx-whisper-tiny",
        116204861,
        "c46116994e539aa165266d96b325252728429c12535eb9d8b6a2b10f129e66b1",
        "Многоязычная (английский и др.) для субтитров и голосовых: быстрая, точность средняя. MIT.",
    ),
    _asr(
        "whisper-base",
        "whisper",
        "Whisper base — другие языки",
        "sherpa-onnx-whisper-base",
        207557382,
        "911b2083efd7c0dca2ac3b358b75222660dc09fb716d64fbfc417ba6c99ff3de",
        "Многоязычная, точнее tiny, но медленнее. MIT.",
    ),
)
MODELS: dict[str, ModelSpec] = {m.id: m for m in SPECS}

VAD_ID = "silero-vad"
STREAM_ID = "vosk-small"
ASR_IDS = ("gigaam-v3", "gigaam-v3-punct")
PUNCT_ID = "gigaam-v3-punct"
WHISPER_IDS = ("whisper-base", "whisper-tiny")  # порядок предпочтения
PIPER_VOICES = ("ruslan", "denis", "dmitri", "irina")


def piper_id(voice: str) -> str:
    return f"piper-{voice}"


# --- файлы моделей --------------------------------------------------------------------------------


def pick_onnx(folder: Path, part: str, *, prefer_int8: bool = True) -> Path | None:
    """encoder/decoder/joiner: *.int8.onnx или обычный *.onnx (имена у моделей разные)."""
    try:
        found = sorted(p for p in folder.glob(f"*{part}*.onnx") if p.is_file())
    except OSError:
        return None
    if not found:
        return None
    int8 = [p for p in found if p.name.endswith(".int8.onnx")]
    plain = [p for p in found if not p.name.endswith(".int8.onnx")]
    first, second = (int8, plain) if prefer_int8 else (plain, int8)
    return (first or second)[0]


def transducer_files(folder: Path) -> dict[str, Path] | None:
    """encoder, decoder, joiner, tokens транcдьюсера (GigaAM, Vosk); чего-то нет — None."""
    tokens = folder / "tokens.txt"
    if not tokens.is_file():
        return None
    files = {"tokens": tokens}
    for part in ("encoder", "decoder", "joiner"):
        path = pick_onnx(folder, part)
        if path is None:
            return None
        files[part] = path
    return files


def whisper_files(folder: Path) -> dict[str, Path] | None:
    try:
        tokens = next(iter(sorted(folder.glob("*tokens.txt"))), None)
    except OSError:
        return None
    encoder, decoder = pick_onnx(folder, "encoder"), pick_onnx(folder, "decoder")
    if tokens is None or encoder is None or decoder is None:
        return None
    return {"encoder": encoder, "decoder": decoder, "tokens": tokens}


def piper_files(folder: Path) -> dict[str, Path] | None:
    """model (.onnx, fp32 — int8-голоса медленнее), tokens.txt, espeak-ng-data."""
    tokens, espeak = folder / "tokens.txt", folder / "espeak-ng-data"
    if not tokens.is_file() or not espeak.is_dir():
        return None
    try:
        models = sorted(p for p in folder.glob("*.onnx") if p.is_file())
    except OSError:
        return None
    if not models:
        return None
    plain = [p for p in models if ".int8." not in p.name]
    return {"model": (plain or models)[0], "tokens": tokens, "espeak": espeak}


def folder_complete(spec: ModelSpec, folder: Path) -> bool:
    if spec.kind == "vad":
        f = folder / spec.filename
        return f.is_file() and f.stat().st_size > 0
    if spec.kind in ("asr", "stream"):
        return transducer_files(folder) is not None
    if spec.kind == "whisper":
        return whisper_files(folder) is not None
    if spec.kind == "tts":
        return piper_files(folder) is not None
    return folder.is_dir()


# --- распаковка -----------------------------------------------------------------------------------

_DRIVE = re.compile(r"^[A-Za-z]:")


class DownloadError(Exception):
    """Скачивание или установка не удались; текст — для человека."""


def check_member(name: str) -> None:
    """Путь внутри архива: только относительный и без «..» (иначе файл ляжет вне папки моделей)."""
    clean = name.replace("\\", "/")
    if not clean or clean.startswith("/") or _DRIVE.match(clean):
        raise DownloadError(f"в архиве недопустимый путь: {name}")
    if ".." in PurePosixPath(clean).parts:
        raise DownloadError(f"в архиве недопустимый путь: {name}")


def safe_extract(archive: Path, dest: Path) -> None:
    """tar.* → dest. Абсолютные пути, «..», ссылки наружу, устройства — DownloadError (ничего не пишется)."""
    try:
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            for m in members:
                check_member(m.name)
                if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                    raise DownloadError(f"в архиве недопустимый файл: {m.name}")
                if m.islnk():
                    check_member(m.linkname)
                elif m.issym():
                    link = m.linkname.replace("\\", "/")
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(m.name), link))
                    outside = target == ".." or target.startswith("../")
                    if link.startswith("/") or _DRIVE.match(link) or outside:
                        raise DownloadError(f"в архиве ссылка наружу: {m.name} -> {m.linkname}")
            tf.extractall(dest, members=members, filter="data")
    except DownloadError:
        raise
    except (tarfile.TarError, OSError, EOFError) as e:
        raise DownloadError(f"архив не распаковался: {e}") from e


def _hash_file(path: Path, h: Any) -> None:
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)


# --- менеджер -------------------------------------------------------------------------------------

Listener = Callable[[str, str], Any]  # (событие "installed" | "deleted", id) → None | awaitable


@dataclass
class _Job:
    task: "asyncio.Task[None]"
    progress: float = 0.0
    stage: str = "download"  # download | install


class ModelManager:
    """Состояние и скачивание моделей. Методы async — из цикла ядра; installed()/info() — из любого потока."""

    def __init__(
        self,
        *,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
        root: Path | Callable[[], Path] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        specs: tuple[ModelSpec, ...] = SPECS,
        throttle_s: float = 0.25,
        space_factor: float = 2.6,
    ) -> None:
        self._publish = publish
        self._root = root
        self.transport = transport
        self.specs = {s.id: s for s in specs}
        self.throttle_s = throttle_s
        self.space_factor = space_factor
        self._jobs: dict[str, _Job] = {}
        self._errors: dict[str, str] = {}
        self._last_emit: dict[str, float] = {}
        self._listeners: list[Listener] = []

    # --- реестр и состояние
    @property
    def root(self) -> Path:
        if self._root is None:
            return paths.models_dir()
        root = self._root() if callable(self._root) else self._root
        root.mkdir(parents=True, exist_ok=True)
        return root

    def spec(self, model_id: str) -> ModelSpec:
        spec = self.specs.get(model_id)
        if spec is None:
            raise UserFacingError(f"Неизвестная модель: {model_id}")
        return spec

    def path(self, model_id: str) -> Path:
        return self.root / self.spec(model_id).folder

    def installed(self, model_id: str) -> bool:
        spec = self.specs.get(model_id)
        if spec is None or (model_id in self._jobs and self._jobs[model_id].stage == "install"):
            return False
        try:
            return folder_complete(spec, self.root / spec.folder)
        except OSError:
            return False

    def downloading(self, model_id: str) -> bool:
        return model_id in self._jobs

    def missing(self, ids: list[str] | tuple[str, ...]) -> list[str]:
        return [i for i in ids if not self.installed(i)]

    def error(self, model_id: str) -> str:
        return self._errors.get(model_id, "")

    @staticmethod
    def required_ids(settings: "Settings", installed: Callable[[str], bool] | None = None) -> list[str]:
        """Что нужно голосу при текущих настройках: VAD, потоковая, модель команд, голос piper; команды не
        по-русски — ещё Whisper (уже скачанный tiny годится — installed, иначе base: точнее)."""
        ids = [VAD_ID, STREAM_ID]
        asr = settings.voice.asr_model
        if asr in MODELS and asr not in ids:
            ids.append(asr)
        if settings.voice.language != "ru":
            have = [i for i in WHISPER_IDS if installed is not None and installed(i)]
            ids.append(have[0] if have else WHISPER_IDS[0])
        ids.append(piper_id(settings.tts.piper_voice))
        return ids

    def info(self, model_id: str, required: bool = False) -> dict[str, Any]:
        spec = self.spec(model_id)
        job = self._jobs.get(model_id)
        installed = self.installed(model_id)
        return {
            "id": spec.id,
            "kind": spec.kind,
            "title": spec.title,
            "description": spec.description,
            "size_mb": spec.size_mb,
            "installed": installed,
            "downloading": job is not None,
            "progress": round(job.progress, 3) if job else (1.0 if installed else 0.0),
            "required": required,
            "error": self._errors.get(model_id, ""),
        }

    def catalog(self, required: list[str] | None = None) -> list[dict[str, Any]]:
        req = set(required or ())
        return [self.info(s.id, s.id in req) for s in self.specs.values()]

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    async def _notify(self, event: str, model_id: str) -> None:
        for listener in list(self._listeners):
            try:
                result = listener(event, model_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("обработчик события модели упал")

    # --- скачивание
    def start(self, model_id: str) -> "asyncio.Task[None]":
        """Скачать в фоне (повторный вызов — та же задача). Ошибка — в событии model.download."""
        spec = self.spec(model_id)
        job = self._jobs.get(spec.id)
        if job is not None:
            return job.task
        task = asyncio.ensure_future(self._run(spec))
        task.set_name(f"model-download:{spec.id}")
        self._jobs[spec.id] = _Job(task)
        task.add_done_callback(_consume_error)
        return task

    async def download(self, model_id: str) -> None:
        """Скачать и дождаться; ошибка — DownloadError с текстом для человека."""
        if self.installed(model_id) and model_id not in self._jobs:
            return
        await asyncio.shield(self.start(model_id))

    async def ensure(self, ids: list[str] | tuple[str, ...]) -> dict[str, str]:
        """Скачать недостающие по очереди; вернёт {id: ошибка} для неудавшихся."""
        errors: dict[str, str] = {}
        for model_id in ids:
            if self.installed(model_id) and model_id not in self._jobs:
                continue
            try:
                await self.download(model_id)
            except DownloadError as e:
                errors[model_id] = str(e)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # отменили того, кто ждёт, а не саму загрузку
                errors[model_id] = "скачивание отменено"
        return errors

    async def cancel_all(self) -> None:
        for job in list(self._jobs.values()):
            job.task.cancel()
        for job in list(self._jobs.values()):
            with contextlib.suppress(BaseException):
                await job.task

    def _emit(self, model_id: str, progress: float, done: bool, error: str = "", force: bool = False) -> None:
        job = self._jobs.get(model_id)
        if job is not None:
            job.progress = progress
        now = time.monotonic()
        if not (force or done) and now - self._last_emit.get(model_id, 0.0) < self.throttle_s:
            return
        self._last_emit[model_id] = now
        if self._publish is None:
            return
        data: dict[str, Any] = {"id": model_id, "progress": round(progress, 3), "done": done}
        if error:
            data["error"] = error
        try:
            self._publish("model.download", data)
        except Exception:
            log.exception("событие model.download не отправилось")

    async def _run(self, spec: ModelSpec) -> None:
        self._errors.pop(spec.id, None)
        self._emit(spec.id, 0.0, False, force=True)
        progress = 0.0
        try:
            log.info("модель %s: скачиваю %s (%.1f МБ)", spec.id, spec.url, spec.size_mb)
            part = await self._fetch(spec)
            progress = 1.0
            job = self._jobs.get(spec.id)
            if job is not None:
                job.stage = "install"
            self._emit(spec.id, 1.0, False, force=True)
            await asyncio.to_thread(self._install, spec, part)
        except asyncio.CancelledError:
            self._errors[spec.id] = "Скачивание отменено"
            self._emit(spec.id, progress, True, error="Скачивание отменено")
            raise
        except Exception as e:
            message = _human_error(e)
            log.warning("модель %s не скачалась: %s", spec.id, message)
            self._errors[spec.id] = message
            job = self._jobs.get(spec.id)
            self._emit(spec.id, job.progress if job else progress, True, error=message)
            raise DownloadError(message) from e
        finally:
            self._jobs.pop(spec.id, None)
        log.info("модель %s установлена в %s", spec.id, self.root / spec.folder)
        crashguard.reset()  # свежая модель — заслуживает новой попытки, даже если прежняя валила ядро
        self._emit(spec.id, 1.0, True)
        await self._notify("installed", spec.id)

    def _check_space(self, spec: ModelSpec, have: int) -> None:
        factor = self.space_factor if spec.archive else 1.05
        need = int(spec.size * factor) - have
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            return
        if need > 0 and free < need:
            raise DownloadError(
                f"не хватает места на диске: нужно ещё ≈{need / 1e6:.0f} МБ, свободно {free / 1e6:.0f} МБ"
            )

    async def _fetch(self, spec: ModelSpec) -> Path:
        tmp = self.root / ".download"
        tmp.mkdir(parents=True, exist_ok=True)
        part = tmp / f"{spec.id}.part"
        have = part.stat().st_size if part.exists() else 0
        if spec.size and have > spec.size:
            part.unlink()
            have = 0
        self._check_space(spec, have)
        digest = hashlib.sha256()
        if have:
            await asyncio.to_thread(_hash_file, part, digest)
        headers = {"Range": f"bytes={have}-"} if have else {}
        timeout = httpx.Timeout(30.0, read=60.0)
        async with (
            httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                transport=self.transport,
                headers={"User-Agent": "Jarvis-Core"},
            ) as client,
            client.stream("GET", spec.url, headers=headers) as resp,
        ):
            if have and resp.status_code == 416 and spec.size and have == spec.size:
                got = have  # уже скачано целиком
            else:
                if resp.status_code == 206 and have:
                    mode = "ab"
                elif resp.status_code == 200:
                    mode, have = "wb", 0
                    digest = hashlib.sha256()
                else:
                    raise DownloadError(f"сервер ответил {resp.status_code}")
                length = int(resp.headers.get("content-length") or 0)
                total = spec.size or (have + length) or 0
                got = have
                with part.open(mode) as f:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        f.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if spec.size and got > spec.size:
                            raise DownloadError("файл больше ожидаемого — скачивание прервано")
                        self._emit(spec.id, got / total if total else 0.0, False)
        if spec.size and got != spec.size:
            if got < spec.size:
                raise DownloadError(f"скачано {got} из {spec.size} байт — связь оборвалась, повторите")
            part.unlink(missing_ok=True)
            raise DownloadError(f"размер не совпал: {got} байт вместо {spec.size}")
        if spec.sha256 and digest.hexdigest() != spec.sha256:
            part.unlink(missing_ok=True)
            raise DownloadError("контрольная сумма SHA-256 не совпала — файл повреждён или подменён")
        return part

    def _install(self, spec: ModelSpec, part: Path) -> None:
        """Скачанный файл → папка модели (поток ввода-вывода). Старая папка заменяется атомарно."""
        root = self.root
        target = root / spec.folder
        try:
            if not spec.archive:
                target.mkdir(parents=True, exist_ok=True)
                os.replace(part, target / spec.filename)
                return
            work = Path(tempfile.mkdtemp(prefix=f".unpack-{spec.id}-", dir=root))
            try:
                safe_extract(part, work)
                entries = list(work.iterdir())
                src = entries[0] if len(entries) == 1 and entries[0].is_dir() else work
                if not folder_complete(spec, src):
                    raise DownloadError("в архиве нет нужных файлов модели")
                for extra in ("test_wavs",):
                    shutil.rmtree(src / extra, ignore_errors=True)
                old: Path | None = None
                if target.exists():
                    old = root / f".old-{spec.id}-{uuid.uuid4().hex[:8]}"
                    os.replace(target, old)
                os.replace(src, target)
                if old is not None:
                    shutil.rmtree(old, ignore_errors=True)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        finally:
            part.unlink(missing_ok=True)

    # --- удаление
    async def delete(self, model_id: str) -> None:
        spec = self.spec(model_id)
        job = self._jobs.get(spec.id)
        if job is not None:
            job.task.cancel()
            with contextlib.suppress(BaseException):
                await job.task
        await self._notify("deleting", spec.id)
        await asyncio.to_thread(self._remove, spec)
        self._errors.pop(spec.id, None)
        crashguard.reset()
        log.info("модель %s удалена", spec.id)
        await self._notify("deleted", spec.id)

    def _remove(self, spec: ModelSpec) -> None:
        root = self.root
        (root / ".download" / f"{spec.id}.part").unlink(missing_ok=True)
        target = root / spec.folder
        if not spec.archive:
            (target / spec.filename).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                target.rmdir()
            return
        if target.exists():
            trash = root / f".old-{spec.id}-{uuid.uuid4().hex[:8]}"
            os.replace(target, trash)
            shutil.rmtree(trash, ignore_errors=True)


def _consume_error(task: "asyncio.Task[None]") -> None:
    """Ошибка фоновой загрузки уже в событии и в info() — в журнал asyncio её не нести."""
    if not task.cancelled():
        task.exception()


def _human_error(e: BaseException) -> str:
    if isinstance(e, DownloadError):
        return str(e)
    if isinstance(e, httpx.TimeoutException):
        return "сервер не отвечает (таймаут) — проверьте интернет"
    if isinstance(e, httpx.ConnectError):
        return "нет связи с GitHub — проверьте интернет"
    if isinstance(e, httpx.HTTPError):
        return f"ошибка сети: {e}"
    if isinstance(e, OSError):
        return f"ошибка диска: {e.strerror or e}"
    return f"{type(e).__name__}: {e}"


def auto_download_allowed(ctx: "AppContext", override: bool | None = None) -> bool:
    """Качать недостающие модели самим? Да, кроме платформы-заглушки (тесты, демо без сети).

    JARVIS_VOICE_AUTODOWNLOAD=1|0 переопределяет; override — явный выбор (тесты).
    """
    if override is not None:
        return override
    env = os.environ.get("JARVIS_VOICE_AUTODOWNLOAD", "").strip().casefold()
    if env:
        return env not in ("0", "false", "no", "off")
    return getattr(ctx.platform, "name", "") != "fake"


# --- один менеджер на ядро ------------------------------------------------------------------------

_managers: "weakref.WeakKeyDictionary[AppContext, ModelManager]" = weakref.WeakKeyDictionary()


def manager_for(ctx: "AppContext") -> ModelManager:
    """Общий менеджер моделей ядра (им пользуются и распознавание, и синтез)."""
    manager = _managers.get(ctx)
    if manager is None:
        manager = ModelManager(publish=ctx.bus.publish)
        _managers[ctx] = manager
    return manager
