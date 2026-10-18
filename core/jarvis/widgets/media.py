"""Медиа и громкость: общие действия для виджета, пульта и Telegram; слежение за «сейчас играет».

MediaWatcher раз в ~2 с спрашивает платформу, что играет и не открыта ли игра во весь экран, и публикует
media.changed (MediaInfo) и fullscreen.changed {fullscreen} только при изменениях. Позиция трека меняется
постоянно — событие из-за неё приходит лишь при перемотке.
"""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from jarvis.platform.base import MediaInfo
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

MEDIA_KEYS = ("play_pause", "next", "prev", "stop")
VOLUME_ACTIONS = ("volume_up", "volume_down", "mute", "unmute", "volume")
ACTIONS = MEDIA_KEYS + VOLUME_ACTIONS
VOLUME_STEP = 10
POLL_INTERVAL = 2.0
SEEK_TOLERANCE = 3.0  # с: расхождение позиции, которое считаем перемоткой


def _clamp(level: Any) -> int:
    try:
        return max(0, min(100, round(float(level))))
    except (TypeError, ValueError) as e:
        raise UserFacingError("Громкость — число от 0 до 100") from e


async def media_action(
    ctx: "AppContext", action: str, *, level: Any = None, step: int = VOLUME_STEP
) -> dict[str, Any]:
    """Кнопки плеера и громкости. mute — переключить звук; volume — установить level (0..100).

    Возвращает {ok, action} и для громкости ещё {volume, muted}. Неизвестное действие — UserFacingError.
    """
    platform = ctx.platform
    if action in MEDIA_KEYS:
        await ctx.run_platform(platform.media_key, action)  # type: ignore[arg-type]
        refresh_media_soon(ctx)
        return {"ok": True, "action": action}

    step = max(1, min(50, int(step or VOLUME_STEP)))

    def run() -> tuple[int, bool]:
        # всё в одном вызове потока платформы: громкость и звук меняются согласованно
        if action == "volume":
            volume = platform.volume_set(_clamp(level))
            if volume > 0 and platform.is_muted():
                platform.mute_set(False)
        elif action == "volume_up":
            volume = platform.volume_change(step)
            if platform.is_muted():
                platform.mute_set(False)  # как клавиша громкости в Windows: громче — значит со звуком
        elif action == "volume_down":
            volume = platform.volume_change(-step)
        elif action == "mute":
            platform.mute_set(None)
            volume = platform.volume_get()
        else:  # unmute
            platform.mute_set(False)
            volume = platform.volume_get()
        return volume, platform.is_muted()

    if action not in VOLUME_ACTIONS:
        raise UserFacingError(f"Неизвестное действие с медиа: {action}")
    if action == "volume" and level is None:
        raise UserFacingError("Не указана громкость")
    volume, muted = await ctx.run_platform(run)
    return {"ok": True, "action": action, "volume": volume, "muted": muted}


def refresh_media_soon(ctx: "AppContext") -> None:
    """После нажатия кнопки плеера — обновить «сейчас играет», не дожидаясь очередного опроса."""
    widgets = getattr(ctx, "widgets", None)
    poke = getattr(widgets, "refresh_media_soon", None)
    if callable(poke):
        poke()


def _signature(info: MediaInfo) -> tuple[Any, ...]:
    thumb = info.thumbnail
    return (
        info.title,
        info.artist,
        info.album,
        info.app,
        info.status,
        round(info.duration_s),
        len(thumb),
        hash(thumb) if thumb else 0,
    )


class MediaWatcher:
    """Опрос платформы в фоне: «сейчас играет» и полноэкранные приложения."""

    def __init__(self, ctx: "AppContext", interval: float = POLL_INTERVAL) -> None:
        self.ctx = ctx
        self.interval = interval
        self.fullscreen = False
        self.media: MediaInfo | None = None
        self._task: asyncio.Task[Any] | None = None
        self._wake: asyncio.Event | None = None
        self._signature: tuple[Any, ...] | None = None
        self._position: float | None = None
        self._position_at = 0.0

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._wake = asyncio.Event()
        self._task = self.ctx.spawn(self._run(), "media-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def poke(self, delay: float = 0.35) -> None:
        """Опросить вне очереди (через delay с — плееру нужно время, чтобы сменить трек)."""
        loop = self.ctx.loop
        wake = self._wake
        if loop is None or loop.is_closed() or wake is None:
            return
        loop.call_soon_threadsafe(lambda: loop.call_later(delay, wake.set))

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures in (1, 10) or failures % 300 == 0:
                    log.warning("опрос медиа не удался (%s раз подряд): %s", failures, e)
            timeout = self.interval * min(failures + 1, 15)  # при ошибках — реже
            wake = self._wake
            if wake is None:
                await asyncio.sleep(timeout)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)
            wake.clear()

    async def poll_once(self) -> None:
        platform = self.ctx.platform

        def probe() -> tuple[MediaInfo | None, bool, bool | None, Exception | None]:
            error: Exception | None = None
            media: MediaInfo | None = None
            media_ok = True
            try:
                media = platform.now_playing()
            except Exception as e:
                media_ok, error = False, e
            fullscreen: bool | None = None
            try:
                fullscreen = bool(platform.is_fullscreen_app())
            except Exception as e:
                error = error or e
            return media, media_ok, fullscreen, error

        media, media_ok, fullscreen, error = await self.ctx.run_platform(probe)
        if fullscreen is not None and fullscreen != self.fullscreen:
            self.fullscreen = fullscreen
            self.ctx.bus.publish("fullscreen.changed", {"fullscreen": fullscreen})
        if media_ok:
            self._check_media(media)
        if error is not None:
            raise error

    def _check_media(self, media: MediaInfo | None) -> None:
        info = media if media is not None else MediaInfo(status="stopped")
        now = time.monotonic()
        signature = _signature(info)
        changed = signature != self._signature
        position = float(info.position_s or 0.0)
        if not changed and self._position is not None and position != self._position:
            # позицию плееры обновляют рывками; перемотка — скачок назад или вперёд быстрее времени
            delta = position - self._position
            elapsed = now - self._position_at if info.status == "playing" else 0.0
            if delta < -SEEK_TOLERANCE or delta > elapsed + SEEK_TOLERANCE:
                changed = True
        if self._position is None or position != self._position:
            self._position = position
            self._position_at = now
        self.media = info
        if changed:
            self._signature = signature
            self.ctx.bus.publish("media.changed", info.to_json())
