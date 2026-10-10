"""Значок Jarvis в трее и его меню. Только UI: пункт меню — сигнал, работу делает app.py в рабочем потоке."""

from PySide6.QtCore import QObject, Signal, SignalInstance
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from jarvis.ui.icon import IconState, icon

TOOLTIP_MAX = 127  # Windows: szTip в NOTIFYICONDATA — 128 символов с нулём


class Tray(QObject):
    """Трей: четыре состояния значка, подсказка с готовностью компонентов, меню из S6a."""

    open_requested = Signal()
    local_toggled = Signal(bool)
    new_conversation = Signal()
    restart_hands = Signal()
    unload_hands = Signal()
    open_logs = Signal()
    autostart_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state: IconState = "busy"
        self._tooltip = ""
        self._tray = QSystemTrayIcon(icon("busy"), self)
        self._menu = QMenu()  # без родителя: меню трея — отдельное окно верхнего уровня
        self._open = self._add("Открыть", self.open_requested)
        self._menu.setDefaultAction(self._open)
        self._menu.addSeparator()
        self._local = self._add("Локальный режим", self.local_toggled, checkable=True)
        self._add("Новый разговор", self.new_conversation)
        self._menu.addSeparator()
        self._add("Перезапустить руки", self.restart_hands)
        self._add("Выгрузить руки", self.unload_hands)
        self._menu.addSeparator()
        self._add("Журнал", self.open_logs)
        self._autostart = self._add("Автозапуск", self.autostart_toggled, checkable=True)
        self._menu.addSeparator()
        self._add("Выход", self.quit_requested)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self.set_tooltip("Jarvis — запускается…")

    def _add(self, text: str, signal: SignalInstance, checkable: bool = False) -> QAction:
        action = QAction(text, self._menu)
        action.setCheckable(checkable)
        action.triggered.connect(signal)  # triggered — только действие человека, не setChecked()
        self._menu.addAction(action)
        return action

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    # --- состояние

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    @property
    def state(self) -> IconState:
        return self._state

    @property
    def tooltip(self) -> str:
        return self._tooltip

    def set_state(self, state: IconState) -> None:
        if state != self._state:
            self._state = state
            self._tray.setIcon(icon(state))

    def set_tooltip(self, text: str) -> None:
        text = text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"
        if text != self._tooltip:
            self._tooltip = text
            self._tray.setToolTip(text)

    def set_local(self, on: bool) -> None:
        self._local.setChecked(on)

    def set_autostart(self, on: bool) -> None:
        self._autostart.setChecked(on)

    def is_local_checked(self) -> bool:
        return self._local.isChecked()

    def is_autostart_checked(self) -> bool:
        return self._autostart.isChecked()

    def notify(self, text: str, kind: str = "info") -> None:
        """Всплывающее уведомление трея (хоткей занят, ошибка запуска)."""
        icons = {
            "info": QSystemTrayIcon.MessageIcon.Information,
            "warn": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }
        self._tray.showMessage("Jarvis", text, icons.get(kind, QSystemTrayIcon.MessageIcon.Information), 6000)

    def trigger(self, name: str) -> None:
        """Нажать пункт меню по имени (для тестов и самопроверки)."""
        for action in self._menu.actions():
            if action.text() == name:
                action.trigger()
                return
        raise KeyError(name)
