"""`jarvis run`: резидентное приложение — трей, глобальный хоткей, окно Jarvis, рабочие потоки.

Старт (значок в трее ≤1,5 с, всё тяжёлое — в фоне): логи → один экземпляр → QApplication → конфиг →
инвентарь приложений из кэша → ConfirmServer и обработчик подтверждений → окно (создаётся скрытым, дальше
только show/hide) → трей («занят») → хоткей → в фоне: Job Object, журнал, руки (ensure_server + warmup),
мозг (start, если режим не local), Core; обновление инвентаря через 15 с и раз в сутки.

PySide6 импортируют только этот модуль и jarvis/ui. UI-поток только ставит задачи: модели, действия, реестр,
процессы — в рабочих потоках; события и подтверждения приходят в UI сигналами Qt (QueuedConnection).
"""

import dataclasses
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from jarvis import config, winapp
from jarvis import log as jlog
from jarvis.context import Context
from jarvis.events import Done
from jarvis.ui.icon import IconState
from jarvis.ui.tray import Tray
from pc import confirm_client
from pc import windows as pc_windows
from pc.result import Caller

log = logging.getLogger("jarvis")

HOTKEY_ID = 1
CONFIRM_TIMEOUT_S = 60.0
CORE_WAIT_S = 60.0  # запрос, пришедший до готовности Core, ждёт её не дольше
PREV_REQUEST_JOIN_S = 5.0  # новый запрос ждёт отменённый старый
BRAIN_READY_WAIT_S = 90.0
APPS_FIRST_REFRESH_S = 15.0  # не мешать прогреву рук и мозга при старте
APPS_REFRESH_S = 24 * 3600.0
STOP_TIMEOUT_S = 8.0
LLAMA_SERVER_EXE = "llama-server.exe"

_MSG_MESSAGE_OFFSET = wintypes.MSG.message.offset


# --- готовность компонентов (без Qt) -------------------------------------------------------------------


@dataclass
class Health:
    """Что показать в трее. Пишут рабочие потоки, читает UI-поток (простые присваивания)."""

    mode: str = "normal"
    init_error: str = ""
    hands: str = "starting"  # starting | ready | stopped | error
    hands_error: str = ""
    hands_warnings: list[str] = field(default_factory=list)
    brain: str = "starting"  # starting | ready | off | error
    brain_error: str = ""
    hotkey: str | None = None  # зарегистрированное сочетание («Ctrl+Alt+Space»)
    hotkey_problem: str = ""  # ни одно сочетание не зарегистрировано — почему


def hands_warnings(status: Any) -> list[str]:
    """Предупреждения из Hands.status (S3): кэш префикса и VRAM."""
    if not isinstance(status, dict):
        return []
    result = []
    if status.get("prefix_cache") == "broken":
        result.append("кэш префикса рук не работает")
    if status.get("vram") == "slow":
        result.append("VRAM переполнена — руки медленные")
    return result


def brain_error(status: Any) -> str:
    """Текст ошибки из Brain.status (dict с ключом error или строка «ошибка: …»), иначе пусто."""
    if isinstance(status, dict):
        err = status.get("error")
        return str(err) if err else ""
    if isinstance(status, str) and status.casefold().startswith(("ошибка", "error")):
        return status
    return ""


_HANDS_TEXT = {"starting": "запускаются…", "ready": "готовы", "stopped": "выгружены", "error": "ошибка"}
_BRAIN_TEXT = {"starting": "запускается…", "ready": "готов", "off": "выключен", "error": "ошибка"}


def summarize(h: Health) -> tuple[IconState, str]:
    """Состояние значка и подсказка трея (≤127 символов, ограничение Windows)."""
    problems: list[str] = []
    if h.init_error:
        problems.append(h.init_error)
    if h.hands == "error":
        problems.append(f"руки: {h.hands_error}" if h.hands_error else "руки не запустились")
    problems += h.hands_warnings
    if h.mode != "local" and h.brain == "error":
        problems.append(f"мозг: {h.brain_error}" if h.brain_error else "мозг не запустился")
    if h.hotkey_problem:
        problems.append(h.hotkey_problem)
    busy = h.hands == "starting" or (h.mode != "local" and h.brain == "starting")
    state: IconState
    if problems:
        state, title = "warn", "Jarvis — есть проблемы"
    elif busy:
        state, title = "busy", "Jarvis — запускается…"
    elif h.mode == "local":
        state, title = "local", "Jarvis — готов, локальный режим"
    else:
        state, title = "ready", "Jarvis — готов"
    brain = "выключен (локальный режим)" if h.mode == "local" else _BRAIN_TEXT.get(h.brain, h.brain)
    lines = [
        title,
        f"Руки: {_HANDS_TEXT.get(h.hands, h.hands)}; мозг: {brain}",
        f"Хоткей: {h.hotkey or 'нет'}",
    ]
    lines += [f"⚠ {p}" for p in problems]
    text = "\n".join(lines)
    return state, text if len(text) <= 127 else text[:126] + "…"


def _check_started(result: Any, default: str) -> None:
    """ensure_server: исключение, False или Result(ok=False) — ошибка."""
    if result is False:
        raise RuntimeError(default)
    if getattr(result, "ok", True) is False:
        raise RuntimeError(getattr(result, "text", "") or default)


def _warm_audio() -> None:
    """Поток звука и импорт pycaw — заранее (pc.audio.warmup), а не на первой команде громкости."""
    from pc import audio

    audio.warmup()


def _safe(fn: Callable[[], Any], what: str) -> None:
    try:
        fn()
    except Exception as e:
        log.warning("%s: %s", what, e)


# --- сообщения Windows -----------------------------------------------------------------------------------


class NativeFilter(QAbstractNativeEventFilter):
    """WM_HOTKEY и «покажись» от второго запуска: сообщения потока UI проходят здесь до DispatchMessage."""

    def __init__(self, handler: Callable[[int], bool]) -> None:
        super().__init__()
        self._handler = handler

    def nativeEventFilter(self, event_type: Any, message: Any) -> tuple[bool, int]:
        try:
            if event_type == b"windows_generic_MSG" and self._handler(int(message)):
                return True, 0
        except Exception:
            log.exception("обработчик сообщения Windows")
        return False, 0


# --- приложение ------------------------------------------------------------------------------------------


class JarvisApp(QObject):
    """Окно, трей, хоткей и компоненты. Живёт в UI-потоке; рабочие потоки общаются с ним сигналами."""

    event_ready = Signal(int, object)  # (номер запроса, событие jarvis.events)
    confirm_ready = Signal(object)  # ui.logic.ConfirmRequest
    health_changed = Signal()
    notify_ready = Signal(str, str)  # (текст, kind) — уведомление трея
    autostart_changed = Signal(bool)
    exit_ready = Signal()  # дочернее остановлено — можно завершать цикл Qt

    def __init__(self, qapp: QApplication, started: float | None = None) -> None:
        super().__init__()
        self.qapp = qapp
        self.started = time.perf_counter() if started is None else started
        self.cfg: Any = None
        self.ctx = Context()
        self.health = Health()
        self.window: Any = None
        self.tray: Tray | None = None
        self.receiver: QWidget | None = None
        self.hands: Any = None
        self.brain: Any = None
        self.core: Any = None
        self.journal: Any = None
        self.confirm_server: Any = None
        self.confirm_address = ""
        self.job: Any = None
        self._hook: Callable[[Any], None] | None = None
        self._filter: NativeFilter | None = None
        self._receiver_hwnd = 0
        self._show_msg = 0
        self._core_ready = threading.Event()
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._hands_lock = threading.Lock()
        self._brain_lock = threading.Lock()
        self._rid = 0
        self._worker: threading.Thread | None = None
        self._canceller: threading.Thread | None = None
        self._hotkey_ms: float | None = None
        self._closed = False
        queued = Qt.ConnectionType.QueuedConnection
        self.event_ready.connect(self._on_event, queued)
        self.confirm_ready.connect(self._on_confirm, queued)
        self.health_changed.connect(self._refresh_tray, queued)
        self.notify_ready.connect(self._notify, queued)
        self.autostart_changed.connect(self._on_autostart_changed, queued)
        self.exit_ready.connect(self._on_exit_ready, queued)

    # --- старт (UI-поток)

    def start(self) -> None:
        self.cfg = config.load()
        self.health.mode = self.cfg.mode
        if self.cfg.mode == "local":
            self.health.brain = "off"
        self._load_inventory()
        self._start_confirm_server()
        self._create_window()
        self._create_tray()
        self._setup_hotkey()
        self._refresh_tray()
        tray_ms = (time.perf_counter() - self.started) * 1000
        log.info("значок в трее через %.0f мс от входа в main", tray_ms)
        tray_wall = time.time()
        threading.Thread(
            target=self._init_components, args=(tray_wall,), name="jarvis-init", daemon=True
        ).start()
        threading.Thread(target=self._apps_loop, name="jarvis-apps", daemon=True).start()

    def _load_inventory(self) -> None:
        try:
            from pc import apps

            count = len(apps.inventory())
            apps.enable_auto_refresh(True)
            log.info("инвентарь приложений из кэша: %d", count)
        except Exception as e:
            log.warning("инвентарь приложений не загружен: %s", e)

    def _start_confirm_server(self) -> None:
        try:
            server = confirm_client.ConfirmServer(self._confirm)
            server.start()
            self.confirm_server = server
            self.confirm_address = str(server.address)
        except Exception as e:
            log.warning("канал подтверждений для мозга не поднят: %s", e)
        confirm_client.set_confirm_handler(self._confirm)

    def _create_window(self) -> None:
        from jarvis.ui.window import LauncherWindow

        w = LauncherWindow(self.cfg.ui)
        w.submitted.connect(self._on_submitted)
        w.cancel_requested.connect(self._on_cancel)
        w.open_item.connect(self._on_open_item)
        w.hidden.connect(self._on_hidden)
        w.set_local_mode(self.cfg.mode == "local")
        w.winId()  # родное окно создаётся сейчас, а не при первом хоткее
        self.window = w

    def _create_tray(self) -> None:
        t = Tray(self)
        t.open_requested.connect(self._on_tray_open)
        t.local_toggled.connect(self._on_local_toggled)
        t.new_conversation.connect(self._on_new_conversation)
        t.restart_hands.connect(self._on_restart_hands)
        t.unload_hands.connect(self._on_unload_hands)
        t.open_logs.connect(self._on_open_logs)
        t.autostart_toggled.connect(self._on_autostart_toggled)
        t.quit_requested.connect(self._on_quit)
        t.set_local(self.cfg.mode == "local")
        t.set_autostart(winapp.autostart_enabled())
        t.set_state("busy")
        t.show()
        self.tray = t

    def _setup_hotkey(self) -> None:
        """Скрытое окно-приёмник (top-level, не показывается): на его HWND — RegisterHotKey и «покажись»."""
        if not winapp.available():
            log.info("не Windows: глобальный хоткей не регистрируется")
            return
        self.receiver = QWidget()
        self.receiver.setWindowTitle(winapp.RECEIVER_TITLE)
        self._receiver_hwnd = int(self.receiver.winId())
        self._show_msg = winapp.show_message_id()
        self._filter = NativeFilter(self.handle_native)
        self.qapp.installNativeEventFilter(self._filter)
        result = winapp.register_with_fallback(self._receiver_hwnd, HOTKEY_ID, self.cfg.ui.hotkey)
        self.health.hotkey = winapp.format_hotkey(result.combo) if result.combo else None
        self.health.hotkey_problem = "" if result.combo else result.message
        if result.message and self.tray is not None:
            self.tray.notify(result.message, "warn")

    # --- компоненты (рабочие потоки)

    def _init_components(self, tray_wall: float) -> None:
        t0 = time.perf_counter()
        self._log_process_start(tray_wall)
        try:
            if winapp.available():
                self.job = winapp.create_job()
                self._hook = winapp.job_hook(self.job)
            journal = self._make_journal()
            from jarvis.brain import Brain
            from jarvis.core import Core
            from jarvis.hands import Hands

            self.hands = Hands(self.cfg.hands, job_hook=self._hook)
            self.brain = Brain(self.cfg.brain, self.confirm_address, process_hook=self._hook)
            self.journal = journal
            self.core = Core(self.cfg, self.hands, self.brain, journal)
            log.info("компоненты созданы за %.0f мс", (time.perf_counter() - t0) * 1000)
        except Exception as e:
            log.exception("компоненты Jarvis не созданы")
            self.health.init_error = f"не запустился: {e}"
        finally:
            self._core_ready.set()
            self.health_changed.emit()
        if self.core is None or self._stop.is_set():
            return
        threading.Thread(target=self._start_hands, name="jarvis-hands", daemon=True).start()
        _safe(_warm_audio, "звук не прогрет")
        self._start_brain()

    def _log_process_start(self, tray_wall: float) -> None:
        try:
            import psutil

            since = tray_wall - psutil.Process().create_time()
            log.info("значок в трее через %.0f мс от запуска процесса", since * 1000)
        except Exception as e:
            log.debug("время старта процесса: %s", e)

    def _make_journal(self) -> Any:
        try:
            from jarvis.journal import Journal

            return Journal()
        except Exception as e:
            log.warning("журнал не открыт: %s", e)
            return None

    def _set_hands(self, state: str, error: str = "") -> None:
        self.health.hands, self.health.hands_error = state, error
        self.health.hands_warnings = hands_warnings(getattr(self.hands, "status", None))
        self.health_changed.emit()

    def _start_hands(self) -> None:
        with self._hands_lock:
            if self._stop.is_set():
                return
            self._set_hands("starting")
            try:
                _check_started(self.hands.ensure_server(), "сервер рук не запустился")
                self._jail_hands_server()
                self.hands.warmup()
            except Exception as e:
                log.warning("руки не готовы: %s", e)
                self._set_hands("error", str(e))
                return
            self._set_hands("ready")

    def _jail_hands_server(self) -> None:
        """Гонка job_hook: cmd.exe мог запустить llama-server раньше, чем сам попал в job.

        Сервер, запущенный этим Jarvis, — наш потомок (Jarvis → cmd.exe → llama-server.exe): добавить в job.
        Принятый чужой сервер (запущен вручную или CLI) нашим потомком не бывает — его не трогаем.
        """
        if self.job is None:
            return
        for pid in winapp.own_descendants(LLAMA_SERVER_EXE):
            winapp.add_to_job(self.job, pid)

    def _stop_hands(self) -> None:
        with self._hands_lock:
            try:
                self.hands.stop()
            except Exception as e:
                log.warning("руки не остановлены: %s", e)
                self._set_hands("error", str(e))
                return
            self._set_hands("stopped")

    def _start_brain(self) -> None:
        """Brain.start() (если режим не local) и ожидание готовности — для подсказки трея."""
        brain = self.brain
        with self._brain_lock:
            if brain is None or self._stop.is_set() or self.health.mode == "local":
                return
            self.health.brain, self.health.brain_error = "starting", ""
            self.health_changed.emit()
            try:
                brain.start()
            except Exception as e:
                log.warning("мозг не запущен: %s", e)
                self.health.brain, self.health.brain_error = "error", str(e)
                self.health_changed.emit()
                return
        deadline = time.monotonic() + BRAIN_READY_WAIT_S
        while not self._stop.is_set() and self.health.mode != "local":
            if getattr(brain, "ready", False):
                self.health.brain = "ready"
                break
            err = brain_error(getattr(brain, "status", None))
            if err:
                self.health.brain, self.health.brain_error = "error", err
                break
            if time.monotonic() > deadline:
                self.health.brain, self.health.brain_error = "error", "мозг не ответил за 90 с"
                break
            self._stop.wait(0.25)
        self.health_changed.emit()

    def _close_brain(self) -> None:
        with self._brain_lock:
            if self.brain is not None:
                _safe(self.brain.close, "мозг не остановлен")
            self.health.brain, self.health.brain_error = "off", ""
        self.health_changed.emit()

    def _apps_loop(self) -> None:
        delay = APPS_FIRST_REFRESH_S
        while not self._stop.wait(delay):
            try:
                from pc import apps

                log.info("инвентарь приложений обновлён: %d", len(apps.refresh()))
            except Exception as e:
                log.warning("инвентарь приложений не обновлён: %s", e)
            delay = APPS_REFRESH_S

    def _bg(self, fn: Callable[..., Any], *args: Any, name: str = "jarvis-task") -> threading.Thread:
        """Короткая задача из обработчика UI — в рабочий поток."""
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()
        return t

    # --- трей

    @Slot()
    def _refresh_tray(self) -> None:
        if self.tray is None:
            return
        state, text = summarize(self.health)
        self.tray.set_state(state)
        self.tray.set_tooltip(text)

    @Slot(str, str)
    def _notify(self, text: str, kind: str) -> None:
        if self.tray is not None:
            self.tray.notify(text, kind)

    @Slot()
    def _on_tray_open(self) -> None:
        self.show_window()

    @Slot(bool)
    def _on_local_toggled(self, on: bool) -> None:
        self.health.mode = "local" if on else "normal"
        if on:
            self.health.brain = "off"
        if self.tray is not None:
            self.tray.set_local(on)
        self.window.set_local_mode(on)
        self._refresh_tray()
        self._bg(self._switch_mode, on, name="jarvis-mode")

    def _switch_mode(self, on: bool) -> None:
        """Режим в jarvis.toml (строка mode), новый cfg — в Core; local → Brain.close(), иначе start()."""
        mode = "local" if on else "normal"
        try:
            config.save_mode(mode)
        except Exception as e:
            log.warning("режим не сохранён в jarvis.toml: %s", e)
            self.notify_ready.emit(f"Режим не сохранён в jarvis.toml: {e}", "warn")
        cfg = config.load()
        if cfg.mode != mode:  # файл не записался — режим всё равно меняется до выхода
            cfg = dataclasses.replace(cfg, mode=mode)
        self.cfg = cfg
        log.info("режим: %s", mode)
        self._core_ready.wait(CORE_WAIT_S)
        if self.core is not None:
            self.core.cfg = cfg  # Core читает cfg на каждый запрос
        if self.health.mode != mode:  # успели переключить обратно
            return
        if on:
            self._close_brain()
        else:
            self._start_brain()

    @Slot()
    def _on_new_conversation(self) -> None:
        brain = self.brain
        if brain is not None and self.health.mode != "local":
            self._bg(_safe, brain.new_conversation, "новый разговор не начат", name="jarvis-brain")

    @Slot()
    def _on_restart_hands(self) -> None:
        if self.hands is not None:
            self._bg(self._restart_hands, name="jarvis-hands")

    def _restart_hands(self) -> None:
        self._stop_hands()
        self._start_hands()

    @Slot()
    def _on_unload_hands(self) -> None:
        if self.hands is not None:
            self._bg(self._stop_hands, name="jarvis-hands")

    @Slot()
    def _on_open_logs(self) -> None:
        self._bg(self._open_logs, name="jarvis-logs")

    def _open_logs(self) -> None:
        from pc import files, settings

        path = settings.data_dir() / "logs"
        try:
            path.mkdir(parents=True, exist_ok=True)
            result = files.open_target(str(path), "folder", "user")
        except Exception as e:
            log.warning("папка логов не открыта: %s", e)
            self.notify_ready.emit(f"Папка логов не открыта: {e}", "warn")
            return
        if not result.ok:
            self.notify_ready.emit(result.text, "warn")

    @Slot(bool)
    def _on_autostart_toggled(self, on: bool) -> None:
        self._bg(self._set_autostart, on, name="jarvis-autostart")

    def _set_autostart(self, on: bool) -> None:
        text = winapp.set_autostart(on)
        enabled = winapp.autostart_enabled()
        self.autostart_changed.emit(enabled)
        if enabled != on:
            self.notify_ready.emit(text, "warn")

    @Slot(bool)
    def _on_autostart_changed(self, on: bool) -> None:
        if self.tray is not None:
            self.tray.set_autostart(on)

    @Slot()
    def _on_quit(self) -> None:
        self.begin_exit()  # цикл Qt завершит exit_ready, когда дочернее остановлено

    @Slot()
    def _on_exit_ready(self) -> None:
        self.qapp.quit()

    # --- окно и хоткей

    def handle_native(self, address: int) -> bool:
        """Сообщение потока UI (адрес MSG). True — наше, дальше не передавать."""
        msg_id = wintypes.UINT.from_address(address + _MSG_MESSAGE_OFFSET).value
        if msg_id == winapp.WM_HOTKEY:
            msg = wintypes.MSG.from_address(address)
            if msg.wParam != HOTKEY_ID:
                return False
            try:
                queue_ms = float((winapp.tick_count() - int(msg.time)) & 0xFFFFFFFF)
            except OSError:
                queue_ms = 0.0
            self.on_hotkey(queue_ms if queue_ms < 10_000 else 0.0)
            return True
        if self._show_msg and msg_id == self._show_msg:
            self.show_window()
            return True
        return False

    def on_hotkey(self, queue_ms: float = 0.0) -> None:
        """WM_HOTKEY: снять активное окно (ДО показа Jarvis) → показать → сразу SetForegroundWindow."""
        t0 = time.perf_counter()
        w = self.window
        if w.isVisible() and w.isActiveWindow():
            if not w.is_busy():
                w.hide_launcher()
            return
        try:
            self.ctx.active_window = pc_windows.foreground()
        except Exception as e:
            log.debug("активное окно не снято: %s", e)
            self.ctx.active_window = None
        w.show_launcher()
        if winapp.available():
            winapp.set_foreground(int(w.winId()))  # HWND — заново: Qt мог пересоздать окно
        ms = queue_ms + (time.perf_counter() - t0) * 1000
        self._hotkey_ms = round(ms, 1)
        log.info("хоткей → окно: %.1f мс (из них в очереди %.0f мс)", ms, queue_ms)

    def show_window(self) -> None:
        """Показать окно из трея или по просьбе второго запуска: активного окна «до Jarvis» здесь нет."""
        w = self.window
        if not w.isVisible():
            self.ctx.active_window = None
        self._hotkey_ms = None
        w.show_launcher()
        if winapp.available():
            winapp.set_foreground(int(w.winId()))  # HWND — заново: Qt мог пересоздать окно

    @Slot()
    def _on_hidden(self) -> None:
        self._hotkey_ms = None

    # --- запросы

    @Slot(str)
    def _on_submitted(self, text: str) -> None:
        text = text.strip()
        if text:
            self.start_request(text)

    @Slot(int)
    def _on_open_item(self, index: int) -> None:
        self.start_request(f"открой {index + 1}")

    @Slot()
    def _on_cancel(self) -> None:
        core = self.core
        if core is not None and self._worker is not None and self._worker.is_alive():
            self._canceller = self._bg(_safe, core.cancel, "отмена запроса", name="jarvis-cancel")

    def start_request(self, text: str) -> None:
        """Новый запрос в своём рабочем потоке; старый (если идёт) этот поток сначала отменяет и ждёт."""
        prev = self._worker if self._worker is not None and self._worker.is_alive() else None
        self._rid += 1
        rid = self._rid
        hotkey_ms, self._hotkey_ms = self._hotkey_ms, None
        self.window.begin_request(text)
        self._worker = self._bg(self._run_request, rid, text, hotkey_ms, prev, name=f"jarvis-request-{rid}")

    def _run_request(
        self, rid: int, text: str, hotkey_ms: float | None, prev: threading.Thread | None
    ) -> None:
        # по порядку: отмена старого → его конец → отмена по Esc (если ещё идёт) → новый handle();
        # иначе запоздалый cancel() отменил бы уже новый запрос (Core сбрасывает флаг в начале handle)
        if prev is not None:
            if self.core is not None:
                _safe(self.core.cancel, "отмена запроса")
            prev.join(PREV_REQUEST_JOIN_S)
            if prev.is_alive():
                log.warning("предыдущий запрос не завершился за %.0f с", PREV_REQUEST_JOIN_S)
        canceller = self._canceller
        if canceller is not None:
            canceller.join(PREV_REQUEST_JOIN_S)
        if not self._core_ready.wait(CORE_WAIT_S) or self.core is None:
            reason = self.health.init_error or "компоненты ещё запускаются"
            self.event_ready.emit(
                rid, Done(ok=False, text=f"Jarvis не готов: {reason}", reason="app:not_ready")
            )
            return
        finished = False
        try:
            for ev in self.core.handle(text, self.ctx, hotkey_ms=hotkey_ms):
                self.event_ready.emit(rid, ev)
                finished = finished or isinstance(ev, Done)
        except Exception as e:
            log.exception("запрос упал")
            if not finished:
                self.event_ready.emit(rid, Done(ok=False, text=f"Ошибка: {e}", reason="app:error"))
            return
        if not finished:
            self.event_ready.emit(
                rid, Done(ok=False, text="Запрос завершился без ответа", reason="app:no_done")
            )

    @Slot(int, object)
    def _on_event(self, rid: int, ev: Any) -> None:
        if rid == self._rid and self.window is not None:  # события отменённого старого запроса не показываем
            self.window.on_event(ev)
        if isinstance(ev, Done):
            self._sync_health()

    def _sync_health(self) -> None:
        """После запроса: руки могли подняться сами («Выгрузить руки» → команда), мозг — упасть или ожить."""
        h = self.health
        status = getattr(self.hands, "status", None)
        if isinstance(status, dict) and h.hands in ("stopped", "error", "ready"):
            server = status.get("server")
            if server == "ready":
                h.hands, h.hands_error = "ready", ""
            elif server == "error":
                h.hands, h.hands_error = "error", str(status.get("error") or "")
            h.hands_warnings = hands_warnings(status)
        if self.brain is not None and h.mode != "local" and h.brain in ("ready", "error"):
            if getattr(self.brain, "ready", False):
                h.brain, h.brain_error = "ready", ""
            else:
                err = brain_error(getattr(self.brain, "status", None))
                if err:
                    h.brain, h.brain_error = "error", err
        self._refresh_tray()

    # --- подтверждения

    def _confirm(self, summary: str, details: str, caller: Caller) -> bool:
        """Обработчик confirm_client и ConfirmServer (рабочий поток или поток канала): одна очередь в окне."""
        if self._closed:
            return False
        if threading.current_thread() is threading.main_thread():
            log.error("подтверждение запрошено из UI-потока — отказ (иначе окно зависнет)")
            return False
        from jarvis.ui.logic import ConfirmRequest

        req = ConfirmRequest(summary, details, caller)
        self.confirm_ready.emit(req)
        return bool(req.wait(CONFIRM_TIMEOUT_S))

    @Slot(object)
    def _on_confirm(self, req: Any) -> None:
        w = self.window
        if not w.isVisible():
            # окно показывается само — без кражи фокуса (человек мог печатать в другом окне), и мигаем
            w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            w.show_launcher()
            w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            QApplication.alert(w)
        w.ask_confirm(req)

    # --- выход

    def begin_exit(self) -> None:
        """Выход без блокировки UI: снять хоткей, убрать трей и окно; дочернее останавливает рабочий поток.

        Brain.close() ждёт codex до нескольких секунд — поэтому не в UI-потоке. Когда всё остановлено
        и Job Object закрыт (он добьёт остатки), поток сигналом завершает цикл Qt.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        log.info("выход")
        confirm_client.set_confirm_handler(None)
        if self._receiver_hwnd and winapp.available():
            winapp.unregister_hotkey(self._receiver_hwnd, HOTKEY_ID)
        if self._filter is not None:
            self.qapp.removeNativeEventFilter(self._filter)
            self._filter = None
        if self.tray is not None:
            self.tray.hide()
        if self.window is not None and self.window.isVisible():
            self.window.hide_launcher()
        self._bg(self._exit_worker, name="jarvis-exit")

    def shutdown(self) -> None:
        """Выход и ожидание остановки дочернего (после цикла Qt и в тестах). Повторно — только ждёт."""
        self.begin_exit()
        if not self._stopped.wait(STOP_TIMEOUT_S + 2):
            log.warning("остановка не закончилась — дочерние процессы завершит Job Object")

    def _exit_worker(self) -> None:
        stopper = self._bg(self._stop_children, name="jarvis-stop")
        stopper.join(STOP_TIMEOUT_S)
        if stopper.is_alive():
            log.warning(
                "дочерние процессы не остановились за %.0f с — их завершит Job Object", STOP_TIMEOUT_S
            )
        winapp.close_job(self.job)
        self.job = None
        self._stopped.set()
        self.exit_ready.emit()

    def _stop_children(self) -> None:
        if self.core is not None:
            _safe(self.core.cancel, "отмена запроса")
        if self.hands is not None:
            _safe(self.hands.stop, "руки не остановлены")
            if hasattr(self.hands, "close"):
                _safe(self.hands.close, "руки не закрыты")
        if self.brain is not None:
            _safe(self.brain.close, "мозг не остановлен")
        if self.confirm_server is not None:
            _safe(self.confirm_server.close, "канал подтверждений не закрыт")
        if self.journal is not None:
            _safe(self.journal.close, "журнал не закрыт")


def scrub_child_env(environ: Any, meipass: str | None) -> None:
    """Убрать из окружения то, что рантайм-хук PyInstaller ставит для нашего Qt.

    QT_PLUGIN_PATH, QML2_IMPORT_PATH и папку бандла в PATH наследуют программы, которые открывает Jarvis,
    и чужой Qt (OBS и т.п.) иначе грузит наши плагины. Вызывать после QCoreApplication.libraryPaths().
    """
    environ.pop("QT_PLUGIN_PATH", None)
    environ.pop("QML2_IMPORT_PATH", None)
    if not meipass:
        return
    parts = [p for p in environ.get("PATH", "").split(os.pathsep) if p]
    keep = [
        p
        for p in parts
        if os.path.normcase(os.path.normpath(p)) != os.path.normcase(os.path.normpath(meipass))
    ]
    if len(keep) != len(parts):
        environ["PATH"] = os.pathsep.join(keep)


def _qapp() -> QApplication:
    existing = QApplication.instance()
    qapp = (
        existing
        if isinstance(existing, QApplication)
        else QApplication([sys.argv[0] if sys.argv else "jarvis"])
    )
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setApplicationName("Jarvis")
    font = QFont(qapp.font())
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    qapp.setFont(font)
    if getattr(sys, "frozen", False):
        qapp.libraryPaths()  # Qt запоминает пути к плагинам до очистки окружения
        scrub_child_env(os.environ, getattr(sys, "_MEIPASS", None))
    return qapp


def main(argv: list[str] | None = None) -> int:
    """`jarvis run`: 0 — штатный выход (или окно уже запущенного Jarvis показано), 1 — не запустился."""
    started = time.perf_counter()
    jlog.setup("jarvis")
    log.info("jarvis run: старт, pid %d", os.getpid())
    instance: int | None = 0
    if winapp.available():
        instance = winapp.acquire_instance()
        if instance is None:
            log.info("Jarvis уже запущен — показываю его окно")
            winapp.signal_first_instance()
            return 0
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C в консоли — сразу выход; дети — в Job Object
    qapp = _qapp()
    app = JarvisApp(qapp, started)
    try:
        app.start()
        return qapp.exec()
    except Exception:
        log.exception("Jarvis не запустился")
        return 1
    finally:
        app.shutdown()
        if winapp.available():
            winapp.release_instance(instance)
