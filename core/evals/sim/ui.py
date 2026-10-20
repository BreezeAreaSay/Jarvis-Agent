"""UI Automation имитатора (агенту — вместо jarvis.ai.uia.WindowsUi) и уведомления Windows (вместо
WinNotificationSource).

Снимок окна — виджеты сцены с uia=True: id e1…eN по порядку, прямоугольники в точках экрана, значение
полей, фокус. Чего нет в UI Automation (список чатов Telegram, обложки плейлистов, мелкие подписи «Облака»),
в снимок не попадает — только на картинку экрана. Нажатие и ввод по id идут «в фоне», шаблонами: окно
вперёд не выходит, мышь не двигается — как у настоящего UI Automation.
"""

from dataclasses import dataclass
from typing import Any

from evals.sim.world import World
from jarvis.ai.uia import SNAPSHOT_LIMIT, UiElement, UiError, UiSnapshot


class SimUi:
    def __init__(self, world: World) -> None:
        self.world = world
        self._ids: dict[str, tuple[int, str]] = {}  # id последнего снимка → (hwnd, ключ виджета)

    def snapshot(self, limit: int = SNAPSHOT_LIMIT, hwnd: int | None = None) -> UiSnapshot:
        world = self.world
        with world.lock:
            world.tick()
            window = world.window(hwnd) if hwnd else world.foreground
            if window is None:
                raise UiError("Окно закрылось — сделай ui_snapshot ещё раз" if hwnd else "Нет активного окна")
            visible = [w for w in window.widgets() if w.uia and w.kind != "Pane"]
            truncated = len(visible) > limit
            self._ids = {}
            elements: list[UiElement] = []
            for number, widget in enumerate(visible[:limit], start=1):
                element_id = f"e{number}"
                self._ids[element_id] = (window.hwnd, widget.key)
                value = (
                    window.values.get(widget.key, widget.value) if widget.kind in ("Edit", "ComboBox") else ""
                )
                elements.append(
                    UiElement(
                        element_id,
                        widget.name,
                        widget.kind,
                        window.screen_rect(widget.rect),
                        enabled=widget.enabled,
                        focused=window is world.foreground and window.focus == widget.key,
                        password=widget.password,
                        value="" if widget.password else value,
                    )
                )
            return UiSnapshot(
                window.title,
                window.exe,
                elements,
                truncated,
                hwnd=window.hwnd,
                pid=window.pid,
                rect=window.rect,
            )

    def _get(self, element_id: str) -> tuple[Any, Any]:
        found = self._ids.get(element_id)
        if found is None:
            raise UiError(f"Элемента {element_id} нет в последнем снимке окна — сначала сделай ui_snapshot")
        window = self.world.window(found[0])
        if window is None:
            raise UiError("Окно закрылось — сделай ui_snapshot ещё раз")
        widget = window.widget(found[1])
        if widget is None or not widget.uia:
            raise UiError(f"Элемент {element_id} больше не существует — сделай ui_snapshot")
        return window, widget

    def click(self, element_id: str) -> str:
        with self.world.lock:
            self.world.tick()
            window, widget = self._get(element_id)
            if not widget.enabled:
                raise UiError(f"Не удалось нажать «{widget.name or element_id}»: элемент недоступен")
            return self.world.click_widget(window, widget, via="uia")

    def set_text(self, element_id: str, text: str) -> str:
        with self.world.lock:
            self.world.tick()
            window, widget = self._get(element_id)
            if not widget.editable:
                raise UiError(f"Не удалось ввести текст в «{widget.name or element_id}»: это не поле ввода")
            self.world.set_text(window, widget, text)
            return "текст вставлен"

    def element_at(self, x: int, y: int) -> tuple[str, str]:
        with self.world.lock:
            window = self.world.window_at(x, y)
            widget = self.world.widget_at(window, x, y) if window is not None else None
            if widget is None or not widget.uia:
                return "", ""
            return widget.name[:120], widget.kind

    def owner_at(self, x: int, y: int) -> tuple[int, str]:
        with self.world.lock:
            if self.world.toast_at(x, y) is not None:
                return 1, "Новое уведомление"
            window = self.world.window_at(x, y)
            return (window.pid, window.title) if window is not None else (0, "")


@dataclass
class SimToast:
    id: int
    app: str
    title: str
    body: str = ""
    created: float = 0.0


class SimToasts:
    """Центр уведомлений Windows: доступ разрешён, в нём — все уведомления мира."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def access(self, request: bool = False) -> str:
        return "allowed"

    async def fetch(self) -> list[SimToast]:
        with self.world.lock:
            self.world.tick()
            return [SimToast(t.id, t.app, t.title, t.body, t.created) for t in self.world.toasts]

    async def close(self) -> None:
        return None
