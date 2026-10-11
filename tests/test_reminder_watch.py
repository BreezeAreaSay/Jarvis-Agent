"""Срабатывание напоминаний в приложении (офскрин Qt): таймер до ближайшего не дольше MAX_WAIT_S, без
напоминаний — без таймера, пропущенные — при старте, изменение файла другим процессом пере-взводит таймер,
fired приходит в UI-поток."""

import json
import os
import time
from collections.abc import Callable, Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # до создания QApplication

from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication

from jarvis import reminders
from jarvis.reminders import Reminder
from jarvis.ui.reminder_watch import LATE_S, MAX_WAIT_S, ReminderWatch, notification_text
from pc import settings


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


@pytest.fixture(autouse=True)
def _fresh_fired() -> Iterator[None]:
    reminders._fired.clear()
    yield
    reminders._fired.clear()


@pytest.fixture
def watch(qapp: QApplication) -> Iterator[ReminderWatch]:
    w = ReminderWatch()
    yield w
    w.stop()
    w.deleteLater()
    QCoreApplication.processEvents()


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def write_raw(items: list[dict[str, object]]) -> None:
    """Как будто файл записал другой процесс (`jarvis ask`): атомарно, через os.replace."""
    path = settings.data_dir() / reminders.FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def test_notification_text_on_time_and_late() -> None:
    now = time.time()
    rem = Reminder("a", "вынуть пиццу", now - 5, now - 600)
    assert notification_text(rem, now) == "Напоминание: вынуть пиццу"
    late = Reminder("b", "позвонить", now - LATE_S - 3600, now - 7200)
    text = notification_text(late, now)
    assert text.startswith("Напоминание (было ") and text.endswith("): позвонить")


def test_no_reminders_no_timer(watch: ReminderWatch) -> None:
    watch.refresh()
    assert not watch.timer.isActive()


def test_timer_waits_until_due_but_not_longer_than_max(watch: ReminderWatch) -> None:
    reminders.add("скоро", time.time() + 10)
    watch.refresh()
    assert watch.timer.isActive() and 9_000 <= watch.timer.interval() <= 10_100
    reminders.cancel("все")
    reminders.add("через час", time.time() + 3600)
    watch.refresh()
    assert watch.timer.interval() <= MAX_WAIT_S * 1000 + 100


def test_start_fires_missed_in_ui_thread_and_removes_them(watch: ReminderWatch, qapp: QApplication) -> None:
    now = time.time()
    write_raw(
        [
            {"id": "a", "text": "пропущенное", "due_ts": now - 3600, "created_ts": now - 7200},
            {"id": "b", "text": "будущее", "due_ts": now + 600, "created_ts": now - 60},
        ]
    )
    got: list[list[Reminder]] = []
    threads: list[bool] = []

    def on_fired(items: list[Reminder]) -> None:
        got.append(items)
        threads.append(QThread.currentThread() is qapp.thread())

    watch.fired.connect(on_fired)
    watch.start()
    assert wait_for(lambda: bool(got))
    assert [r.text for r in got[0]] == ["пропущенное"] and threads == [True]
    assert [r.text for r in reminders.pending()] == ["будущее"]
    assert wait_for(watch.timer.isActive)


def test_due_reminder_fires_by_timer(watch: ReminderWatch) -> None:
    got: list[str] = []
    watch.fired.connect(lambda items: got.extend(r.text for r in items))
    watch.start()
    reminders.add("через миг", time.time() + 0.2)
    watch.refresh()
    assert wait_for(lambda: got == ["через миг"], timeout=3.0)
    assert reminders.pending() == [] and wait_for(lambda: not watch.timer.isActive())


def test_file_written_by_other_process_rearms(watch: ReminderWatch) -> None:
    watch.start()
    assert wait_for(lambda: watch._worker is not None and not watch._worker.is_alive())
    QCoreApplication.processEvents()
    assert not watch.timer.isActive()
    now = time.time()
    write_raw([{"id": "c", "text": "из консоли", "due_ts": now + 30, "created_ts": now}])
    assert wait_for(watch.timer.isActive, timeout=5.0)


def test_stop_cancels_timer_and_signals(watch: ReminderWatch) -> None:
    got: list[object] = []
    watch.fired.connect(got.append)
    reminders.add("потом", time.time() + 60)
    watch.start()
    assert wait_for(watch.timer.isActive)
    watch.stop()
    assert not watch.timer.isActive() and watch._watcher.directories() == []
    watch.check()  # после stop — ничего
    QCoreApplication.processEvents()
    assert got == []
