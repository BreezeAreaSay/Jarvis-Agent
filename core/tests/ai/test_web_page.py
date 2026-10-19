"""Окно браузера для агента: кнопки самого браузера, потом элементы видимой части страницы и её текст
(jarvis.ai.uia.split_page — быстрый снимок одним запросом UI Automation)."""

from typing import Any

from jarvis.ai.uia import WEB_UI_LIMIT, UiElement, UiSnapshot, format_snapshot, page_text, split_page

FRAME = (0, 0, 1280, 900)


def _e(name: str, kind: str, rect: tuple[int, int, int, int]) -> tuple[Any, UiElement]:
    return object(), UiElement("", name, kind, rect)


def _youtube() -> list[tuple[Any, UiElement]]:
    return [
        _e("Котики — YouTube", "TabItem", (50, 5, 250, 35)),
        _e("Новая вкладка", "Button", (260, 5, 290, 35)),
        _e("Назад", "Button", (5, 40, 35, 70)),
        _e("Адресная строка и строка поиска", "Edit", (100, 40, 900, 70)),
        _e("Котики — YouTube", "Text", (60, 10, 240, 30)),  # заголовок вкладки текстом — не нужен
        _e("Котики — YouTube", "Document", (0, 80, 1280, 860)),
        _e("Введите запрос", "Edit", (300, 90, 800, 120)),
        _e("Смешные котики 2026", "Hyperlink", (100, 200, 500, 230)),
        _e("Смешные котики 2026", "Text", (110, 205, 400, 225)),  # подпись ссылки — уже в элементах
        _e("1 млн просмотров", "Text", (100, 235, 300, 250)),
        _e("1 млн просмотров", "Text", (100, 251, 300, 266)),  # повтор подряд
        _e("Реклама", "Text", (100, 300, 200, 320)),
        _e("Рамка", "Document", (600, 300, 900, 500)),  # вложенный документ (iframe) — не элемент
        _e("Ещё видео", "Button", (100, 850, 300, 880)),  # видна краем у нижней границы — тоже страница
        _e("Загрузки", "Button", (0, 862, 100, 890)),  # полоса загрузок браузера под страницей
    ]


def test_browser_window_splits_into_browser_page_and_text() -> None:
    chosen, cut, title, text = split_page(_youtube(), FRAME)
    names = [(e.kind, e.name, e.page) for _, e in chosen]
    assert names == [
        ("TabItem", "Котики — YouTube", False),
        ("Button", "Новая вкладка", False),
        ("Button", "Назад", False),
        ("Edit", "Адресная строка и строка поиска", False),
        ("Button", "Загрузки", False),
        ("Document", "Котики — YouTube", True),
        ("Edit", "Введите запрос", True),
        ("Hyperlink", "Смешные котики 2026", True),
        ("Button", "Ещё видео", True),
    ]
    assert not cut and title == "Котики — YouTube" and text == "1 млн просмотров · Реклама"

    for number, (_, element) in enumerate(chosen, start=1):
        element.id = f"e{number}"
    snap = UiSnapshot("Котики — YouTube — Google Chrome", "chrome.exe", [e for _, e in chosen], page=title)
    snap.page_text = text
    shown = format_snapshot(snap)
    assert shown.splitlines()[1] == "Браузер (вкладки, адресная строка, кнопки):"
    assert "Страница «Котики — YouTube» — видимая часть (дальше — scroll и новый снимок):" in shown
    assert shown.index("Страница «") < shown.index("e8 Hyperlink «Смешные котики 2026»")
    assert shown.endswith("Текст страницы на экране: 1 млн просмотров · Реклама")


def test_page_gets_the_room_and_browser_buttons_are_capped() -> None:
    found = [_e(f"Кнопка {i}", "Button", (i, 5, i + 20, 35)) for i in range(60)]
    found.append(_e("Магазин", "Document", (0, 80, 1280, 860)))
    found += [_e(f"Товар {i}", "Hyperlink", (10, 100 + i, 300, 101 + i)) for i in range(400)]
    chosen, cut, _, _ = split_page(found, FRAME, limit=150)
    ui = [e for _, e in chosen if not e.page]
    assert len(ui) == WEB_UI_LIMIT and len(chosen) == 150 and cut
    assert chosen[WEB_UI_LIMIT][1].kind == "Document"


def test_no_page_document_means_plain_window() -> None:
    found = [_e("Настройки", "Button", (5, 5, 50, 30)), _e("Версия 2.3", "Text", (5, 40, 90, 60))]
    chosen, cut, title, text = split_page(found, FRAME)
    assert [e.name for _, e in chosen] == ["Настройки"] and not cut and title == "" and text == ""
    assert not any(e.page for _, e in chosen)
    # маленький документ (поле редактора) — не страница
    found.append(_e("Поле", "Document", (10, 100, 200, 140)))
    assert split_page(found, FRAME)[2] == ""


def test_page_text_is_capped() -> None:
    texts = [UiElement("", f"Абзац номер {i} " * 5, "Text", (0, i * 10, 900, i * 10 + 9)) for i in range(200)]
    text = page_text(texts, [], limit=300)
    assert len(text) <= 300 and text.startswith("Абзац номер 0") and text.endswith("…")


# --- WindowsUi на поддельном UI Automation: запрос, повтор, нажатие по элементу страницы ------------------

CONTROL_TYPES = {
    "ButtonControl": 50000, "SplitButtonControl": 50031, "EditControl": 50004, "HyperlinkControl": 50005,
    "MenuItemControl": 50011, "ListItemControl": 50007, "TabItemControl": 50019, "CheckBoxControl": 50002,
    "RadioButtonControl": 50013, "ComboBoxControl": 50003, "TreeItemControl": 50024, "DocumentControl": 50030,
    "DataItemControl": 50029, "SliderControl": 50015, "SpinnerControl": 50016, "TextControl": 50020,
}  # fmt: skip
PROPERTIES = {
    "NameProperty": 30005, "ControlTypeProperty": 30003, "BoundingRectangleProperty": 30001,
    "IsEnabledProperty": 30010, "HasKeyboardFocusProperty": 30008, "IsPasswordProperty": 30019,
    "ValueValueProperty": 30045, "IsOffscreenProperty": 30022,
}  # fmt: skip


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Node:
    """IUIAutomationElement со свойствами из кэша запроса."""

    def __init__(self, name: str, kind: str, rect: tuple[int, int, int, int], value: str = "") -> None:
        self.CachedName, self.CachedControlType = name, CONTROL_TYPES[f"{kind}Control"]
        self.CachedBoundingRectangle = _Rect(*rect)
        self.CachedIsEnabled, self.CachedHasKeyboardFocus, self.CachedIsPassword = True, False, False
        self.value = value

    def GetCachedPropertyValue(self, prop: int) -> Any:
        return self.value if prop == PROPERTIES["ValueValueProperty"] else None


class _Array:
    def __init__(self, nodes: list[_Node]) -> None:
        self.nodes, self.Length = nodes, len(nodes)

    def GetElement(self, i: int) -> _Node:
        return self.nodes[i]


class _Window:
    """Окно браузера: первый запрос — страница ещё пустая (Chrome строит дерево), второй — с содержимым."""

    def __init__(self, answers: list[list[_Node]]) -> None:
        self.answers, self.asked = answers, []

    def FindAllBuildCache(self, scope: int, condition: Any, cache: Any) -> _Array:
        self.asked.append((scope, condition, sorted(cache.props)))
        return _Array(self.answers[min(len(self.asked), len(self.answers)) - 1])


class _Cache:
    def __init__(self) -> None:
        self.props: list[int] = []

    def AddProperty(self, prop: int) -> None:
        self.props.append(prop)


class _Uia:
    def CreatePropertyCondition(self, prop: int, value: Any) -> Any:
        return ("prop", prop, value)

    def CreateOrCondition(self, a: Any, b: Any) -> Any:
        return ("or", a, b)

    def CreateAndCondition(self, a: Any, b: Any) -> Any:
        return ("and", a, b)

    def CreateCacheRequest(self) -> _Cache:
        return _Cache()


class _Invoke:
    def __init__(self, log: list[str], name: str) -> None:
        self.log, self.name = log, name

    def Invoke(self) -> None:
        self.log.append(self.name)


class _Control:
    """uiautomation.Control поверх элемента — для нажатия по шаблону Invoke."""

    def __init__(self, node: _Node, log: list[str]) -> None:
        self.node, self.log, self.FrameworkId = node, log, "Chrome"

    def GetPattern(self, pattern: int) -> Any:
        return _Invoke(self.log, self.node.CachedName) if pattern == 10000 else None


def _fake_uiautomation(window: _Window, invoked: list[str]) -> Any:
    import types

    top = types.SimpleNamespace(
        Name="Котики — YouTube — Google Chrome",
        ProcessId=4242,
        BoundingRectangle=_Rect(*FRAME),
        Element=window,
        NativeWindowHandle=777,
    )
    client = types.SimpleNamespace(IUIAutomation=_Uia())
    core = types.SimpleNamespace(_AutomationClient=types.SimpleNamespace(instance=lambda: client))
    auto = types.ModuleType("uiautomation")
    auto.uiautomation = core  # type: ignore[attr-defined]
    auto.SetGlobalSearchTimeout = lambda seconds: None  # type: ignore[attr-defined]
    auto.ControlFromHandle = lambda hwnd: top  # type: ignore[attr-defined]
    auto.ControlType = types.SimpleNamespace(**CONTROL_TYPES)  # type: ignore[attr-defined]
    auto.PropertyId = types.SimpleNamespace(**PROPERTIES)  # type: ignore[attr-defined]
    auto.PatternId = types.SimpleNamespace(  # type: ignore[attr-defined]
        InvokePattern=10000, SelectionItemPattern=10010, TogglePattern=10015, ExpandCollapsePattern=10005
    )
    auto.Control = types.SimpleNamespace(  # type: ignore[attr-defined]
        CreateControlFromElement=lambda node: _Control(node, invoked)
    )
    return auto


def test_windows_browser_snapshot_in_one_request_and_click(monkeypatch: Any) -> None:
    import sys
    import types

    from jarvis.ai import uia

    empty = [
        _Node("Котики — YouTube", "TabItem", (50, 5, 250, 35)),
        _Node("", "Document", (0, 80, 1280, 860)),
    ]
    full = [
        _Node("Котики — YouTube", "TabItem", (50, 5, 250, 35)),
        _Node("Адресная строка и строка поиска", "Edit", (100, 40, 900, 70), value="youtube.com/results"),
        _Node("Котики — YouTube", "Document", (0, 80, 1280, 860)),
        _Node("Смешные котики 2026", "Hyperlink", (100, 200, 500, 230)),
        _Node("1 млн просмотров", "Text", (100, 235, 300, 250)),
    ]
    window, invoked, woken = _Window([empty, full]), [], []
    auto = _fake_uiautomation(window, invoked)
    monkeypatch.setitem(sys.modules, "uiautomation", auto)
    monkeypatch.setitem(sys.modules, "uiautomation.uiautomation", auto.uiautomation)
    monkeypatch.setattr("psutil.Process", lambda pid: types.SimpleNamespace(name=lambda: "chrome.exe"))
    monkeypatch.setattr(uia, "_WEB_RETRY_S", 0.0)
    monkeypatch.setattr(
        uia.WindowsUi, "_wake", staticmethod(lambda top: woken.append(top.NativeWindowHandle))
    )

    ui = uia.WindowsUi(object())  # type: ignore[arg-type]
    snap = ui.snapshot(hwnd=777)
    # одним запросом (FindAllBuildCache по всему окну), пустая страница — разбудить Chrome и спросить ещё раз
    assert len(window.asked) == 2 and woken == [777]
    scope, condition, props = window.asked[0]
    assert scope == 4 and condition[0] == "and" and condition[2] == ("prop", 30022, False)  # только видимые
    assert props == sorted(PROPERTIES[p] for p in PROPERTIES if p != "IsOffscreenProperty")
    assert [(e.id, e.kind, e.page) for e in snap.elements] == [
        ("e1", "TabItem", False),
        ("e2", "Edit", False),
        ("e3", "Document", True),
        ("e4", "Hyperlink", True),
    ]
    assert (
        snap.app == "chrome.exe" and snap.page == "Котики — YouTube" and snap.page_text == "1 млн просмотров"
    )
    assert snap.elements[1].value == "youtube.com/results"
    # нажатие по ссылке страницы: элемент из быстрого снимка обёрнут для шаблонов UI Automation
    assert ui.click("e4") == "нажато" and invoked == ["Смешные котики 2026"]
