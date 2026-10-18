"""«Сейчас играет»: WinRT GlobalSystemMediaTransportControlsSessionManager (то же, что показывает Windows
в плашке громкости): название, исполнитель, альбом, программа, состояние, позиция и обложка.

WinRT работает в собственном фоновом потоке в многопоточном апартаменте (MTA): поток платформы — STA,
и ждать в нём асинхронные операции WinRT нельзя (pywinrt запрещает там блокирующий .get(): завершение может
прийти через очередь сообщений STA, и ожидание зависнет). Поток платформы ждёт результат не дольше
CALL_TIMEOUT_S; если служба медиа зависла — не спрашивает её BACKOFF_S секунд. Операции WinRT ждём через
обработчик completed и threading.Event. Обложка читается DataReader'ом один раз на трек (кэш), большая —
уменьшается Pillow.
Любая ошибка — None («ничего не играет»), подробности — в журнал.
"""

import base64
import contextlib
import datetime as dt
import io
import logging
import ntpath
import queue
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from jarvis.platform.base import MediaInfo

log = logging.getLogger(__name__)

T = TypeVar("T")

REQUEST_TIMEOUT_S = 2.0
PROPS_TIMEOUT_S = 1.5
THUMB_TIMEOUT_S = 1.5
CALL_TIMEOUT_S = 4.0  # дольше поток платформы не ждёт «сейчас играет»
BACKOFF_S = 30.0  # служба медиа не ответила — столько её не трогаем
MAX_THUMB_BYTES = 4 * 1024 * 1024  # больше — не читаем
MAX_INLINE_BYTES = 200 * 1024  # такую обложку отдаём как есть
MAX_THUMB_SIDE = 512
THUMB_CACHE = 16
THUMB_RETRY_S = 3.0  # обложки ещё нет (плеер грузит её позже) — спросить снова через
MAX_EXTRAPOLATE_S = 6 * 3600.0

STATUS = {0: "stopped", 3: "stopped", 4: "playing", 5: "paused"}  # Closed, Stopped, Playing, Paused
PLAYING = 4

# AUMID / source_app_user_model_id → имя для человека (порядок важен: Яндекс Музыка раньше Яндекс Браузера)
_APP_NAMES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name)
    for pattern, name in (
        (r"spotify", "Spotify"),
        (r"yandex.*music|music.*yandex|яндекс.*музык", "Яндекс Музыка"),
        (r"yandex|яндекс", "Яндекс Браузер"),
        (r"^chrome(?:$|[._])|google.?chrome", "Chrome"),
        (r"msedge|microsoftedge", "Edge"),
        (r"firefox", "Firefox"),
        (r"opera.?gx", "Opera GX"),
        (r"\bopera\b", "Opera"),
        (r"brave", "Brave"),
        (r"vivaldi", "Vivaldi"),
        (r"zunemusic|windows\.?mediaplayer|microsoft\.media\.player", "Медиаплеер"),
        (r"zunevideo", "Кино и ТВ"),
        (r"\bvlc\b|videolan", "VLC"),
        (r"aimp", "AIMP"),
        (r"foobar2000", "foobar2000"),
        (r"telegram", "Telegram"),
        (r"discord", "Discord"),
        (r"applemusic", "Apple Music"),
        (r"itunes", "iTunes"),
        (r"amazon.?music", "Amazon Music"),
        (r"deezer", "Deezer"),
        (r"tidal", "TIDAL"),
        (r"soundcloud", "SoundCloud"),
        (r"\bvk\b|vk.?music|vkmusic", "VK Музыка"),
        (r"sberzvuk|\bzvuk\b", "Звук"),
        (r"potplayer", "PotPlayer"),
        (r"mpc-?hc|mpc-?be", "MPC"),
        (r"winamp", "Winamp"),
        (r"musicbee", "MusicBee"),
        (r"\bplex", "Plex"),
        (r"\bkodi\b", "Kodi"),
        (r"netflix", "Netflix"),
    )
)
_HASH_AUMID = re.compile(r"^[0-9A-Fa-f]{16}$")  # Firefox: AUMID — хэш пути установки (308046B0AF4A39CB)
_PUBLISHER = re.compile(r"_[a-z0-9]{13}$")  # «_8wekyb3d8bbwe» у пакетов Microsoft Store
_SQUIRREL = re.compile(r"^com\.squirrel\.[^.]+\.([^.]+)$", re.IGNORECASE)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def friendly_app_name(aumid: str) -> str:
    """«Spotify.exe» → «Spotify», «Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic» → «Медиаплеер»,
    «Chrome» → «Chrome», неизвестное — читаемое имя из AUMID («Contoso.CoolPlayer_…!App» → «Cool Player»)."""
    s = (aumid or "").strip()
    if not s:
        return ""
    for pattern, name in _APP_NAMES:
        if pattern.search(s):
            return name
    if _HASH_AUMID.match(s):
        return "Firefox"
    base = s.split("!", 1)[0]
    base = ntpath.basename(base.replace("/", "\\"))
    base = _PUBLISHER.sub("", base)
    base = re.sub(r"\.exe$", "", base, flags=re.IGNORECASE)
    if m := _SQUIRREL.match(base):
        base = m.group(1)
    parts = [p for p in base.split(".") if p]
    if parts:
        base = parts[-1]
    base = _CAMEL.sub(" ", base).replace("_", " ").replace("-", " ")
    base = " ".join(base.split())
    return (base[:1].upper() + base[1:]) if base else s


def sniff_image_mime(data: bytes) -> str:
    """Тип картинки по первым байтам; не картинка — ""."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def thumbnail_data_url(data: bytes, content_type: str = "") -> str:
    """Байты обложки → data:image/…;base64,… Небольшие PNG/JPEG/GIF/WebP — как есть, остальное —
    уменьшается до MAX_THUMB_SIDE и пережимается в JPEG. Не картинка или не вышло — ""."""
    if not data:
        return ""
    mime = sniff_image_mime(data) or (content_type if content_type.startswith("image/") else "")
    if not mime:
        return ""
    if len(data) <= MAX_INLINE_BYTES and mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.thumbnail((MAX_THUMB_SIDE, MAX_THUMB_SIDE))
            rgb = img.convert("RGB")
            out = io.BytesIO()
            rgb.save(out, "JPEG", quality=85, optimize=True)
    except Exception as e:
        log.debug("обложка не пережата: %s", e)
        return ""
    return f"data:image/jpeg;base64,{base64.b64encode(out.getvalue()).decode('ascii')}"


def timeline_position(
    position_s: float,
    duration_s: float,
    playing: bool,
    last_updated: dt.datetime | None,
    now: dt.datetime,
    rate: float = 1.0,
) -> float:
    """Позиция «сейчас»: плееры обновляют её рывками, поэтому при воспроизведении прибавляем время с момента
    обновления (last_updated). Результат — в пределах 0..duration (если длительность известна)."""
    pos = max(0.0, float(position_s))
    if playing and last_updated is not None and last_updated.year > 2000:
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=dt.UTC)
        elapsed = (now - last_updated).total_seconds()
        if 0 < elapsed < MAX_EXTRAPOLATE_S:
            pos += elapsed * (rate if rate and rate > 0 else 1.0)
    if duration_s > 0:
        pos = min(pos, float(duration_s))
    return pos


def wait_async(operation: Any, timeout: float) -> Any:
    """Дождаться IAsyncOperation WinRT в любом апартаменте (в т. ч. STA): обработчик completed + Event."""
    done = threading.Event()

    def on_done(_operation: Any, _status: Any) -> None:
        done.set()

    operation.completed = on_done
    if not done.wait(timeout):
        with contextlib.suppress(Exception):
            operation.cancel()
        raise TimeoutError("операция WinRT не завершилась вовремя")
    return operation.get_results()


def _seconds(value: Any) -> float:
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _init_mta() -> None:
    """Фоновый поток WinRT — в MTA (вне Windows библиотек нет — ничего не делаем)."""
    try:
        from winrt.runtime import ApartmentType, init_apartment  # type: ignore[import-not-found]

        init_apartment(ApartmentType.MULTI_THREADED)
        return
    except ImportError:
        return
    except Exception as e:  # апартамент уже задан — пробуем через pywin32
        log.debug("init_apartment: %r", e)
    with contextlib.suppress(Exception):
        import pythoncom  # type: ignore[import-not-found]

        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)


class MtaWorker:
    """Один фоновый поток (MTA) с очередью задач. Зависшая задача — тайм-аут у вызывающего; следующий вызов
    получит новый поток, а старый завершится, когда (если) задача вернётся."""

    def __init__(self, init: Callable[[], None] = _init_mta, name: str = "jarvis-winrt") -> None:
        self._init = init
        self._name = name
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[Future[Any], Callable[[], Any]] | None] | None = None

    def _run(self, jobs: "queue.Queue[tuple[Future[Any], Callable[[], Any]] | None]") -> None:
        try:
            self._init()
        except Exception as e:
            log.warning("поток WinRT: апартамент не инициализирован: %r", e)
        while True:
            job = jobs.get()
            if job is None:
                return
            future, fn = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as e:
                future.set_exception(e)

    def call(self, fn: Callable[[], T], timeout: float) -> T:
        with self._lock:
            if self._queue is None:
                self._queue = queue.Queue()
                threading.Thread(target=self._run, args=(self._queue,), name=self._name, daemon=True).start()
            jobs = self._queue
        future: Future[T] = Future()
        jobs.put((future, fn))
        try:
            return future.result(timeout)
        except TimeoutError:
            if future.done():  # TimeoutError бросила сама задача
                raise
            future.cancel()
            with self._lock:
                if self._queue is jobs:
                    self._queue = None
                    jobs.put(None)  # старый поток выйдет после зависшей задачи
            raise

    def stop(self) -> None:
        with self._lock:
            jobs, self._queue = self._queue, None
        if jobs is not None:
            jobs.put(None)


class Media:
    """Медиасессии Windows. Всё WinRT — в потоке MtaWorker; менеджер кэшируется там же; ошибка — None."""

    def __init__(self, worker: MtaWorker | None = None) -> None:
        self._local = threading.local()
        self._thumbs: OrderedDict[tuple[str, ...], tuple[str, float]] = OrderedDict()
        self._thumbs_lock = threading.Lock()
        self._worker = worker or MtaWorker()
        self._skip_until = 0.0

    def _manager(self) -> Any:
        manager = getattr(self._local, "manager", None)
        if manager is None:
            from winrt.windows.media.control import (  # type: ignore[import-not-found]
                GlobalSystemMediaTransportControlsSessionManager as Manager,
            )

            manager = wait_async(Manager.request_async(), REQUEST_TIMEOUT_S)
            self._local.manager = manager
        return manager

    def now_playing(self) -> MediaInfo | None:
        if time.monotonic() < self._skip_until:
            return None
        try:
            return self._worker.call(self._now_playing_safe, CALL_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "служба медиа Windows не ответила за %.0f с — пауза %.0f с", CALL_TIMEOUT_S, BACKOFF_S
            )
            self._skip_until = time.monotonic() + BACKOFF_S
            return None
        except Exception as e:
            log.debug("медиасессия не прочитана: %r", e)
            return None

    def _now_playing_safe(self) -> MediaInfo | None:
        """В потоке WinRT: ошибка — сбросить менеджер (в следующий раз — заново) и вернуть None."""
        try:
            return self._now_playing()
        except Exception as e:
            log.debug("медиасессия не прочитана: %r", e)
            self._local.manager = None
            return None

    @staticmethod
    def _status(session: Any) -> int:
        try:
            return int(session.get_playback_info().playback_status)
        except Exception:
            return -1

    def _pick_session(self, manager: Any) -> Any:
        """Текущая сессия, если играет; иначе любая играющая; иначе текущая или первая."""
        current = manager.get_current_session()
        if current is not None and self._status(current) == PLAYING:
            return current
        sessions = list(manager.get_sessions() or [])
        for session in sessions:
            if self._status(session) == PLAYING:
                return session
        if current is not None:
            return current
        return sessions[0] if sessions else None

    def _now_playing(self) -> MediaInfo | None:
        session = self._pick_session(self._manager())
        if session is None:
            return None
        aumid = str(session.source_app_user_model_id or "")
        props = wait_async(session.try_get_media_properties_async(), PROPS_TIMEOUT_S)
        status_code = self._status(session)
        status = STATUS.get(status_code, "unknown")
        info = MediaInfo(app=friendly_app_name(aumid), status=status)  # type: ignore[arg-type]
        if props is not None:
            info.title = str(props.title or "")
            info.artist = str(props.artist or props.album_artist or "")
            info.album = str(props.album_title or "")
        with contextlib.suppress(Exception):
            rate = 1.0
            with contextlib.suppress(Exception):
                rate = float(session.get_playback_info().playback_rate or 1.0)
            timeline = session.get_timeline_properties()
            start = _seconds(timeline.start_time)
            duration = max(0.0, _seconds(timeline.end_time) - start)
            position = max(0.0, _seconds(timeline.position) - start)
            info.duration_s = round(duration, 1)
            info.position_s = round(
                timeline_position(
                    position,
                    duration,
                    status_code == PLAYING,
                    getattr(timeline, "last_updated_time", None),
                    dt.datetime.now(dt.UTC),
                    rate,
                ),
                1,
            )
        if props is not None:
            info.thumbnail = self._thumbnail((aumid, info.title, info.artist, info.album), props)
        return info

    def _thumbnail(self, key: tuple[str, ...], props: Any) -> str:
        now = time.monotonic()
        with self._thumbs_lock:
            cached = self._thumbs.get(key)
            if cached is not None and (cached[0] or now - cached[1] < THUMB_RETRY_S):
                self._thumbs.move_to_end(key)
                return cached[0]
        thumb = ""
        ref = None
        with contextlib.suppress(Exception):
            ref = props.thumbnail
        if ref is not None:
            try:
                thumb = thumbnail_data_url(*self._read_stream(ref))
            except Exception as e:
                log.debug("обложка не прочитана: %r", e)
        with self._thumbs_lock:  # поток WinRT, переживший тайм-аут, мог бы писать сюда одновременно с новым
            self._thumbs[key] = (thumb, now)
            self._thumbs.move_to_end(key)
            while len(self._thumbs) > THUMB_CACHE:
                self._thumbs.popitem(last=False)
        return thumb

    @staticmethod
    def _read_stream(ref: Any) -> tuple[bytes, str]:
        """IRandomAccessStreamReference → (байты, content type) через DataReader."""
        from winrt.windows.storage.streams import (  # type: ignore[import-not-found]
            DataReader,
            InputStreamOptions,
        )

        stream = wait_async(ref.open_read_async(), THUMB_TIMEOUT_S)
        try:
            size = int(stream.size or 0)
            if size > MAX_THUMB_BYTES:
                return b"", ""
            content_type = ""
            with contextlib.suppress(Exception):
                content_type = str(stream.content_type or "")
            reader = DataReader(stream.get_input_stream_at(0))
            try:
                reader.input_stream_options = InputStreamOptions.PARTIAL
                want = size or MAX_THUMB_BYTES
                chunks: list[bytes] = []
                total = 0
                while total < want:
                    loaded = int(
                        wait_async(reader.load_async(min(256 * 1024, want - total)), THUMB_TIMEOUT_S)
                    )
                    if loaded <= 0:
                        break
                    buf = bytearray(loaded)
                    reader.read_bytes(buf)
                    chunks.append(bytes(buf))
                    total += loaded
            finally:
                reader.close()
        finally:
            stream.close()
        return b"".join(chunks), content_type
