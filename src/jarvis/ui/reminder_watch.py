"""Срабатывание напоминаний в приложении (jarvis.reminders): один таймер до ближайшего, без опроса файла.

- QTimer (single-shot) взводится на reminders.next_due(), но не дальше MAX_WAIT_S: после сна ПК таймер может
  опоздать, поэтому, пока есть хоть одно напоминание, раз в MAX_WAIT_S сверяемся с часами. Напоминаний нет —
  таймера нет (покой — 0 % CPU).
- Папку данных слушает QFileSystemWatcher: напоминание, добавленное из консоли (`jarvis ask`), пере-взводит
  таймер сразу. Внутри приложения после каждого запроса app зовёт refresh().
- pop_due переписывает файл — это делает рабочий поток (UI-поток не ждёт диск); итог приходит сигналом fired
  в UI-поток.
- Пропущенные, пока Jarvis не работал, приходят первым срабатыванием после start() — с временем «было …».
"""

import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer, Signal

from jarvis import reminders
from jarvis.reminders import Reminder
from pc import settings

log = logging.getLogger("jarvis")

MAX_WAIT_S = 120.0  # дольше не ждать: таймер мог «проспать» сон ПК
LATE_S = 60.0  # сработало позже на столько — показать, на когда было назначено
SLACK_MS = 50  # таймер Windows может сработать чуть раньше срока


def notification_text(rem: Reminder, now: float | None = None) -> str:
    """«Напоминание: вынуть пиццу»; пропущенное — «Напоминание (было вчера в 18:30): …»."""
    t = time.time() if now is None else now
    if t - rem.due_ts > LATE_S:
        return f"Напоминание (было {reminders.format_due(rem.due_ts, t)}): {rem.text}"
    return f"Напоминание: {rem.text}"


class ReminderWatch(QObject):
    """Создаётся в UI-потоке; fired(list[Reminder]) — тоже в UI-потоке."""

    fired = Signal(list)
    _popped = Signal(list)  # из рабочего потока → UI-поток (очередь Qt)

    def __init__(self, parent: QObject | None = None, clock: Callable[[], float] = time.time) -> None:
        super().__init__(parent)
        self._clock = clock
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # грубый таймер Qt ошибается до 5 % срока
        self._timer.timeout.connect(self.check)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir)
        self._popped.connect(self._on_popped)
        self._version = -1
        self._worker: threading.Thread | None = None
        self._stopped = False

    @property
    def timer(self) -> QTimer:
        return self._timer

    def start(self) -> None:
        """Слушать папку данных и сразу показать пропущенные."""
        self._stopped = False
        folder = settings.data_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("напоминания: папка данных недоступна: %s", e)
        if not self._watcher.addPath(str(folder)):
            log.warning("напоминания: не слежу за %s — напоминания из консоли заметятся позже", folder)
        self.check()

    def stop(self) -> None:
        self._stopped = True
        self._timer.stop()
        paths = self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)

    def refresh(self) -> None:
        """Список мог измениться (запрос в этом процессе) — пере-взвести таймер."""
        if not self._stopped:
            self._arm()

    def check(self) -> None:
        """Забрать сработавшие (в рабочем потоке) и пере-взвести таймер."""
        if self._stopped or (self._worker is not None and self._worker.is_alive()):
            return
        now = self._clock()
        self._worker = threading.Thread(target=self._pop, args=(now,), name="jarvis-reminders", daemon=True)
        self._worker.start()

    def _pop(self, now: float) -> None:
        try:
            due = reminders.pop_due(now)
        except Exception:  # чтение не бросает, но поток не должен умереть молча
            log.exception("напоминания: не удалось забрать сработавшие")
            due = []
        self._popped.emit(due)

    def _on_popped(self, due: list[Reminder]) -> None:
        if due and not self._stopped:
            self.fired.emit(due)
        self.refresh()

    def _on_dir(self, _path: str) -> None:
        version = reminders.version()
        if version != self._version:
            self._version = version
            self.refresh()

    def _arm(self) -> None:
        due = reminders.next_due()
        if due is None:
            self._timer.stop()
            return
        wait = min(max(due - self._clock(), 0.0), MAX_WAIT_S)
        self._timer.start(int(wait * 1000) + SLACK_MS)
