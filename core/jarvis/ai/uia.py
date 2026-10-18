"""Элементы окон для агента: UI Automation на Windows (библиотека uiautomation), заглушка на других системах.

Бэкенд синхронный и вызывается только из потока платформы (ctx.run_platform): там уже инициализирован COM.
Снимок — интерактивные элементы окна (кнопки, поля, ссылки, пункты меню, вкладки, флажки…) с короткими id
e1…eN, именами, типами и прямоугольниками; id действуют до следующего снимка. Окно агент передаёт сам (hwnd
из platform.foreground_window — оно никогда не окно Jarvis): иначе, если задачу дали из окна Jarvis, агент
видел бы и нажимал интерфейс самого Jarvis. Нажатие — шаблонами UI Automation, без мыши («в фоне»); мышь —
только если элемент их не умеет и под точкой окно той же программы, а курсор человека потом возвращается на
место. Окна, клавиши и запуск программ агент делает через jarvis.platform — их бэкенд не дублирует.
"""

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.ai.format import one_line
from jarvis.platform.base import Platform, PlatformError

log = logging.getLogger(__name__)

SNAPSHOT_LIMIT = 150
_SCAN_LIMIT = 5000  # сколько узлов дерева UIA просматривать
_SCAN_SECONDS = 4.0

INTERACTIVE = {
    "ButtonControl": "Button",
    "SplitButtonControl": "SplitButton",
    "EditControl": "Edit",
    "HyperlinkControl": "Hyperlink",
    "MenuItemControl": "MenuItem",
    "ListItemControl": "ListItem",
    "TabItemControl": "TabItem",
    "CheckBoxControl": "CheckBox",
    "RadioButtonControl": "RadioButton",
    "ComboBoxControl": "ComboBox",
    "TreeItemControl": "TreeItem",
    "DocumentControl": "Document",
    "DataItemControl": "DataItem",
    "SliderControl": "Slider",
    "SpinnerControl": "Spinner",
}
TEXTUAL = {"TextControl": "Text"}


class UiError(PlatformError):
    """Окно или элемент недоступны; текст — для человека и модели."""


@dataclass
class UiElement:
    id: str
    name: str
    kind: str  # Button, Edit, Hyperlink, MenuItem, ListItem, TabItem, CheckBox, ComboBox, Document, Text…
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    enabled: bool = True
    focused: bool = False
    password: bool = False
    value: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


@dataclass
class UiSnapshot:
    window: str
    app: str = ""
    elements: list[UiElement] = field(default_factory=list)
    truncated: bool = False
    hwnd: int = 0
    pid: int = 0
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # окно на экране, физические точки
    mouse: bool = False  # последнее нажатие — настоящей мышью (у элемента нет шаблонов UIA)

    def find(self, element_id: str) -> UiElement | None:
        return next((e for e in self.elements if e.id == element_id), None)


class UiBackend(Protocol):
    def snapshot(self, limit: int = SNAPSHOT_LIMIT, hwnd: int | None = None) -> UiSnapshot: ...
    def click(self, element_id: str) -> str: ...
    def set_text(self, element_id: str, text: str) -> str: ...
    def element_at(self, x: int, y: int) -> tuple[str, str]: ...  # (имя, тип) элемента под точкой экрана
    def owner_at(self, x: int, y: int) -> tuple[int, str]: ...  # (pid, заголовок окна) под точкой экрана


def format_snapshot(snapshot: UiSnapshot) -> str:
    """Снимок окна для модели: строка на элемент — «e3 Button «Написать» [x,y,w×h] фокус»."""
    head = f"Активное окно: «{snapshot.window}»" + (f" ({snapshot.app})" if snapshot.app else "")
    lines = [head]
    if not snapshot.elements:
        lines.append("Интерактивных элементов не найдено.")
    for element in snapshot.elements:
        left, top, right, bottom = element.rect
        name = f"«{one_line(element.name, 80)}»" if element.name else "(без имени)"
        value = f" = «{one_line(element.value, 60)}»" if element.value and not element.password else ""
        flags = [
            flag
            for flag, on in (
                ("недоступен", not element.enabled),
                ("фокус", element.focused),
                ("пароль", element.password),
            )
            if on
        ]
        line = f"{element.id} {element.kind} {name}{value} [{left},{top},{right - left}x{bottom - top}]"
        lines.append(line + (" " + ", ".join(flags) if flags else ""))
    if snapshot.truncated:
        lines.append("(показаны не все элементы окна)")
    return "\n".join(lines)


class UnsupportedUi:
    """Не Windows: элементов окон нет, агент работает клавишами, командами и запуском программ."""

    MESSAGE = "Управление элементами окон работает только в Windows (UI Automation)"

    def snapshot(self, limit: int = SNAPSHOT_LIMIT, hwnd: int | None = None) -> UiSnapshot:
        raise UiError(self.MESSAGE)

    def click(self, element_id: str) -> str:
        raise UiError(self.MESSAGE)

    def set_text(self, element_id: str, text: str) -> str:
        raise UiError(self.MESSAGE)

    def element_at(self, x: int, y: int) -> tuple[str, str]:
        return "", ""

    def owner_at(self, x: int, y: int) -> tuple[int, str]:
        return 0, ""


class WindowsUi:
    """UI Automation через библиотеку uiautomation (импорт — только здесь и только на Windows)."""

    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self._controls: dict[str, Any] = {}
        self._elements: dict[str, UiElement] = {}
        self._pid = 0  # процесс окна последнего снимка: мышь жмёт, только если под точкой оно

    @staticmethod
    def _auto() -> Any:
        import uiautomation as auto  # type: ignore[import-not-found]

        auto.SetGlobalSearchTimeout(1.0)
        return auto

    def snapshot(self, limit: int = SNAPSHOT_LIMIT, hwnd: int | None = None) -> UiSnapshot:
        auto = self._auto()
        if hwnd:
            top = auto.ControlFromHandle(int(hwnd))
            if top is None:
                raise UiError("Окно закрылось — сделай ui_snapshot ещё раз")
        else:
            control = auto.GetForegroundControl()
            if control is None:
                raise UiError("Нет активного окна")
            try:
                top = control.GetTopLevelControl() or control
            except Exception:
                top = control
        window = (top.Name or "").strip()
        app, pid = "", 0
        try:
            import psutil

            pid = int(top.ProcessId)
            app = psutil.Process(pid).name()
        except Exception:
            app = ""
        try:
            box = top.BoundingRectangle
            frame = (box.left, box.top, box.right, box.bottom)
        except Exception:
            frame = (0, 0, 0, 0)
        found: list[
            tuple[int, Any, UiElement, bool]
        ] = []  # (порядок в дереве, элемент UIA, описание, текст?)
        deadline = time.monotonic() + _SCAN_SECONDS
        truncated = False
        for order, (node, _depth) in enumerate(auto.WalkControl(top, includeTop=False, maxDepth=40)):
            if order >= _SCAN_LIMIT or time.monotonic() > deadline:
                truncated = True
                break
            try:
                type_name = node.ControlTypeName
                kind = INTERACTIVE.get(type_name) or TEXTUAL.get(type_name)
                if kind is None or node.IsOffscreen:
                    continue
                rect = node.BoundingRectangle
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                name = (node.Name or "").strip()
                if kind == "Text" and not name:
                    continue
                element = UiElement(
                    "",
                    name[:200],
                    kind,
                    (rect.left, rect.top, rect.right, rect.bottom),
                    enabled=bool(node.IsEnabled),
                    focused=bool(node.HasKeyboardFocus),
                    password=bool(getattr(node, "IsPassword", False)),
                )
                if kind in ("Edit", "ComboBox") and not element.password:
                    element.value = self._value(auto, node)
            except Exception:
                continue
            found.append((order, node, element, kind == "Text"))
        interactive = [f for f in found if not f[3]]
        texts = [f for f in found if f[3]]
        if len(interactive) > limit:
            truncated = True
        chosen = interactive[:limit]
        chosen += texts[: max(0, limit - len(chosen))]
        chosen.sort(key=lambda f: f[0])
        self._controls, self._elements, self._pid = {}, {}, pid
        elements = []
        for number, (_, node, element, _) in enumerate(chosen, start=1):
            element.id = f"e{number}"
            self._controls[element.id] = node
            self._elements[element.id] = element
            elements.append(element)
        return UiSnapshot(window, app, elements, truncated, hwnd=int(hwnd or 0), pid=pid, rect=frame)

    def element_at(self, x: int, y: int) -> tuple[str, str]:
        """Имя и тип элемента под точкой (для проверки клика по картинке: не «Отправить» ли это)."""
        try:
            auto = self._auto()
            node = auto.ControlFromPoint(int(x), int(y))
            for _ in range(4):  # у значка в кнопке имени нет — берём ближайшего предка с именем
                if node is None:
                    break
                name = (node.Name or "").strip()
                if name:
                    type_name = str(node.ControlTypeName)
                    kind = INTERACTIVE.get(type_name) or type_name.replace("Control", "")
                    return name[:120], kind
                node = node.GetParentControl()
        except Exception:
            log.debug("элемент под точкой не определён", exc_info=True)
        return "", ""

    def owner_at(self, x: int, y: int) -> tuple[int, str]:
        """(pid, заголовок окна) того, что под точкой экрана; (0, "") — не узнать."""
        try:
            auto = self._auto()
            node = auto.ControlFromPoint(int(x), int(y))
            if node is None:
                return 0, ""
            pid = int(node.ProcessId)
            try:
                top = node.GetTopLevelControl() or node
                title = (top.Name or "").strip()
            except Exception:
                title = ""
            return pid, title
        except Exception:
            log.debug("окно под точкой не определено", exc_info=True)
            return 0, ""

    def _mouse_click(self, x: int, y: int) -> None:
        """Клик настоящей мышью — и курсор человека обратно. Элемент закрыт чужим окном — клика нет."""
        pid, title = self.owner_at(x, y)
        if self._pid and pid and pid != self._pid:
            raise UiError(f"Элемент закрыт другим окном («{title or 'без названия'}») — клик не сделан")
        saved: tuple[int, int] | None = None
        try:
            saved = self.platform.cursor_pos()
        except Exception:
            saved = None
        self.platform.mouse_click(x, y)
        if saved is not None:
            time.sleep(0.05)
            try:
                self.platform.mouse_move(*saved)
            except Exception:
                log.debug("курсор не вернулся на место", exc_info=True)

    @staticmethod
    def _value(auto: Any, node: Any) -> str:
        try:
            pattern = node.GetPattern(auto.PatternId.ValuePattern)
            return str(getattr(pattern, "Value", "") or "")[:200] if pattern else ""
        except Exception:
            return ""

    def _get(self, element_id: str) -> tuple[Any, UiElement]:
        node = self._controls.get(element_id)
        element = self._elements.get(element_id)
        if node is None or element is None:
            raise UiError(f"Элемента {element_id} нет в последнем снимке окна — сначала сделай ui_snapshot")
        return node, element

    def click(self, element_id: str) -> str:
        auto = self._auto()
        node, element = self._get(element_id)
        label = element.name or element_id
        try:
            framework = str(getattr(node, "FrameworkId", "") or "")
        except Exception as e:
            raise UiError(f"Элемент «{label}» больше не существует — сделай ui_snapshot") from e
        # У классических кнопок Win32/WinForms Invoke не возвращается, пока открыт модальный диалог, который
        # кнопка показала, — поток платформы бы завис. Им — настоящий клик мышью.
        if framework in ("Win32", "WinForm") and element.kind in ("Button", "SplitButton", "MenuItem"):
            order: tuple[str, ...] = ()
        elif element.kind == "CheckBox":
            order = ("TogglePattern", "InvokePattern")
        elif element.kind in ("ListItem", "TabItem", "TreeItem", "DataItem", "RadioButton"):
            order = ("SelectionItemPattern", "InvokePattern")
        elif element.kind == "ComboBox":
            order = ("ExpandCollapsePattern",)
        else:
            order = ("InvokePattern", "SelectionItemPattern", "TogglePattern")
        try:
            for pattern_name in order:
                pattern = node.GetPattern(getattr(auto.PatternId, pattern_name))
                if not pattern:
                    continue
                if pattern_name == "InvokePattern":
                    pattern.Invoke()
                    return "нажато"
                if pattern_name == "SelectionItemPattern":
                    pattern.Select()
                    return "выбрано"
                if pattern_name == "TogglePattern":
                    pattern.Toggle()
                    return "переключено"
                if pattern_name == "ExpandCollapsePattern":
                    pattern.Expand()
                    return "список раскрыт"
            x, y = element.center
            self._mouse_click(x, y)
            return "клик мышью"
        except PlatformError:
            raise
        except Exception as e:
            raise UiError(f"Не удалось нажать «{label}»: {e}") from e

    def set_text(self, element_id: str, text: str) -> str:
        auto = self._auto()
        node, element = self._get(element_id)
        label = element.name or element_id
        try:
            framework = str(getattr(node, "FrameworkId", "") or "")
            # веб-страницы (Chrome/Edge) не замечают SetValue — им печатаем по-настоящему
            if framework != "Chrome" and element.kind != "Document":
                pattern = node.GetPattern(auto.PatternId.ValuePattern)
                if pattern and not getattr(pattern, "IsReadOnly", False):
                    pattern.SetValue(text)
                    return "текст вставлен"
            try:
                node.SetFocus()
            except Exception:
                x, y = element.center
                self._mouse_click(x, y)
            self.platform.press_keys("ctrl+a")
            if "\n" in text or len(text) > 300:
                self.platform.paste_text(text)  # Enter в тексте не отправит сообщение раньше времени
            else:
                self.platform.type_text(text)
            return "текст набран"
        except PlatformError:
            raise
        except Exception as e:
            raise UiError(f"Не удалось ввести текст в «{label}»: {e}") from e


def create_ui(platform: Platform) -> UiBackend:
    if sys.platform == "win32" and getattr(platform, "name", "") == "windows":
        return WindowsUi(platform)
    return UnsupportedUi()
