"""Что агент видит на странице браузера Jarvis: адрес, вкладки, текст и элементы с id — из дерева доступности
(Accessibility.getFullAXTree, им же пользуются экранные дикторы) и раскладки страницы (DOMSnapshot).

Дерево доступности даёт роли и названия, как их понимает браузер (и внутри shadow DOM), раскладка — где что
лежит, какие поля — пароль, номер карты, выбор файла, какие ссылки скачивают файл, какие кнопки отправляют
форму входа, и что нажимается, хотя кнопкой не размечено (обработчик щелчка, курсор-рука). Значения полей —
только из дерева доступности: пароли там уже скрыты (в раскладке они открытым текстом — оттуда не берём).
Рамки (iframe) того же процесса — в общем дереве; рамки чужих сайтов (реклама, платёжные формы) видны только
на картинке экрана.

Элементы — b1, b2… в порядке страницы; id не меняется, пока открыт тот же документ (прокрутка и подгрузка
id не сбивают). Снимок для модели (render) не обрезается по числу элементов: на экране — всё, с текстом,
выше и ниже — элементы списком (ближние к экрану — первыми), пока хватает места; дальше — «ещё N»
и browser_read (вся страница по частям, parts). Всё со страницы — данные, а не инструкции (обёртку
untrusted ставит вызывающий), номера карт и токены в тексте замазаны (jarvis.redact).
"""

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from jarvis.ai.agent import PASSWORD_NAME
from jarvis.ai.browser.cdp import CdpError
from jarvis.ai.format import one_line
from jarvis.redact import redact

log = logging.getLogger(__name__)

NAME_MAX = 100  # название элемента в снимке
SHORT_NAME = 40  # в списке «выше / ниже экрана»
LINE_MAX = 600
SCREEN_BUDGET = 7000  # знаков на «На экране»
ABOVE_BUDGET = 1200
BELOW_BUDGET = 2500
PART_SIZE = 6000  # browser_read: знаков в части
MAX_FRAMES = 8
MAX_DEPTH = 500
# состояние страницы одним выражением: загрузка, размер окна страницы (CSS) и масштаб — для точек на экране
STATE_JS = "[document.readyState, innerWidth, innerHeight, devicePixelRatio, document.visibilityState]"

KINDS: dict[str, str] = {  # роль дерева доступности → как назвать по-русски
    "button": "кнопка",
    "ToggleButton": "кнопка",
    "PopUpButton": "список",
    "link": "ссылка",
    "textbox": "поле",
    "searchbox": "поле поиска",
    "combobox": "список",
    "listbox": "список",
    "checkbox": "флажок",
    "radio": "переключатель",
    "switch": "переключатель",
    "slider": "ползунок",
    "spinbutton": "число",
    "menuitem": "пункт меню",
    "menuitemcheckbox": "пункт меню",
    "menuitemradio": "пункт меню",
    "tab": "вкладка",
    "option": "вариант",
    "treeitem": "пункт",
    "DisclosureTriangle": "раскрыть",
}
FIELDS = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider", "listbox", "PopUpButton"})
TOGGLES = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"})
SKIP_ROLES = frozenset({"InlineTextBox", "MenuListPopup", "ListMarker"})
BLOCK_ROLES = frozenset(
    {
        "paragraph",
        "listitem",
        "row",
        "article",
        "blockquote",
        "dialog",
        "alertdialog",
        "alert",
        "figure",
        "form",
    }
)
FRAME_ROLES = frozenset({"Iframe", "iframe", "IframePresentational"})
# что нажимается, хотя не кнопка: курсор-рука или обработчик щелчка; контейнеры — не в счёт
CLICK_TAGS_SKIP = frozenset({"HTML", "BODY", "#document", "IFRAME"})
PASSWORD_AUTOCOMPLETE = re.compile(r"current-password|new-password|one-time-code", re.I)
CARD_FIELD = re.compile(r"^cc-|card.?num|cardnumber|\bcvc\b|\bcvv\b|cc-?csc|номер карты|security.?code", re.I)
DOWNLOAD_EXT = re.compile(
    r"\.(exe|msi|msix|appx|bat|cmd|ps1|scr|zip|rar|7z|tar|gz|tgz|iso|dmg|apk|torrent|jar|vbs|docm|xlsm)$",
    re.I,
)
CAPTCHA_URL = re.compile(
    r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare\.com|captcha|smartcaptcha", re.I
)
CAPTCHA_TEXT = re.compile(
    r"я не робот|i'?m not a robot|подтвердите, что (?:вы )?не робот|verify (?:that )?you are human|"
    r"\bcaptcha\b|\bкапч",
    re.I,
)


@dataclass
class Element:
    id: str
    backend: int  # backendDOMNodeId: по нему браузер находит элемент для нажатия и ввода
    role: str
    kind: str  # по-русски: кнопка, ссылка, поле…
    name: str
    value: str = ""
    url: str = ""  # куда ведёт ссылка
    tag: str = ""
    rect: tuple[float, float, float, float] | None = None  # x, y, ширина, высота — в точках CSS окна страницы
    top: float | None = None  # где на странице (для «выше / на экране / ниже»)
    focused: bool = False
    disabled: bool = False
    checked: str = ""  # true / false / mixed
    expanded: bool | None = None
    selected: bool = False
    readonly: bool = False
    required: bool = False
    invalid: bool = False
    password: bool = False  # поле пароля или кода
    card: bool = False  # поле номера карты или CVC
    file: bool = False  # выбор файла для загрузки на сайт
    download: bool = False  # ссылка скачивает файл
    login: bool = False  # кнопка (или поле: Enter) отправляет форму с паролем
    editable: bool = False
    frame: bool = False  # внутри рамки (iframe)

    @property
    def is_field(self) -> bool:
        return self.editable or self.role in FIELDS

    def token(self, short: bool = False) -> str:
        """«b3 поле поиска «Поиск» = «наушники», фокус» — как элемент выглядит в снимке."""
        limit = SHORT_NAME if short else NAME_MAX
        name = f" «{one_line(self.name, limit)}»" if self.name else ""
        if short:
            return f"{self.id} {self.kind}{name}"
        value = ""
        if self.password:
            value = " = ••••" if self.value else ""
        elif self.value and self.value != self.name:
            value = f" = «{one_line(redact(self.value), 60)}»"
        flags = [
            text
            for text, on in (
                ("пароль", self.password),
                ("номер карты", self.card and not self.password),
                ("отмечен" if self.checked == "true" else "не отмечен", self.checked in ("true", "false")),
                ("частично", self.checked == "mixed"),
                ("раскрыт" if self.expanded else "свёрнут", self.expanded is not None),
                ("выбран", self.selected),
                ("фокус", self.focused),
                ("недоступен", self.disabled),
                ("только чтение", self.readonly and self.is_field),
                ("обязательное", self.required),
                ("с ошибкой", self.invalid),
                ("скачивает файл", self.download),
            )
            if on
        ]
        target = f" → {self.url}" if self.url and self.role == "link" else ""
        tail = (", " + ", ".join(flags)) if flags else ""
        return f"{self.id} {self.kind}{name}{value}{target}{tail}"


@dataclass
class Line:
    text: str
    top: float | None = None
    bottom: float | None = None
    elements: list[str] = field(default_factory=list)


@dataclass
class Page:
    target: str
    session: str
    url: str
    title: str
    lines: list[Line] = field(default_factory=list)
    elements: dict[str, Element] = field(default_factory=dict)
    scroll: tuple[float, float] = (0.0, 0.0)  # прокрутка, точки CSS
    viewport: tuple[float, float] = (0.0, 0.0)  # видимая часть страницы, точки CSS
    content: tuple[float, float] = (0.0, 0.0)  # вся страница
    inner: tuple[float, float] = (0.0, 0.0)  # окно страницы (innerWidth/innerHeight)
    dpr: float = 1.0  # точек экрана на точку CSS
    ready: str = "complete"
    dialog: dict[str, Any] | None = None  # окно сайта поверх страницы — тогда страница не отвечает
    notes: list[str] = field(default_factory=list)
    focused: str = ""  # id элемента с фокусом
    captured: float = field(default_factory=time.monotonic)
    parents: dict[int, int] = field(
        default_factory=dict
    )  # узел → родитель: что закрывает элемент при нажатии
    origin: tuple[float, float] | None = None  # левый верх окна страницы на экране (физические точки)

    def find(self, element_id: str) -> Element | None:
        return self.elements.get(element_id.strip().casefold())

    def inside(self, inner: int, outer: int) -> bool:
        """Узел inner — это outer или лежит внутри него (по раскладке последнего снимка)."""
        seen = 0
        while inner and seen < 200:
            if inner == outer:
                return True
            inner, seen = self.parents.get(inner, 0), seen + 1
        return False

    def owner_of(self, backend: int) -> Element | None:
        """Ближайший элемент снимка, внутри которого лежит узел (что закрывает кнопку)."""
        by_backend = {e.backend: e for e in self.elements.values()}
        seen = 0
        while backend and seen < 200:
            if backend in by_backend:
                return by_backend[backend]
            backend, seen = self.parents.get(backend, 0), seen + 1
        return None

    def to_screen(self, x: float, y: float) -> tuple[int, int] | None:
        """Точка окна страницы (CSS) → точка экрана, если знаем, где окно страницы на экране."""
        if self.origin is None:
            return None
        return int(self.origin[0] + x * self.dpr), int(self.origin[1] + y * self.dpr)

    @property
    def host(self) -> str:
        return site_of(self.url)

    def where(self, line: Line) -> int:
        """-1 — выше экрана, 0 — на экране, 1 — ниже."""
        top, bottom = line.top, line.bottom
        if top is None or bottom is None:
            return 0
        start, end = self.scroll[1], self.scroll[1] + max(1.0, self.viewport[1])
        if bottom <= start + 1:
            return -1
        if top >= end - 1:
            return 1
        return 0


class Ids:
    """id элементов вкладки: тот же элемент (backendNodeId) того же документа — тот же b-номер."""

    def __init__(self) -> None:
        self.document = 0
        self.known: dict[int, str] = {}
        self.next = 1

    def reset(self, document: int) -> None:
        if document != self.document:
            self.document, self.known, self.next = document, {}, 1

    def get(self, backend: int) -> str:
        found = self.known.get(backend)
        if found is None:
            found = self.known[backend] = f"b{self.next}"
            self.next += 1
        return found


def site_of(url: str) -> str:
    with contextlib.suppress(ValueError):
        return re.sub(r"^www\.", "", (urlparse(url).hostname or "").casefold())
    return ""


def short_url(url: str, base: str = "") -> str:
    """Куда ведёт ссылка: на тот же сайт — путь, на другой — сайт и путь; данные в адресе замазаны."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return parsed.scheme + ":" if parsed.scheme else ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    same = site_of(url) == site_of(base) and bool(base)
    text = path if same else f"{site_of(url)}{path if path != '/' else ''}"
    return one_line(redact(unquote(text)), 50)


def readable(url: str) -> str:
    """Адрес для модели: %D0%BD… — буквами, данные в адресе замазаны."""
    with contextlib.suppress(ValueError):
        url = unquote(url)
    return redact(url)


# --- раскладка страницы (DOMSnapshot) ----------------------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: int  # backendNodeId родителя (0 — нет)
    rect: tuple[float, float, float, float] | None = None  # в окне страницы, CSS
    top: float | None = None  # на странице главного документа
    clickable: bool = False
    pointer: bool = False
    frame: bool = False


_ATTRS = frozenset(
    {
        "type",
        "autocomplete",
        "download",
        "href",
        "name",
        "id",
        "placeholder",
        "src",
        "contenteditable",
        "form",
    }
)


class _Dom:
    """backendNodeId → тег, нужные атрибуты, родитель и прямоугольник; формы с паролем."""

    def __init__(self, snapshot: dict[str, Any], scroll: tuple[float, float]) -> None:
        self.nodes: dict[int, _Node] = {}
        self.login_forms: set[int] = set()
        strings = snapshot.get("strings") or []

        def text(index: Any) -> str:
            return strings[index] if isinstance(index, int) and 0 <= index < len(strings) else ""

        documents = snapshot.get("documents") or []
        # где документ рамки на странице: (смещение x, смещение y, узел-владелец) — по iframe в документе выше
        offsets: dict[int, tuple[float, float, int]] = {0: (0.0, 0.0, 0)}
        done: set[int] = set()
        while True:
            ready = [i for i in offsets if i not in done and i < len(documents)]
            if not ready:
                break  # остальные рамки — без владельца в снимке
            index = ready[0]
            done.add(index)
            doc = documents[index]
            base_x, base_y, owner = offsets[index]
            nodes = doc.get("nodes") or {}
            backends = nodes.get("backendNodeId") or []
            parents = nodes.get("parentIndex") or []
            names = nodes.get("nodeName") or []
            attributes = nodes.get("attributes") or []
            clickable = set((nodes.get("isClickable") or {}).get("index") or [])
            content = nodes.get("contentDocumentIndex") or {}
            layout = doc.get("layout") or {}
            styles = layout.get("styles") or []
            bounds_of: dict[int, list[float]] = {}
            pointer: set[int] = set()
            for k, node_index in enumerate(layout.get("nodeIndex") or []):
                bounds = (layout.get("bounds") or [])[k] if k < len(layout.get("bounds") or []) else None
                if bounds and len(bounds) >= 4:
                    bounds_of[node_index] = bounds
                style = styles[k] if k < len(styles) else []
                if style and text(style[0]) == "pointer":
                    pointer.add(node_index)
            scroll_x = float(doc.get("scrollOffsetX") or 0) if index else 0.0
            scroll_y = float(doc.get("scrollOffsetY") or 0) if index else 0.0
            for i, backend in enumerate(backends):
                parent_index = parents[i] if i < len(parents) else -1
                parent = backends[parent_index] if 0 <= parent_index < len(backends) else owner
                attrs: dict[str, str] = {}
                raw = attributes[i] if i < len(attributes) else []
                for a in range(0, len(raw) - 1, 2):
                    key = text(raw[a]).casefold()
                    if key in _ATTRS:
                        attrs[key] = text(raw[a + 1])
                node = _Node(text(names[i]) if i < len(names) else "", attrs, parent, frame=index > 0)
                bounds = bounds_of.get(i)
                if bounds is not None:
                    x = bounds[0] - scroll_x + base_x
                    page_y = bounds[1] - scroll_y + base_y
                    node.top = page_y
                    node.rect = (x - scroll[0], page_y - scroll[1], float(bounds[2]), float(bounds[3]))
                node.clickable = i in clickable
                node.pointer = i in pointer and (parent_index < 0 or parent_index not in pointer)
                self.nodes[int(backend)] = node
            for i, child in zip(content.get("index") or [], content.get("value") or [], strict=False):
                bounds = bounds_of.get(i)
                if bounds is not None and isinstance(child, int):
                    offsets[child] = (
                        bounds[0] - scroll_x + base_x,
                        bounds[1] - scroll_y + base_y,
                        int(backends[i]),
                    )
        for backend, node in self.nodes.items():
            if node.tag == "INPUT" and node.attrs.get("type", "").casefold() == "password":
                form = self.form_of(backend)
                if form:
                    self.login_forms.add(form)

    def get(self, backend: Any) -> _Node | None:
        return self.nodes.get(backend) if isinstance(backend, int) else None

    def form_of(self, backend: int) -> int:
        seen = 0
        while backend and seen < 60:
            node = self.nodes.get(backend)
            if node is None:
                return 0
            if node.tag == "FORM":
                return backend
            backend, seen = node.parent, seen + 1
        return 0

    def inside(self, inner: int, outer: int) -> bool:
        """inner — это outer или лежит внутри него."""
        seen = 0
        while inner and seen < 200:
            if inner == outer:
                return True
            node = self.nodes.get(inner)
            if node is None:
                return False
            inner, seen = node.parent, seen + 1
        return False


# --- дерево доступности → строки и элементы ------------------------------------------------------------


@dataclass
class _Piece:
    text: str
    top: float | None = None
    bottom: float | None = None
    element: str = ""
    newline: bool = False


def _value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else None


def _props(node: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prop in node.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name"):
            out[str(prop["name"])] = _value(prop.get("value"))
    return out


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


class _Builder:
    def __init__(
        self, frames: dict[str, list[dict[str, Any]]], owners: dict[int, str], dom: _Dom, ids: Ids, url: str
    ) -> None:
        self.trees = {key: {str(n.get("nodeId")): n for n in nodes} for key, nodes in frames.items()}
        self.owners = owners
        self.dom = dom
        self.ids = ids
        self.url = url
        self.pieces: list[_Piece] = []
        self.elements: dict[str, Element] = {}
        self.hidden_frames = 0
        self.captcha = False
        self.focused = ""
        self._break = True

    def root(self, frame: str) -> dict[str, Any] | None:
        nodes = self.trees.get(frame) or {}
        roots = [n for n in nodes.values() if not n.get("parentId")]
        return next((n for n in roots if _value(n.get("role")) == "RootWebArea"), roots[0] if roots else None)

    def build(self) -> None:
        root = self.root("")
        if root is not None:
            self.ids.reset(int(root.get("backendDOMNodeId") or 0))
            self.node("", root, "", 0)

    def node(self, frame: str, node: dict[str, Any] | None, covered: str, depth: int) -> None:
        if node is None or depth > MAX_DEPTH:
            return
        tree = self.trees.get(frame) or {}
        children = [tree.get(str(c)) for c in node.get("childIds") or []]
        if node.get("ignored"):
            for child in children:
                self.node(frame, child, covered, depth + 1)
            return
        role = str(_value(node.get("role")) or "")
        name = _clean(_value(node.get("name")))
        backend = node.get("backendDOMNodeId")
        if role in SKIP_ROLES:
            return
        if role in FRAME_ROLES:
            self.frame(backend, covered, depth)
            return
        if role == "StaticText":
            if name and not (covered and name in covered):
                self.text(name, backend)
            return
        if role == "LineBreak":
            self._break = True
            return
        props = _props(node)
        if role == "RootWebArea" and depth == 0 and CAPTCHA_URL.search(str(props.get("url") or "")):
            self.captcha = True
        kind = self.kind(role, props, backend)
        if kind:
            element = self.element(kind, role, name, props, backend, node)
            if element.is_field:
                return  # внутри поля — его же текст
            inner = element.name if len(element.name) <= NAME_MAX else ""
            for child in children:
                self.node(frame, child, inner or covered, depth + 1)
            return
        start = len(self.pieces)
        if role == "heading":
            level = props.get("level") if isinstance(props.get("level"), int) else 2
            self._break = True
            self.pieces.append(_Piece("#" * max(1, min(int(level), 4)), *self.span(backend), newline=True))
            self._break = False  # текст заголовка — в той же строке
        elif role in BLOCK_ROLES:
            self._break = True
        elif role in ("image", "img") and name and not (covered and name in covered) and len(name) >= 3:
            self.text(f"[картинка: {one_line(name, 80)}]", backend)
        for child in children:
            self.node(frame, child, covered, depth + 1)
        if role == "heading" or role in BLOCK_ROLES:
            self._break = True
        self.clickable(start, role, backend, depth)

    def frame(self, backend: Any, covered: str, depth: int) -> None:
        key = self.owners.get(backend) if isinstance(backend, int) else None
        if key is not None and key in self.trees:
            self._break = True
            self.node(key, self.root(key), covered, depth + 1)
            self._break = True
            return
        owner = self.dom.get(backend)
        if owner is not None and CAPTCHA_URL.search(owner.attrs.get("src", "")):
            self.captcha = True
        if owner is not None and owner.rect is not None and owner.rect[2] >= 40 and owner.rect[3] >= 40:
            self.hidden_frames += 1

    def span(self, backend: Any) -> tuple[float | None, float | None]:
        node = self.dom.get(backend)
        if node is None or node.top is None or node.rect is None:
            return None, None
        return node.top, node.top + max(1.0, node.rect[3])

    def text(self, text: str, backend: Any) -> None:
        top, bottom = self.span(backend)
        self.pieces.append(_Piece(text, top, bottom, newline=self._break))
        self._break = False

    def kind(self, role: str, props: dict[str, Any], backend: Any) -> str:
        if not isinstance(backend, int):
            return ""  # без узла страницы — не нажать и не ввести: просто текст
        if role in KINDS:
            dom = self.dom.get(backend)
            if role == "option" and (dom is None or dom.rect is None):
                return ""  # вариант свёрнутого списка — значение списка его уже показывает
            return KINDS[role]
        if props.get("editable") in ("richtext", "plaintext") and props.get("focusable"):
            return "поле"
        return ""

    def element(
        self, kind: str, role: str, name: str, props: dict[str, Any], backend: Any, node: dict[str, Any]
    ) -> Element:
        dom = self.dom.get(backend)
        backend_id = int(backend)
        element = Element(
            self.ids.get(backend_id),
            backend_id,
            role,
            kind,
            one_line(name, 300),
            value=_clean(_value(node.get("value")))[:300],
            tag=dom.tag if dom is not None else "",
            focused=bool(props.get("focused")),
            disabled=bool(props.get("disabled")),
            checked=str(props.get("checked") or "") if role in TOGGLES else "",
            expanded=props.get("expanded") if isinstance(props.get("expanded"), bool) else None,
            selected=bool(props.get("selected")) and role in ("tab", "option", "treeitem"),
            readonly=bool(props.get("readonly")),
            required=bool(props.get("required")),
            invalid=str(props.get("invalid") or "false") not in ("false", ""),
            editable=props.get("editable") in ("richtext", "plaintext"),
        )
        if role == "link":
            element.url = short_url(str(props.get("url") or ""), self.url)
        if dom is not None:
            element.rect, element.top, element.frame = dom.rect, dom.top, dom.frame
            kind_attr = dom.attrs.get("type", "").casefold()
            autocomplete = dom.attrs.get("autocomplete", "")
            labels = " ".join(
                (name, dom.attrs.get("name", ""), dom.attrs.get("id", ""), dom.attrs.get("placeholder", ""))
            )
            element.password = kind_attr == "password" or bool(PASSWORD_AUTOCOMPLETE.search(autocomplete))
            element.card = bool(CARD_FIELD.search(autocomplete) or CARD_FIELD.search(labels))
            if dom.tag == "INPUT" and kind_attr == "file":
                element.file, element.kind = True, "выбор файла"
            if role == "link":
                href = dom.attrs.get("href", "")
                path = urlparse(href).path if href else ""
                element.download = "download" in dom.attrs or bool(DOWNLOAD_EXT.search(path))
            form = self.dom.form_of(backend_id)
            if form in self.dom.login_forms:
                submit = (dom.tag == "BUTTON" and kind_attr in ("", "submit")) or (
                    dom.tag == "INPUT" and kind_attr in ("submit", "image")
                )
                element.login = submit or element.is_field
        if (
            element.is_field
            and (element.password or bool(PASSWORD_NAME.search(name)))
            and element.role != "combobox"
        ):
            element.password = True
        self.elements[element.id] = element
        if element.focused:
            self.focused = element.id
        top, bottom = self.span(backend)
        self.pieces.append(_Piece("", top, bottom, element=element.id, newline=self._break))
        self._break = False
        return element

    def clickable(self, start: int, role: str, backend: Any, depth: int) -> None:
        """Нажимается, хотя не кнопка (div с обработчиком щелчка, курсор-рука): одним элементом, если внутри
        нет других элементов и текста немного."""
        dom = self.dom.get(backend)
        if dom is None or depth == 0 or dom.tag in CLICK_TAGS_SKIP or not (dom.clickable or dom.pointer):
            return
        pieces = self.pieces[start:]
        if not pieces or any(p.element for p in pieces):
            return
        text = one_line(" ".join(p.text for p in pieces if not p.text.startswith("#")), 300)
        if not text or len(text) > 150:
            return
        backend_id = int(backend)
        element = Element(
            self.ids.get(backend_id),
            backend_id,
            role or "generic",
            "элемент",
            text,
            tag=dom.tag,
            rect=dom.rect,
            top=dom.top,
            frame=dom.frame,
        )
        self.elements[element.id] = element
        first = pieces[0]
        top, bottom = self.span(backend)
        self.pieces[start:] = [_Piece("", top, bottom, element=element.id, newline=first.newline)]


def _lines(pieces: list[_Piece], elements: dict[str, Element]) -> list[Line]:
    """Куски текста и элементы → строки, как они стоят на странице: что рядом по высоте — в одну строку."""
    lines: list[Line] = []
    current: Line | None = None
    for piece in pieces:
        token = f"[{elements[piece.element].token()}]" if piece.element else piece.text
        if not token:
            continue
        same = current is not None and not piece.newline and len(current.text) < LINE_MAX
        if same and current is not None and piece.top is not None and current.top is not None:
            assert piece.bottom is not None and current.bottom is not None
            overlap = min(current.bottom, piece.bottom) - max(current.top, piece.top)
            same = overlap > 0.5 * min(current.bottom - current.top, piece.bottom - piece.top)
        if same and current is not None:
            current.text = f"{current.text} {token}"
            if piece.top is not None and piece.bottom is not None:
                current.top = piece.top if current.top is None else min(current.top, piece.top)
                current.bottom = piece.bottom if current.bottom is None else max(current.bottom, piece.bottom)
        else:
            previous = current
            current = Line(token, piece.top, piece.bottom)
            if current.top is None and previous is not None:  # без раскладки — там же, где предыдущая
                current.top, current.bottom = previous.top, previous.bottom
            lines.append(current)
        if piece.element:
            current.elements.append(piece.element)
    return [line for line in lines if line.text.strip("# ")]


def build(
    target: str,
    session: str,
    url: str,
    title: str,
    frames: dict[str, list[dict[str, Any]]],
    owners: dict[int, str],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    state: Any,
    ids: Ids,
) -> Page:
    """Снимок страницы из ответов браузера (дерево доступности по рамкам, DOMSnapshot, размеры окна)."""
    visual = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    scroll = (float(visual.get("pageX") or 0), float(visual.get("pageY") or 0))
    page = Page(target, session, url, title)
    page.scroll = scroll
    page.viewport = (float(visual.get("clientWidth") or 0), float(visual.get("clientHeight") or 0))
    page.content = (float(size.get("width") or 0), float(size.get("height") or 0))
    if isinstance(state, list) and len(state) >= 4:
        with contextlib.suppress(TypeError, ValueError):
            page.ready = str(state[0] or "")
            page.inner = (float(state[1]), float(state[2]))
            page.dpr = float(state[3]) or 1.0
    dom = _Dom(snapshot, scroll)
    builder = _Builder(frames, owners, dom, ids, url)
    builder.build()
    page.elements = builder.elements
    page.parents = {backend: node.parent for backend, node in dom.nodes.items()}
    page.lines = _lines(builder.pieces, builder.elements)
    page.focused = builder.focused
    if not page.title:
        root = builder.root("")
        page.title = _clean(_value(root.get("name"))) if root is not None else ""
    text = " ".join(line.text for line in page.lines[:400])
    if builder.captcha or CAPTCHA_TEXT.search(text):
        page.notes.append(
            "На странице проверка «я не робот» (капча) — не проходи её сам: попроси пользователя "
            "(finish, status=partial)."
        )
    if any(e.password for e in page.elements.values()):
        page.notes.append(
            "На странице вход (поле пароля): пароль вводи, только если пользователь дал его в задаче; иначе "
            "попроси его войти самого в браузере Jarvis."
        )
    if builder.hidden_frames:
        page.notes.append(
            f"Встроенных окон других сайтов на странице: {builder.hidden_frames} — их содержимое видно "
            "только "
            "на картинке экрана."
        )
    return page


async def capture(
    browser: Any, target: str, session: str, ids: Ids, *, url: str = "", title: str = ""
) -> Page:
    """Снять страницу вкладки: размеры, дерево доступности (с рамками того же процесса) и раскладку — разом.
    Открыто окно сайта (alert, confirm) — страница не отвечает: снимок без содержимого, с этим окном."""
    dialog = browser.dialogs.get(session)
    if dialog is not None:
        page = Page(target, session, url, title, dialog=dict(dialog))
        page.ready = "dialog"
        return page

    async def frames() -> tuple[dict[str, list[dict[str, Any]]], dict[int, str]]:
        found: dict[str, list[dict[str, Any]]] = {}
        owners: dict[int, str] = {}
        try:
            tree = await browser.send("Page.getFrameTree", session=session, timeout=5.0)
        except (CdpError, TimeoutError):
            return found, owners
        queue = list((tree.get("frameTree") or {}).get("childFrames") or [])
        while queue and len(found) < MAX_FRAMES:
            item = queue.pop(0)
            frame_id = str((item.get("frame") or {}).get("id") or "")
            queue.extend(item.get("childFrames") or [])
            try:
                owner = await browser.send(
                    "DOM.getFrameOwner", {"frameId": frame_id}, session=session, timeout=3.0
                )
                nodes = await browser.send(
                    "Accessibility.getFullAXTree", {"frameId": frame_id}, session=session, timeout=8.0
                )
            except (CdpError, TimeoutError):
                continue  # рамка другого сайта (свой процесс) — только на картинке
            backend = owner.get("backendNodeId")
            if isinstance(backend, int):
                found[frame_id], owners[backend] = list(nodes.get("nodes") or []), frame_id
        return found, owners

    metrics, state, tree, snapshot, (children, owners) = await asyncio.gather(
        browser.send("Page.getLayoutMetrics", session=session, timeout=5.0),
        browser.evaluate(session, STATE_JS),
        browser.send("Accessibility.getFullAXTree", session=session, timeout=15.0),
        browser.send(
            "DOMSnapshot.captureSnapshot", {"computedStyles": ["cursor"]}, session=session, timeout=15.0
        ),
        frames(),
    )
    all_frames = {"": list(tree.get("nodes") or []), **children}
    return build(target, session, url, title, all_frames, owners, snapshot, metrics, state, ids)


# --- снимок для модели ---------------------------------------------------------------------------------


def _position(page: Page) -> str:
    height = max(1.0, page.viewport[1])
    total = max(page.content[1], height)
    screens = max(1, int((total + height - 1) // height))
    current = min(screens, int(page.scroll[1] // height) + 1)
    if screens == 1:
        return "страница — на одном экране"
    if page.scroll[1] <= 1:
        return f"экран 1 из {screens} (верх страницы)"
    if page.scroll[1] + height >= total - 2:
        return f"экран {screens} из {screens} (низ страницы)"
    return f"экран {current} из {screens}"


def dialog_text(dialog: dict[str, Any]) -> str:
    kinds = {
        "alert": "сообщение",
        "confirm": "подтверждение",
        "prompt": "вопрос с полем ввода",
        "beforeunload": "«Покинуть страницу?» (несохранённое пропадёт)",
    }
    kind = kinds.get(str(dialog.get("type") or ""), "окно")
    message = one_line(redact(str(dialog.get("message") or "")), 300)
    keys = "enter — OK" if dialog.get("type") == "alert" else "enter — OK (согласиться), escape — Отмена"
    return (
        f"Поверх страницы окно сайта ({kind}): «{message}». Пока оно открыто, страница не отвечает: "
        f"browser_press {keys}."
    )


def render(page: Page, tabs: list[dict[str, Any]], notes: list[str] | None = None) -> str:
    """Снимок страницы для модели: заголовок, вкладки, что на экране — полностью, выше и ниже — элементы."""
    index = next((i for i, t in enumerate(tabs, 1) if t.get("targetId") == page.target), 0)
    head = [
        (f"Вкладка {index} из {len(tabs)}" if index else "Вкладка")
        + f": «{one_line(page.title, 120) or 'без названия'}» — {one_line(readable(page.url), 200)}"
    ]
    if len(tabs) > 1:
        names = [
            f"{i} «{one_line(str(t.get('title') or t.get('url') or ''), 40)}»"
            + (" (эта)" if i == index else "")
            for i, t in enumerate(tabs, 1)
        ]
        head.append("Вкладки: " + " · ".join(names))
    if page.dialog is not None:
        head.append(dialog_text(page.dialog))
        head.extend(notes or [])
        return "\n".join(head)
    state = _position(page)
    if page.ready not in ("complete", ""):
        state += "; страница ещё загружается"
    head.append(state.capitalize())
    above, screen, below = [], [], []
    for line in page.lines:
        (above, screen, below)[page.where(line) + 1].append(line)
    out = list(head)
    out.extend(_outside(page, above, "Выше экрана", ABOVE_BUDGET, nearest_last=True))
    out.append(
        "На экране:" if screen else "На экране текста и элементов нет (картинка, видео или пустая страница)."
    )
    used, skipped = 0, 0
    for line in screen:
        text = line.text
        if used + len(text) > SCREEN_BUDGET:
            if not line.elements:
                skipped += len(text)
                continue
            text = " ".join(f"[{page.elements[e].token()}]" for e in line.elements if e in page.elements)
        out.append(text)
        used += len(text)
    if skipped:
        out.append(f"(ещё ≈{skipped} знаков текста на экране пропущено — прочитай browser_read)")
    out.extend(_outside(page, below, "Ниже экрана", BELOW_BUDGET, nearest_last=False))
    out.extend(page.notes)
    out.extend(notes or [])
    return "\n".join(out)


def _outside(page: Page, lines: list[Line], label: str, budget: int, *, nearest_last: bool) -> list[str]:
    ids = [e for line in lines for e in line.elements if e in page.elements]
    chars = sum(len(line.text) for line in lines)
    if not ids and chars < 40:
        return []
    order = list(reversed(ids)) if nearest_last else ids
    shown: list[str] = []
    used = 0
    for element_id in order:
        token = page.elements[element_id].token(short=True)
        if used + len(token) > budget:
            break
        shown.append(element_id)
        used += len(token) + 2
    if nearest_last:
        shown.reverse()
    hidden = len(ids) - len(shown)
    text = f"{label} — элементов: {len(ids)}, текста ≈{chars} знаков"
    if shown:
        text += ": " + ", ".join(page.elements[e].token(short=True) for e in shown)
    if hidden:
        text += f"; ещё {hidden} не показано — browser_scroll или browser_read"
    elif chars:
        text += " — прочитать: browser_read"
    return [text]


def parts(page: Page, size: int = PART_SIZE) -> list[str]:
    """Вся страница (текст и элементы с id) — частями не больше size знаков, по строкам."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for line in page.lines:
        text = redact(line.text)
        while len(text) > size:  # очень длинная строка — режем
            if current:
                chunks.append("\n".join(current))
                current, used = [], 0
            chunks.append(text[:size])
            text = text[size:]
        if used + len(text) > size and current:
            chunks.append("\n".join(current))
            current, used = [], 0
        current.append(text)
        used += len(text) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or ["(на странице нет текста)"]


def screen_rect(page: Page, element: Element) -> tuple[int, int, int, int] | None:
    """Прямоугольник элемента на экране (для меток на картинке и курсора агента); не на экране — None."""
    if element.rect is None or page.origin is None:
        return None
    x, y, w, h = element.rect
    if w < 2 or h < 2 or y + h <= 0 or x + w <= 0 or y >= page.viewport[1] or x >= page.viewport[0] + 20:
        return None
    left, top = page.to_screen(x, y) or (0, 0)
    right, bottom = page.to_screen(x + w, y + h) or (0, 0)
    return left, top, right, bottom


def place(page: Page, window_rect: tuple[int, int, int, int]) -> None:
    """Где окно страницы на экране: внизу окна браузера (сверху — вкладки и адресная строка), по центру."""
    left, top, right, bottom = window_rect
    inner_w, inner_h = page.inner
    if right <= left or bottom <= top or inner_w <= 0 or inner_h <= 0:
        page.origin = None
        return
    width, height = inner_w * page.dpr, inner_h * page.dpr
    side = max(0.0, (right - left - width) / 2)
    page.origin = (left + side, max(float(top), bottom - side - height))
