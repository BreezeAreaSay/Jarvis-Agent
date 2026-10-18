"""«Блокировка, когда отходишь»: Windows блокируется, когда за компьютером никого нет.

Режим «камера»: раз в ~1,5 с кадр с веб-камеры в низком разрешении, лицо ищут каскады Хаара из OpenCV
(cv2.data.haarcascades: анфас и профиль). Нет лица дольше config.away_seconds (и, если включено, нет нажатий
и движений мыши) — ctx.platform.power("lock"). Повторно не блокирует, пока не увидит вас снова. Камера может
отпускаться между кадрами (config.camera_idle_release), чтобы индикатор не горел постоянно; камера занята
другой программой (созвон) — блокировки нет.
Режим «бездействие»: блокировать, если нет ввода дольше config.away_seconds.
Во время игр и видео во весь экран не блокирует (кроме config.force). Статус — {present, last_seen, …},
событие plugin.autolock.state.

OpenCV в установщик не входит (это +110 МБ): действие install_camera скачивает его в <data>/pylib.
"""

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from jarvis import extras
from jarvis.plugins.base import ConfigField, Option, Plugin, PluginAction, error_text
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

INTERVAL_S = 1.5
LOCKED_INTERVAL_S = 5.0  # после блокировки проверяем реже — человека всё равно нет
CAMERA_RETRY_S = 10.0
CASCADES = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")
CAMERA_PACKAGE = "opencv-python-headless>=4.10"
CAMERA_MODULE_HINT = (
    "Для режима «Камера» нужен модуль распознавания лица (~40 МБ) — нажмите «Скачать модуль камеры»"
)


class FaceDetector(Protocol):
    def detect(self) -> bool | None:
        """True — лицо в кадре, False — нет, None — кадр не получен."""
        ...

    def close(self) -> None: ...


class CameraFaceDetector:
    """Лицо в кадре веб-камеры (OpenCV, каскады Хаара). Звать из одного и того же потока."""

    def __init__(self, index: int = 0, release: bool = False, width: int = 320, height: int = 240) -> None:
        self.index = index
        self.release = release
        self.width = width
        self.height = height
        self._cap: Any = None
        self._cascades: list[Any] | None = None

    def _cv2(self) -> Any:
        extras.ensure_on_path()
        try:
            import cv2
        except ImportError as e:
            raise UserFacingError(CAMERA_MODULE_HINT) from e
        return cv2

    def _load_cascades(self, cv2: Any) -> list[Any]:
        cascades = [cv2.CascadeClassifier(cv2.data.haarcascades + name) for name in CASCADES]
        cascades = [c for c in cascades if not c.empty()]
        if not cascades:
            raise UserFacingError("не найдены каскады OpenCV для поиска лиц")
        return cascades

    def _open(self, cv2: Any) -> Any:
        api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, api)
        if not cap.isOpened():
            cap.release()
            raise UserFacingError("камера не открывается (занята или отключена)")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def detect(self) -> bool | None:
        cv2 = self._cv2()
        if self._cascades is None:
            self._cascades = self._load_cascades(cv2)
        fresh = self._cap is None
        if fresh:
            self._cap = self._open(cv2)
        try:
            frame = None
            for _ in range(5 if fresh else 1):  # после включения камере нужно несколько кадров на экспозицию
                ok, image = self._cap.read()
                if ok:
                    frame = image
            if frame is None:
                self.close()
                return None
            gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            images = [gray, cv2.flip(gray, 1)]  # профильный каскад видит только одну сторону — зеркалим
            for index, cascade in enumerate(self._cascades):
                for image in images if index else images[:1]:
                    faces = cascade.detectMultiScale(
                        image, scaleFactor=1.15, minNeighbors=5, minSize=(36, 36)
                    )
                    if len(faces):
                        return True
            return False
        finally:
            if self.release:
                self.close()

    def close(self) -> None:
        if self._cap is not None:
            with contextlib.suppress(Exception):
                self._cap.release()
            self._cap = None


class AutoLock(Plugin):
    id = "autolock"
    name = "Блокировка, когда отходишь"
    description = "Блокирует компьютер, когда камера не видит вас за столом. Вернулись — просто войдите."
    icon = "lock"
    color = ("#2DD4BF", "#0D9488")
    fields = (
        ConfigField(
            "mode",
            "select",
            "Как понять, что вы ушли",
            default="camera",
            options=(Option("camera", "Камера не видит лица"), Option("idle", "Нет нажатий и движений мыши")),
        ),
        ConfigField(
            "away_seconds", "number", "Блокировать через, с", default=20, min=5, max=600, integer=True
        ),
        ConfigField("camera_index", "number", "Номер камеры", default=0, min=0, max=9, integer=True),
        ConfigField(
            "camera_idle_release",
            "bool",
            "Выключать камеру между проверками",
            default=False,
            hint="Индикатор камеры почти не горит, но каждая проверка дольше",
        ),
        ConfigField("use_input", "bool", "Не блокировать, пока работают клавиатура и мышь", default=True),
        ConfigField("force", "bool", "Блокировать и во время игр и видео во весь экран", default=False),
    )
    actions = (
        PluginAction("pause", "Не блокировать час"),
        PluginAction("resume", "Снова следить"),
        PluginAction("check", "Проверить камеру"),
        PluginAction("install_camera", "Скачать модуль камеры"),
    )
    examples = ("не блокируй компьютер час", "включи автоблокировку")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        # тесты подставляют свой детектор и часы
        self.detector_factory: Callable[[dict[str, Any]], FaceDetector] | None = None
        self.clock: Callable[[], float] = time.monotonic
        self.wall: Callable[[], float] = time.time
        self._detector: FaceDetector | None = None
        self._detector_key: tuple[Any, ...] = ()
        self._camera_pool: ThreadPoolExecutor | None = None
        self._camera = "off"  # ok | error | off
        self._camera_error = ""
        self._camera_retry_at = 0.0
        self._present: bool | None = None
        self._last_seen = 0.0  # по self.clock
        self._armed = True
        self._locked_at = 0.0  # по self.wall
        self._locked_clock = -1.0
        self._paused_until = 0.0  # по self.wall
        self._published: dict[str, Any] = {}

    # --- жизненный цикл ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._reset()
        self.spawn(self._loop(), "watch")

    async def stop(self) -> None:
        await self._close_detector()
        if self._camera_pool is not None:
            self._camera_pool.shutdown(wait=False, cancel_futures=True)
            self._camera_pool = None
        self._camera = "off"

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        if config.get("mode") != old.get("mode") or config.get("camera_index") != old.get("camera_index"):
            await self._close_detector()
            self._camera = "off"
            self._camera_retry_at = 0.0
        self._reset()

    def _reset(self) -> None:
        self._last_seen = self.clock()
        self._armed = True
        self._present = True
        self._locked_clock = -1.0

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("автоблокировка: проверка упала")
            await asyncio.sleep(INTERVAL_S if self._armed else LOCKED_INTERVAL_S)

    # --- проверка --------------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused_until > self.wall()

    def _mark_seen(self, at: float) -> None:
        if at > self._last_seen:
            self._last_seen = at
        if at > self._locked_clock:
            self._armed = True  # человек вернулся после блокировки — снова следим

    async def tick(self) -> bool:
        """Одна проверка: обновить присутствие и, если надо, заблокировать. Вернёт True, если заблокировал."""
        if self.paused:
            self._publish_state()
            return False
        if self._paused_until:
            self._paused_until = 0.0
            self._reset()
        config = self.config
        now = self.clock()
        away = float(config.get("away_seconds") or 20)
        camera_mode = config.get("mode") == "camera"
        face: bool | None = None
        if camera_mode:
            face = await self._detect(config)
            if face:
                self._mark_seen(now)
        if not camera_mode or config.get("use_input"):
            idle = await self._idle_seconds()
            if idle is not None:
                self._mark_seen(now - idle)
        gone = now - self._last_seen
        self._present = gone < away
        locked = False
        can_lock = self._armed and gone >= away and (not camera_mode or face is False)
        if can_lock and (config.get("force") or not await self._fullscreen()):
            locked = await self._lock(now)
        self._publish_state()
        return locked

    async def _lock(self, now: float) -> bool:
        try:
            await self.ctx.run_platform(self.ctx.platform.power, "lock")
        except Exception as e:
            log.warning("автоблокировка: заблокировать не удалось (%s)", error_text(e))
            self.set_error(f"Не удалось заблокировать: {error_text(e)}")
            return False
        log.info("автоблокировка: никого нет — компьютер заблокирован")
        self._armed = False
        self._locked_clock = now
        self._locked_at = self.wall()
        self.changed()
        return True

    async def _idle_seconds(self) -> float | None:
        try:
            return float(await self.ctx.run_platform(self.ctx.platform.idle_seconds))
        except Exception:
            return None

    async def _fullscreen(self) -> bool:
        try:
            return bool(await self.ctx.run_platform(self.ctx.platform.is_fullscreen_app))
        except Exception:
            return False

    def _make_detector(self, config: dict[str, Any]) -> FaceDetector:
        if self.detector_factory is not None:
            return self.detector_factory(config)
        return CameraFaceDetector(
            int(config.get("camera_index") or 0), bool(config.get("camera_idle_release"))
        )

    async def _run_camera(self, fn: Callable[[], Any]) -> Any:
        if self._camera_pool is None:
            # камера (DirectShow/COM) живёт в одном и том же потоке
            self._camera_pool = ThreadPoolExecutor(1, thread_name_prefix="camera")
        return await asyncio.get_running_loop().run_in_executor(self._camera_pool, fn)

    async def _detect(self, config: dict[str, Any]) -> bool | None:
        if self._camera == "error" and self.clock() < self._camera_retry_at:
            return None
        key = (config.get("camera_index"), config.get("camera_idle_release"))
        if self._detector is not None and key != self._detector_key:
            await self._close_detector()
        try:
            if self._detector is None:
                self._detector = self._make_detector(config)
                self._detector_key = key
            face = await self._run_camera(self._detector.detect)
        except Exception as e:
            await self._close_detector()
            self._camera_failed(f"Камера недоступна: {error_text(e)}")
            return None
        if face is None:
            self._camera_failed("Камера не отдаёт кадры")
            return None
        if self._camera != "ok":
            self._camera = "ok"
            self._camera_error = ""
            self.set_error("")
            self.changed()
        return bool(face)

    def _camera_failed(self, message: str) -> None:
        self._camera_retry_at = self.clock() + CAMERA_RETRY_S
        if self._camera != "error" or self._camera_error != message:
            log.info("автоблокировка: %s", message)
            self._camera = "error"
            self._camera_error = message
            self.set_error(message)
            self.changed()

    async def _close_detector(self) -> None:
        detector, self._detector = self._detector, None
        if detector is not None:
            with contextlib.suppress(Exception):
                await self._run_camera(detector.close)

    # --- статус ---------------------------------------------------------------------------------------
    def _last_seen_wall(self) -> float:
        return self.wall() - max(0.0, self.clock() - self._last_seen)

    def status(self) -> dict[str, Any]:
        config = self.config
        return {
            "mode": config.get("mode"),
            "present": self._present,
            "last_seen": round(self._last_seen_wall(), 1),
            "away_s": round(max(0.0, self.clock() - self._last_seen), 1),
            "locked_at": self._locked_at,
            "armed": self._armed,
            "paused_until": self._paused_until if self.paused else 0.0,
            "camera": self._camera if config.get("mode") == "camera" else "off",
        }

    def _publish_state(self) -> None:
        state = {
            "present": self._present,
            "locked": not self._armed,
            "paused": self.paused,
            "camera": self._camera,
        }
        if state != self._published:
            self._published = state
            self.publish("state", {**state, "last_seen": round(self._last_seen_wall(), 1)})
            self.changed()

    # --- действия и команды ---------------------------------------------------------------------------
    async def act_pause(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            minutes = float(args.get("minutes") or 60)
        except (TypeError, ValueError):
            raise UserFacingError("Сколько минут не блокировать — нужно число") from None
        minutes = max(1.0, min(minutes, 24 * 60))
        self._paused_until = self.wall() + minutes * 60
        self._publish_state()
        self.changed()
        return {"paused_until": self._paused_until}

    async def act_resume(self, args: dict[str, Any]) -> dict[str, Any]:
        self._paused_until = 0.0
        self._reset()
        self._publish_state()
        self.changed()
        return {"paused_until": 0.0}

    async def act_install_camera(self, args: dict[str, Any]) -> dict[str, Any]:
        """Скачать OpenCV в <data>/pylib и сразу попробовать камеру."""
        self.toast("Скачиваю модуль камеры", "Около 40 МБ, это займёт минуту", icon="lock")
        await extras.install(CAMERA_PACKAGE)
        await self._close_detector()
        self._camera_retry_at = 0.0
        self._camera_error = ""
        self.toast("Модуль камеры готов", "Блокировка по камере работает", status="ok", icon="lock")
        return {"ok": True}

    async def act_check(self, args: dict[str, Any]) -> dict[str, Any]:
        """Проверить камеру сейчас: видно ли лицо."""
        self._camera_retry_at = 0.0
        face = await self._detect(self.config)
        if face is None:
            raise UserFacingError(self._camera_error or "Камера недоступна")
        if face:
            self._mark_seen(self.clock())
        return {"present": face}

    def commands(self) -> list[DynamicCommand]:
        return [
            self.command(
                "pause",
                "Не блокировать час",
                [
                    "не блокируй компьютер",
                    "не блокируй компьютер час",
                    "пауза автоблокировки",
                    "отключи автоблокировку на час",
                ],
                self._cmd_pause,
            ),
            self.command(
                "resume",
                "Включить автоблокировку",
                ["включи автоблокировку", "снова блокируй когда отойду", "блокируй когда отойду"],
                self._cmd_resume,
            ),
        ]

    async def _cmd_pause(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_pause({"minutes": 60})
        return ExecResult(True, "Автоблокировка", "Не блокирую час", icon=self.icon)

    async def _cmd_resume(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_resume({})
        return ExecResult(True, "Автоблокировка", "Снова слежу", icon=self.icon)
