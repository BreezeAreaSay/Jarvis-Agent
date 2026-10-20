"""Элементы окон для агента: UI Automation на Windows (библиотека uiautomation), заглушка на других системах.

Бэкенд синхронный и вызывается только из потока платформы (ctx.run_platform): там уже инициализирован COM.
Снимок — интерактивные элементы окна (кнопки, поля, ссылки, пункты меню, вкладки, флажки…) с короткими id
e1…eN, именами, типами и прямоугольниками; id действуют до следующего снимка. Окно агент передаёт сам (hwnd
из platform.foreground_window — оно никогда не окно Jarvis): иначе, если задачу дали из окна Jarvis, агент
видел бы и нажимал интерфейс самого Jarvis. Нажатие — шаблонами UI Automation, без мыши («в фоне»); мышь —
только если элемент их не умеет и под точкой окно той же программы, а курсор человека потом возвращается на
место. Окна, клавиши и запуск программ агент делает через jarvis.platform — их бэкенд не дублирует.
"""

import ctypes
import functools
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarvis.ai.format import one_line
from jarvis.ai.playbooks import BROWSER_EXES
from jarvis.platform.base import Platform, PlatformError

log = logging.getLogger(__name__)

SNAPSHOT_LIMIT = 150
_SCAN_LIMIT = 5000  # сколько узлов дерева UIA просматривать
_SCAN_SECONDS = 4.0
WEB_UI_LIMIT = 30  # элементов самого браузера (вкладки, адресная строка, кнопки); остальное место — странице
PAGE_TEXT_LIMIT = 2500  # символов текста видимой части страницы
_WEB_TRIES = 3  # страница пустая — Chrome ещё строит её дерево для UI Automation: снимок ещё раз
_WEB_RETRY_S = 0.4
_TREE_SCOPE_DESCENDANTS = 4
_OBJID_CLIENT = -4
_CHROME_PAGE_CLASS = "Chrome_RenderWidgetHostHWND"

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
    page: bool = False  # элемент открытой страницы (окно браузера), а не самого браузера

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
    page: str = ""  # окно браузера: заголовок открытой страницы
    page_text: str = ""  # окно браузера: текст видимой части страницы (подписи ссылок и кнопок — в elements)

    def find(self, element_id: str) -> UiElement | None:
        return next((e for e in self.elements if e.id == element_id), None)


class UiBackend(Protocol):
    def snapshot(self, limit: int = SNAPSHOT_LIMIT, hwnd: int | None = None) -> UiSnapshot: ...
    def click(self, element_id: str) -> str: ...
    def set_text(self, element_id: str, text: str) -> str: ...
    def element_at(self, x: int, y: int) -> tuple[str, str]: ...  # (имя, тип) элемента под точкой экрана
    def owner_at(self, x: int, y: int) -> tuple[int, str]: ...  # (pid, заголовок окна) под точкой экрана


def _plain(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").strip(" «»\"'").split())


def find_by_name(elements: list[UiElement], name: str) -> UiElement | None:
    """Элемент по названию (шаг сценария «Нажать кнопку в окне»): точное совпадение без учёта регистра,
    потом название, которое начинается с него или содержит его (самое короткое), потом похожее. Доступные —
    первыми."""
    want = _plain(name)
    if not want:
        return None
    named = sorted((e for e in elements if e.name), key=lambda e: (not e.enabled, len(e.name)))
    for test in (
        lambda n: n == want,
        lambda n: n.startswith(want),
        lambda n: want in n,
    ):
        found = next((e for e in named if test(_plain(e.name))), None)
        if found is not None:
            return found
    from rapidfuzz import fuzz

    scored = [(fuzz.ratio(want, _plain(e.name)), e) for e in named]
    best = max(scored, key=lambda item: item[0], default=None)
    return best[1] if best is not None and best[0] >= 85 else None


def format_snapshot(snapshot: UiSnapshot) -> str:
    """Снимок окна для модели: строка на элемент — «e3 Button «Написать» [x,y,w×h] фокус». Окно браузера —
    сначала его собственные кнопки, потом элементы страницы и текст её видимой части."""
    head = f"Активное окно: «{snapshot.window}»" + (f" ({snapshot.app})" if snapshot.app else "")
    lines = [head]
    if not snapshot.elements:
        lines.append("Интерактивных элементов не найдено.")
    web = any(e.page for e in snapshot.elements)
    if web and not snapshot.elements[0].page:
        lines.append("Браузер (вкладки, адресная строка, кнопки):")
    in_page = False
    for element in snapshot.elements:
        if element.page and not in_page:
            in_page = True
            title = f" «{one_line(snapshot.page, 100)}»" if snapshot.page else ""
            lines.append(f"Страница{title} — видимая часть (дальше — scroll и новый снимок):")
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
    if snapshot.page_text:
        lines.append(f"Текст страницы на экране: {snapshot.page_text}")
    if snapshot.truncated:
        lines.append("(показаны не все элементы окна)")
    return "\n".join(lines)


def _page_index(found: list[tuple[Any, UiElement]], frame: tuple[int, int, int, int]) -> int:
    """Документ открытой страницы: первый Document не меньше пятой части окна; нет — -1."""
    area = max(1, (frame[2] - frame[0]) * (frame[3] - frame[1]))
    for index, (_, element) in enumerate(found):
        left, top, right, bottom = element.rect
        if element.kind == "Document" and (right - left) * (bottom - top) * 5 >= area:
            return index
    return -1


def _within(element: UiElement, box: tuple[int, int, int, int]) -> bool:
    x, y = element.center
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _overlaps(element: UiElement, box: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = element.rect
    return left < box[2] and right > box[0] and top < box[3] and bottom > box[1]


def page_text(texts: list[UiElement], controls: list[UiElement], limit: int = PAGE_TEXT_LIMIT) -> str:
    """Текст видимой части страницы по порядку: без подписей ссылок и кнопок (они — в списке элементов) и без
    повторов подряд, до limit символов."""
    out: list[str] = []
    size, last = 0, ""
    for text in texts:
        name = one_line(text.name, 300)
        if not name or name == last or any(_within(text, c.rect) for c in controls):
            continue
        last = name
        out.append(name)
        size += len(name) + 3
        if size >= limit:
            break
    joined = " · ".join(out)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


def split_page(
    found: list[tuple[Any, UiElement]], frame: tuple[int, int, int, int], limit: int = SNAPSHOT_LIMIT
) -> tuple[list[tuple[Any, UiElement]], bool, str, str]:
    """Видимые элементы окна браузера (в порядке дерева) → (выбранные, обрезано ли, заголовок страницы, её
    текст). Сначала кнопки самого браузера (до WEB_UI_LIMIT), потом документ страницы и её элементы — внутри
    документа и после него в дереве; текст страницы — отдельно. Документа нет — просто видимые элементы."""
    index = _page_index(found, frame)
    if index < 0:
        items = [f for f in found if f[1].kind != "Text"]
        return items[:limit], len(items) > limit, "", ""
    doc_node, doc = found[index]
    # страница — после документа в дереве и хоть краем в нём (видна у края — тоже её); вне — полоса загрузок,
    # поиск по странице и прочее самого браузера
    in_page = [i > index and _overlaps(f[1], doc.rect) for i, f in enumerate(found)]
    ui = [f for i, f in enumerate(found) if i != index and not in_page[i] and f[1].kind != "Text"]
    page = [f for i, f in enumerate(found) if in_page[i]]
    controls = [f for f in page if f[1].kind not in ("Text", "Document")]  # вложенные документы — рамки
    ui = ui[:WEB_UI_LIMIT]
    room = max(0, limit - len(ui) - 1)
    doc.page = True
    for _, element in controls:
        element.page = True
    text = page_text([f[1] for f in page if f[1].kind == "Text"], [f[1] for f in controls])
    return [*ui, (doc_node, doc), *controls[:room]], len(controls) > room, doc.name, text


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
        if app.casefold() in BROWSER_EXES:
            try:
                chosen, cut, page, text = self._web(auto, top, limit, frame)
            except Exception:
                log.info("быстрый снимок окна браузера не получился — обхожу дерево", exc_info=True)
            else:
                snap = self._keep(chosen, window, app, cut, hwnd, pid, frame)
                snap.page, snap.page_text = page, text
                return snap
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
        pairs = [(node, element) for _, node, element, _ in chosen]
        return self._keep(pairs, window, app, truncated, hwnd, pid, frame)

    def _keep(
        self,
        chosen: list[tuple[Any, UiElement]],
        window: str,
        app: str,
        truncated: bool,
        hwnd: int | None,
        pid: int,
        frame: tuple[int, int, int, int],
    ) -> UiSnapshot:
        """Номера e1…eN по порядку; элементы запоминаются для нажатий до следующего снимка."""
        self._controls, self._elements, self._pid = {}, {}, pid
        elements = []
        for number, (node, element) in enumerate(chosen, start=1):
            element.id = f"e{number}"
            self._controls[element.id] = node
            self._elements[element.id] = element
            elements.append(element)
        return UiSnapshot(window, app, elements, truncated, hwnd=int(hwnd or 0), pid=pid, rect=frame)

    def forget(self) -> None:
        """Снимок сделан не UI Automation (страница браузера Jarvis): id e… прошлого снимка не действуют."""
        self._controls, self._elements = {}, {}

    def _web(
        self, auto: Any, top: Any, limit: int, frame: tuple[int, int, int, int]
    ) -> tuple[list[tuple[Any, UiElement]], bool, str, str]:
        """Окно браузера одним запросом UI Automation (FindAllBuildCache): все видимые элементы сразу со
        свойствами. Обход дерева по узлу (вызов в процесс браузера на каждое свойство) на тяжёлой странице не
        успевает дойти до её содержимого. Страница ещё пустая — Chrome строит её дерево, когда его об этом
        спросят: разбудить и снять ещё раз."""
        from uiautomation import uiautomation as core  # type: ignore[import-not-found]  # клиент — не в пакете

        uia = core._AutomationClient.instance().IUIAutomation
        prop = auto.PropertyId
        kinds = {
            int(getattr(auto.ControlType, name)): kind for name, kind in {**INTERACTIVE, **TEXTUAL}.items()
        }
        by_type = [uia.CreatePropertyCondition(prop.ControlTypeProperty, t) for t in kinds]
        condition = uia.CreateAndCondition(
            functools.reduce(uia.CreateOrCondition, by_type),
            uia.CreatePropertyCondition(prop.IsOffscreenProperty, False),
        )
        cache = uia.CreateCacheRequest()
        for prop_id in (
            prop.NameProperty,
            prop.ControlTypeProperty,
            prop.BoundingRectangleProperty,
            prop.IsEnabledProperty,
            prop.HasKeyboardFocusProperty,
            prop.IsPasswordProperty,
            prop.ValueValueProperty,
        ):
            cache.AddProperty(prop_id)
        result: tuple[list[tuple[Any, UiElement]], bool, str, str] = ([], False, "", "")
        for attempt in range(_WEB_TRIES):
            found = self._find_all(top.Element, condition, cache, kinds, prop.ValueValueProperty)
            result = split_page(found, frame, limit)
            if result[3] or any(e.page and e.kind != "Document" for _, e in result[0]):
                break
            if attempt + 1 < _WEB_TRIES:
                if attempt == 0:
                    self._wake(top)
                time.sleep(_WEB_RETRY_S)
        return result

    @staticmethod
    def _find_all(
        root: Any, condition: Any, cache: Any, kinds: dict[int, str], value_prop: int
    ) -> list[tuple[Any, UiElement]]:
        """Видимые элементы нужных типов под root — в порядке дерева, со свойствами из кэша запроса."""
        array = root.FindAllBuildCache(_TREE_SCOPE_DESCENDANTS, condition, cache)
        out: list[tuple[Any, UiElement]] = []
        for i in range(int(array.Length) if array else 0):
            try:
                node = array.GetElement(i)
                kind = kinds.get(int(node.CachedControlType))
                box = node.CachedBoundingRectangle
                rect = (int(box.left), int(box.top), int(box.right), int(box.bottom))
                if kind is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
                    continue
                name = str(node.CachedName or "").strip()
                if kind == "Text" and not name:
                    continue
                element = UiElement(
                    "",
                    name[:200],
                    kind,
                    rect,
                    enabled=bool(node.CachedIsEnabled),
                    focused=bool(node.CachedHasKeyboardFocus),
                    password=bool(node.CachedIsPassword),
                )
                if kind in ("Edit", "ComboBox") and not element.password:
                    value = node.GetCachedPropertyValue(value_prop)
                    element.value = value[:200] if isinstance(value, str) else ""
            except Exception:
                continue
            out.append((node, element))
        return out

    @staticmethod
    def _wake(top: Any) -> None:
        """Chrome и Edge строят дерево страницы, когда его спросят, — как экранный диктор: объект доступности
        окна страницы (Chrome_RenderWidgetHostHWND) через AccessibleObjectFromWindow; ссылка сразу
        отпускается."""
        try:
            from comtypes import POINTER, IUnknown  # type: ignore[import-not-found]

            from jarvis.platform.windows import winapi as wa

            w = wa.api()
            hwnd = int(top.NativeWindowHandle or 0)
            if not hwnd:
                return
            pages: list[int] = []
            name = ctypes.create_unicode_buffer(64)

            def visit(child: Any, _lparam: Any) -> bool:
                if child and w.GetClassNameW(child, name, 64) and name.value == _CHROME_PAGE_CLASS:
                    pages.append(int(child))
                return True

            w.EnumChildWindows(hwnd, w.WNDENUMPROC(visit), 0)
            oleacc = ctypes.oledll.oleacc  # type: ignore[attr-defined]
            for child in pages[:4]:
                obj = POINTER(IUnknown)()
                oleacc.AccessibleObjectFromWindow(
                    ctypes.c_void_p(child),
                    ctypes.c_ulong(_OBJID_CLIENT & 0xFFFFFFFF),
                    ctypes.byref(IUnknown._iid_),
                    ctypes.byref(obj),
                )
                del obj  # comtypes отпускает ссылку сам
        except Exception:
            log.debug("дерево страницы для UI Automation не разбужено", exc_info=True)

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
        # сырой элемент UIA из быстрого снимка браузера — обёртка uiautomation, чтобы нажимать шаблонами
        if not hasattr(node, "GetPattern"):
            control = self._auto().Control.CreateControlFromElement(node)
            if control is None:
                raise UiError(
                    f"Элемент «{element.name or element_id}» исчез со страницы — сделай ui_snapshot"
                )
            self._controls[element_id] = node = control
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
