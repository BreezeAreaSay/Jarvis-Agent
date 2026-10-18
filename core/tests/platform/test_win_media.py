"""«Сейчас играет»: имена программ по AUMID, обложки, позиция, ожидание WinRT (фейки WinRT — на Linux)."""

import base64
import datetime as dt
import io
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from jarvis.platform.windows import media as m


@pytest.mark.parametrize(
    ("aumid", "name"),
    [
        ("Spotify.exe", "Spotify"),
        ("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "Spotify"),
        ("Chrome", "Chrome"),
        ("Chrome._crx_cinhimbnkkaeohfgghhklpknlkffjgod", "Chrome"),
        ("MSEdge", "Edge"),
        ("308046B0AF4A39CB", "Firefox"),
        ("Mozilla.Firefox", "Firefox"),
        ("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic", "Медиаплеер"),
        ("Microsoft.ZuneVideo_8wekyb3d8bbwe!Microsoft.ZuneVideo", "Кино и ТВ"),
        ("ru.yandex.desktop.music", "Яндекс Музыка"),
        ("Yandex.Music", "Яндекс Музыка"),
        ("Yandex", "Яндекс Браузер"),
        ("OperaSoftware.OperaGX", "Opera GX"),
        ("Opera", "Opera"),
        ("org.videolan.vlc", "VLC"),
        ("VLC.exe", "VLC"),
        ("AIMP", "AIMP"),
        ("foobar2000.exe", "foobar2000"),
        ("com.squirrel.Discord.Discord", "Discord"),
        ("TelegramDesktop", "Telegram"),
        ("AppleInc.AppleMusicWin_nzyj5cx40ttqa!App", "Apple Music"),
        ("Contoso.CoolPlayer_abcdefghijklm!App", "Cool Player"),
        (r"C:\Program Files\Foo Player\foo.exe", "Foo"),
        ("ChromeCanary", "Chrome Canary"),
        ("", ""),
    ],
)
def test_friendly_app_name(aumid: str, name: str) -> None:
    assert m.friendly_app_name(aumid) == name


def _png(size: tuple[int, int], noise: bool = False) -> bytes:
    if noise:
        import os

        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        img = Image.new("RGB", size, (200, 30, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_sniff_image_mime() -> None:
    assert m.sniff_image_mime(_png((2, 2))) == "image/png"
    assert m.sniff_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert m.sniff_image_mime(b"GIF89a....") == "image/gif"
    assert m.sniff_image_mime(b"BM\x00\x00") == "image/bmp"
    assert m.sniff_image_mime(b"RIFF\x10\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert m.sniff_image_mime(b"hello") == ""
    assert m.sniff_image_mime(b"") == ""


def test_small_thumbnail_kept_as_is() -> None:
    data = _png((64, 64))
    url = m.thumbnail_data_url(data, "image/png")
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data
    # content type плеера врёт — верим байтам
    assert m.thumbnail_data_url(data, "application/octet-stream").startswith("data:image/png;base64,")


def test_large_thumbnail_is_downscaled_to_jpeg() -> None:
    data = _png((900, 700), noise=True)
    assert len(data) > m.MAX_INLINE_BYTES
    url = m.thumbnail_data_url(data, "image/png")
    assert url.startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(img.size) <= m.MAX_THUMB_SIDE
    assert img.size == (512, 398)


def test_thumbnail_rejects_garbage() -> None:
    assert m.thumbnail_data_url(b"", "image/png") == ""
    assert m.thumbnail_data_url(b"not an image", "") == ""
    assert m.thumbnail_data_url(b"x" * (m.MAX_INLINE_BYTES + 10), "image/png") == ""  # не открывается


def test_timeline_position() -> None:
    now = dt.datetime(2026, 10, 18, 12, 0, 10, tzinfo=dt.UTC)
    updated = now - dt.timedelta(seconds=10)
    assert m.timeline_position(60, 200, True, updated, now) == 70
    assert m.timeline_position(60, 200, False, updated, now) == 60  # пауза — позиция не идёт
    assert m.timeline_position(195, 200, True, updated, now) == 200  # не дальше конца
    assert m.timeline_position(60, 200, True, updated, now, rate=2.0) == 80
    naive = updated.replace(tzinfo=None)  # без часового пояса — UTC
    assert m.timeline_position(60, 200, True, naive, now) == 70
    zero = dt.datetime(1601, 1, 1, tzinfo=dt.UTC)  # «не обновлялось»
    assert m.timeline_position(60, 200, True, zero, now) == 60
    future = now + dt.timedelta(seconds=30)
    assert m.timeline_position(60, 200, True, future, now) == 60
    assert m.timeline_position(-5, 0, True, None, now) == 0
    assert m.timeline_position(500, 0, False, None, now) == 500  # длительность неизвестна


class FakeOp:
    """IAsyncOperation: обработчик completed вызывается сразу, из другого потока или никогда."""

    def __init__(self, result: Any = None, mode: str = "now", error: Exception | None = None) -> None:
        self.result = result
        self.mode = mode
        self.error = error
        self.cancelled = False
        self._handler: Any = None

    @property
    def completed(self) -> Any:
        return self._handler

    @completed.setter
    def completed(self, handler: Any) -> None:
        self._handler = handler
        if self.mode == "now":
            handler(self, 1)
        elif self.mode == "thread":
            threading.Timer(0.02, handler, args=(self, 1)).start()

    def cancel(self) -> None:
        self.cancelled = True

    def get_results(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


def test_wait_async() -> None:
    assert m.wait_async(FakeOp(42), 1.0) == 42
    assert m.wait_async(FakeOp("late", mode="thread"), 1.0) == "late"
    hung = FakeOp(mode="never")
    with pytest.raises(TimeoutError):
        m.wait_async(hung, 0.05)
    assert hung.cancelled
    with pytest.raises(OSError):
        m.wait_async(FakeOp(error=OSError("E_FAIL")), 1.0)


class FakeInfo:
    def __init__(self, status: int) -> None:
        self.playback_status = status
        self.playback_rate = 1.0


class FakeTimeline:
    def __init__(self) -> None:
        self.start_time = dt.timedelta(0)
        self.end_time = dt.timedelta(seconds=214)
        self.position = dt.timedelta(seconds=63)
        self.last_updated_time = dt.datetime(1601, 1, 1, tzinfo=dt.UTC)


class FakeProps:
    def __init__(self, title: str = "Night Drive") -> None:
        self.title = title
        self.artist = "Neon Coast"
        self.album_artist = ""
        self.album_title = "Roads"
        self.thumbnail = object()


class FakeSession:
    def __init__(self, aumid: str, status: int, props: FakeProps | None = None) -> None:
        self.source_app_user_model_id = aumid
        self.status = status
        self.props = props

    def get_playback_info(self) -> FakeInfo:
        return FakeInfo(self.status)

    def get_timeline_properties(self) -> FakeTimeline:
        return FakeTimeline()

    def try_get_media_properties_async(self) -> FakeOp:
        return FakeOp(self.props)


class FakeManager:
    def __init__(self, current: FakeSession | None, sessions: list[FakeSession]) -> None:
        self.current = current
        self.sessions = sessions

    def get_current_session(self) -> FakeSession | None:
        return self.current

    def get_sessions(self) -> list[FakeSession]:
        return self.sessions


@pytest.fixture
def media() -> Iterator[m.Media]:
    instance = m.Media()
    yield instance
    instance._worker.stop()


def test_pick_session_prefers_playing() -> None:
    paused = FakeSession("Chrome", 5)
    playing = FakeSession("Spotify.exe", 4)
    media = m.Media()
    assert media._pick_session(FakeManager(paused, [paused, playing])) is playing
    assert media._pick_session(FakeManager(playing, [paused, playing])) is playing
    assert media._pick_session(FakeManager(paused, [paused])) is paused
    assert media._pick_session(FakeManager(None, [paused])) is paused
    assert media._pick_session(FakeManager(None, [])) is None


def test_now_playing_with_fake_winrt(media: m.Media, monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", 4, FakeProps())
    monkeypatch.setattr(media, "_manager", lambda: FakeManager(session, [session]))
    reads: list[Any] = []

    def read_stream(ref: Any) -> tuple[bytes, str]:
        reads.append(ref)
        return _png((32, 32)), "image/png"

    monkeypatch.setattr(media, "_read_stream", read_stream)
    info = media.now_playing()
    assert info is not None
    assert (info.title, info.artist, info.album, info.app, info.status) == (
        "Night Drive",
        "Neon Coast",
        "Roads",
        "Spotify",
        "playing",
    )
    assert info.duration_s == 214 and info.position_s == 63
    assert info.thumbnail.startswith("data:image/png;base64,")
    again = media.now_playing()
    assert again is not None and again.thumbnail == info.thumbnail
    assert len(reads) == 1  # обложка читается один раз на трек

    session.props = FakeProps("Другой трек")
    assert media.now_playing().title == "Другой трек"  # type: ignore[union-attr]
    assert len(reads) == 2


def test_now_playing_without_sessions_or_on_error(media: m.Media, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "_manager", lambda: FakeManager(None, []))
    assert media.now_playing() is None

    def broken() -> Any:
        raise OSError("WinRT недоступен")

    monkeypatch.setattr(media, "_manager", broken)
    assert media.now_playing() is None


def test_missing_thumbnail_is_retried_later(media: m.Media, monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession("Chrome", 4, FakeProps())
    monkeypatch.setattr(media, "_manager", lambda: FakeManager(session, [session]))
    calls: list[int] = []

    def read_stream(_ref: Any) -> tuple[bytes, str]:
        calls.append(1)
        return (b"", "") if len(calls) == 1 else (_png((8, 8)), "image/png")

    monkeypatch.setattr(media, "_read_stream", read_stream)
    clock = [1000.0]
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=lambda: clock[0]))  # только в модуле media
    assert media.now_playing().thumbnail == ""  # type: ignore[union-attr]
    assert media.now_playing().thumbnail == ""  # type: ignore[union-attr]  # рано спрашивать снова
    clock[0] += m.THUMB_RETRY_S + 1
    assert media.now_playing().thumbnail.startswith("data:image/png")  # type: ignore[union-attr]
    assert len(calls) == 2


def test_mta_worker_runs_jobs_in_its_thread() -> None:
    worker = m.MtaWorker(init=lambda: None)
    names: list[str] = []
    assert worker.call(lambda: names.append(threading.current_thread().name) or 7, 1.0) == 7
    assert names == ["jarvis-winrt"]

    def boom() -> None:
        raise ValueError("ошибка задачи")

    with pytest.raises(ValueError):
        worker.call(boom, 1.0)
    worker.stop()


def test_mta_worker_replaces_hung_thread() -> None:
    worker = m.MtaWorker(init=lambda: None)
    release = threading.Event()
    threads: list[threading.Thread] = []

    def hang() -> None:
        threads.append(threading.current_thread())
        release.wait(5)

    with pytest.raises(TimeoutError):
        worker.call(hang, 0.05)
    assert worker.call(lambda: threading.current_thread(), 1.0) is not threads[0]  # новый поток
    release.set()
    threads[0].join(2)
    assert not threads[0].is_alive()  # старый вышел, когда задача вернулась
    worker.stop()


def test_now_playing_backs_off_when_media_service_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    media = m.Media(worker=m.MtaWorker(init=lambda: None))
    monkeypatch.setattr(m, "CALL_TIMEOUT_S", 0.05)
    release = threading.Event()
    calls: list[int] = []

    def hang() -> None:
        calls.append(1)
        release.wait(5)

    monkeypatch.setattr(media, "_now_playing", hang)
    try:
        assert media.now_playing() is None
        assert media.now_playing() is None  # пауза: служба не трогается
        assert calls == [1]
    finally:
        release.set()
        media._worker.stop()
