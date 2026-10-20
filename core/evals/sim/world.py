"""Мир имитатора: экран 1920×1080, окна программ-сцен, уведомления, файлы, звук и связь — и журнал всего,
что в нём случилось (ввод, нажатия, действия программ).

Программа-сцена (App) хранит своё состояние и по нему каждый раз заново строит виджеты окна (view): что
показать, что видно UI Automation, что делает щелчок. Текст полей лежит в окне (Window.values) по ключу
виджета, фокус ввода — ключ виджета (Window.focus). Ввод приходит от SimPlatform (клавиши, мышь — как
настоящие) и от SimUi (шаблоны UI Automation — без мыши, в фоне).

Журнал (World.log) — основа проверок: каждая запись знает номер действия агента (World.step — его ставит
прогон по событию agent.step), окно и программу. Рискованные действия программ (отправить, удалить,
переключить связь) помечены risky: прогон сверяет их с разрешениями человека.

Время мира — World.now(): секунды с начала, умноженные на speed (детерминированный режим ускоряет
«медленные» окна, чтобы тесты не ждали). Таймеры (after) и крючки событий (on/emit) — для задач: окно
всплывает посреди дела, папка долго грузится.

Мир трогают из потока платформы и из пула ввода-вывода (картинка экрана снимается параллельно) — всё под
одной блокировкой World.lock.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis.platform.base import AppInfo, WindowInfo

Rect = tuple[int, int, int, int]
SCREEN: Rect = (0, 0, 1920, 1080)
TASKBAR = 48  # панель задач снизу
TITLE_BAR = 32  # заголовок окна
HOME = "C:\\Users\\me"
FOLDERS = {  # папки пользователя: путь → как их зовёт Проводник
    HOME + "\\Desktop": "Рабочий стол",
    HOME + "\\Downloads": "Загрузки",
    HOME + "\\Documents": "Документы",
    HOME + "\\Pictures": "Изображения",
    HOME + "\\Music": "Музыка",
}
EDITABLE = frozenset({"Edit", "Document"})
LIST_KINDS = frozenset({"ListItem", "DataItem", "TreeItem"})


def norm(text: str) -> str:
    """Для сравнения: без регистра, «ё» как «е», лишних пробелов, кавычек и точки в конце."""
    text = text.casefold().replace("ё", "е")
    for quote in "«»\"'“”„":
        text = text.replace(quote, "")
    return " ".join(text.split()).strip(" .!")


@dataclass
class Widget:
    """Элемент окна: что нарисовано, что видно UI Automation и что делает щелчок."""

    key: str  # стабильный ключ в окне («search», «chat:Мама»)
    kind: (
        str  # тип для UI Automation: Button, Edit, Document, ListItem, Text, MenuItem, TreeItem…; Pane — фон
    )
    name: str = ""  # имя для UI Automation (и подпись на картинке, если label не задан)
    rect: Rect = (0, 0, 0, 0)  # в окне: от его левого верхнего угла (вместе с заголовком)
    value: str = ""  # текст поля, второй строкой у пункта списка
    uia: bool = True  # виден UI Automation
    enabled: bool = True
    password: bool = False
    placeholder: str = ""
    label: str | None = None  # что нарисовано, если не name ("" — только значок)
    icon: str = ""  # значок: play, pause, next, prev, search, menu, send, trash, folder, file, avatar…
    size: int = 15  # кегль подписи
    style: str = ""  # primary, danger, selected, bold, header, muted, card, bubble-in, bubble-out, on, off…
    color: tuple[int, int, int] | None = None
    multiline: bool = False  # Edit в несколько строк (поле сообщения): перевод строки в тексте не теряется
    on_click: Callable[[], Any] | None = None
    on_double: Callable[[], Any] | None = None
    on_enter: Callable[[], Any] | None = None  # Enter, когда фокус в этом поле

    @property
    def editable(self) -> bool:
        return self.kind in EDITABLE and self.enabled

    @property
    def text(self) -> str:
        return self.name if self.label is None else self.label


@dataclass(eq=False)
class Window:
    app: "App"
    hwnd: int
    title: str
    rect: Rect
    kind: str = "main"  # main | dialog | popup
    values: dict[str, str] = field(default_factory=dict)  # текст полей по ключу виджета
    focus: str = ""  # ключ виджета с фокусом ввода
    selected_all: bool = False  # ctrl+a или поле только что выбрано: следующий ввод заменит текст
    minimized: bool = False
    data: dict[str, Any] = field(default_factory=dict)  # что ещё нужно сцене (открытый чат, папка…)

    @property
    def exe(self) -> str:
        return str(self.data.get("exe") or self.app.exe)  # просмотр файлов: у PDF — Edge, у .docx — Word

    @property
    def pid(self) -> int:
        return self.app.pid

    def widgets(self) -> list[Widget]:
        """Кнопки заголовка («Свернуть», «Развернуть», «Закрыть» — их видит и UI Automation) и виджеты
        сцены."""
        width, world = self.size[0], self.app.world
        chrome = [
            Widget(
                "title:min",
                "Button",
                "Свернуть",
                (width - 150, 0, width - 106, TITLE_BAR),
                style="chrome",
                on_click=lambda: world.minimize(self),
            ),
            Widget(
                "title:max", "Button", "Развернуть", (width - 106, 0, width - 62, TITLE_BAR), style="chrome"
            ),
            Widget(
                "title:close",
                "Button",
                "Закрыть",
                (width - 62, 0, width - 18, TITLE_BAR),
                style="chrome",
                on_click=lambda: world.close(self),
            ),
        ]
        return chrome + self.app.view(self)

    def widget(self, key: str) -> Widget | None:
        return next((w for w in self.widgets() if w.key == key), None)

    def screen_rect(self, rect: Rect) -> Rect:
        """Прямоугольник в окне → на экране."""
        x, y = self.rect[0], self.rect[1]
        return rect[0] + x, rect[1] + y, rect[2] + x, rect[3] + y

    @property
    def size(self) -> tuple[int, int]:
        return self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]

    def info(self, foreground: bool = False) -> WindowInfo:
        return WindowInfo(
            self.hwnd, self.title, self.exe, self.pid, self.rect, self.minimized, False, foreground
        )


class App:
    """Программа-сцена. Наследники задают имя в «Пуске», exe, заголовок и view."""

    name = "Программа"  # как в меню «Пуск»
    exe = "app.exe"
    title = "Программа"
    rect: Rect = (160, 60, 1560, 960)  # где открывается главное окно
    background: tuple[int, int, int] = (255, 255, 255)
    launch_delay = 0.0  # секунд мира от запуска до окна
    single = True  # второй запуск выводит вперёд уже открытое окно

    def __init__(self, world: "World") -> None:
        self.world = world
        self.pid = world.new_pid()

    @property
    def path(self) -> str:
        return f"C:\\Program Files\\{self.name}\\{self.exe}"

    def info(self) -> AppInfo:
        return AppInfo(self.name, self.path, self.path)

    def windows(self) -> list[Window]:
        return [w for w in self.world.windows if w.app is self]

    def main(self) -> Window | None:
        return next((w for w in self.windows() if w.kind == "main"), None)

    def open_main(self, **data: Any) -> Window:
        return self.world.open(self, self.title, self.rect, data=data)

    def launch(self) -> None:
        window = self.main()
        if window is not None and self.single:
            self.world.front(window)
            return
        if self.launch_delay > 0:
            self.world.after(self.launch_delay, self.open_main)
        else:
            self.open_main()

    def view(self, window: Window) -> list[Widget]:
        return []

    def key(self, window: Window, combo: str) -> bool:
        """Клавиша в окне программы; False — программа её не знает (обработает мир: tab, ctrl+a…)."""
        return False

    def changed(self, window: Window, key: str) -> None:
        """Текст поля key изменился (набрали, вставили, задали через UI Automation)."""

    def url(self, url: str) -> bool:
        """Адрес для этой программы (tg:, ms-settings:…); False — не её."""
        return False

    def close(self, window: Window) -> bool:
        """Окно просят закрыть (крестик, alt+f4); False — программа показала свой вопрос и не закрылась."""
        return True


@dataclass
class Toast:
    id: int
    app: str
    title: str
    body: str = ""
    created: float = 0.0
    until: float = 0.0  # до какого времени мира видна на экране (в центре уведомлений — всегда)
    on_click: Callable[[], Any] | None = None


@dataclass
class Event:
    """Что случилось в мире."""

    seq: int
    t: float  # время мира
    step: int  # номер действия агента (0 — до первого действия или не агент)
    kind: str  # type (ввод текста), key, click, effect (действие программы), launch, url, open, window
    text: str = ""
    exe: str = ""
    window: str = ""
    risky: bool = False  # рискованное действие программы: нужно разрешение человека
    data: dict[str, Any] = field(default_factory=dict)


class World:
    def __init__(self, *, speed: float = 1.0) -> None:
        self.lock = threading.RLock()
        self.speed = speed
        self._t0 = time.monotonic()
        self.windows: list[Window] = []  # снизу вверх: последнее — впереди
        self.apps: list[App] = []
        self.toasts: list[Toast] = []
        self.files: dict[str, str] = {}  # путь → текст
        self.folders: set[str] = set(FOLDERS)
        self.opened: list[str] = []  # открытые файлы
        self.volume = 40
        self.muted = False
        self.bluetooth = False
        self.wifi = True
        self.cursor = (960, 540)
        self.log: list[Event] = []
        self.seq = 0
        self.step = 0
        self.version = 0  # растёт при каждом изменении — картинка экрана перерисовывается
        self._timers: list[tuple[float, Callable[[], Any]]] = []
        self._hooks: dict[str, list[Callable[..., Any]]] = {}
        self._hwnd = 0x10000
        self._pid = 4000
        self._toast_id = 0

    # --- время и события ---------------------------------------------------------------------------
    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    def after(self, seconds: float, fn: Callable[[], Any]) -> None:
        """Через seconds секунд мира — fn (при ближайшем обращении к миру)."""
        with self.lock:
            self._timers.append((self.now() + seconds, fn))

    def on(self, name: str, fn: Callable[..., Any]) -> None:
        self._hooks.setdefault(name, []).append(fn)

    def emit(self, name: str, *args: Any) -> None:
        for fn in list(self._hooks.get(name, [])):
            fn(*args)

    def tick(self) -> None:
        with self.lock:
            now = self.now()
            due = [t for t in self._timers if t[0] <= now]
            if not due:
                return
            self._timers = [t for t in self._timers if t[0] > now]
            for _, fn in sorted(due, key=lambda t: t[0]):
                fn()
            self.touch()

    def touch(self) -> None:
        self.version += 1

    def set_step(self, step: int) -> None:
        """Агент начал действие номер step (прогон — по событию agent.step); задачам — событие «step»."""
        with self.lock:
            self.step = step
            self.emit("step", step)

    def record(self, kind: str, what: str = "", *, window: Window | None = None, **data: Any) -> Event:
        with self.lock:
            self.seq += 1
            risky = bool(data.pop("risky", False))
            app = data.pop("app", None)
            exe = window.exe if window is not None else (app.exe if isinstance(app, App) else str(app or ""))
            event = Event(
                self.seq,
                round(self.now(), 3),
                self.step,
                kind,
                what,
                exe,
                window.title if window is not None else "",
                risky,
                data,
            )
            self.log.append(event)
            self.touch()
            self.emit("record", event)  # задачам: «после первого ввода в Блокнот — вперёд Telegram»
            return event

    def effects(self, action: str | None = None) -> list[Event]:
        return [
            e for e in self.log if e.kind == "effect" and (action is None or e.data.get("action") == action)
        ]

    def new_pid(self) -> int:
        self._pid += 4
        return self._pid

    # --- программы и окна ---------------------------------------------------------------------------
    def install(self, app: App) -> App:
        self.apps.append(app)
        return app

    def app(self, kind: type) -> Any:
        """Программа по классу (первая такая)."""
        return next((a for a in self.apps if isinstance(a, kind)), None)

    def open(
        self,
        app: App,
        title: str,
        rect: Rect,
        *,
        kind: str = "main",
        front: bool = True,
        data: dict[str, Any] | None = None,
        focus: str = "",
    ) -> Window:
        with self.lock:
            self._hwnd += 2
            window = Window(app, self._hwnd, title, rect, kind, data=dict(data or {}), focus=focus)
            if front:
                self.windows.append(window)
            else:
                self.windows.insert(max(0, len(self.windows) - 1), window)
            self.record("window", f"открылось «{title}»", window=window)
            return window

    def close(self, window: Window, *, force: bool = False) -> bool:
        with self.lock:
            if window not in self.windows:
                return True
            if not force and not window.app.close(window):
                return False
            self.windows.remove(window)
            self.record("window", f"закрылось «{window.title}»", window=window)
            return True

    def front(self, window: Window) -> None:
        """Окно — вперёд; у него открыт диалог — вперёд и диалог (модальный); «Пуск» закрывается, как в
        жизни."""
        with self.lock:
            if window not in self.windows:
                return
            for start in [w for w in self.windows if w.data.get("start") and w is not window]:
                self.windows.remove(start)
            self.windows.remove(window)
            self.windows.append(window)
            window.minimized = False
            dialog = self.modal(window)
            if dialog is not None:
                self.windows.remove(dialog)
                self.windows.append(dialog)
            self.touch()

    def minimize(self, window: Window) -> None:
        with self.lock:
            window.minimized = True
            self.record("window", f"свёрнуто «{window.title}»", window=window)

    def modal(self, window: Window) -> Window | None:
        """Открытый диалог этого окна («Сохранение», «Сохранить изменения?»): пока он есть, окно не нажать."""
        return next((w for w in self.windows if w.kind == "dialog" and w.data.get("owner") is window), None)

    @property
    def foreground(self) -> Window | None:
        with self.lock:
            return next((w for w in reversed(self.windows) if not w.minimized), None)

    def window(self, hwnd: int | None) -> Window | None:
        with self.lock:
            return next((w for w in self.windows if w.hwnd == hwnd), None)

    def find_window(self, target: int | str | None) -> Window | None:
        """Окно по hwnd, заголовку или exe (для window_action)."""
        if target is None:
            return self.foreground
        if isinstance(target, int):
            return self.window(target)
        want = norm(str(target)).removesuffix(".exe")
        for window in reversed(self.windows):
            if want and (want in norm(window.title) or want == norm(window.exe).removesuffix(".exe")):
                return window
        return None

    def window_at(self, x: int, y: int) -> Window | None:
        with self.lock:
            for window in reversed(self.windows):
                left, top, right, bottom = window.rect
                if not window.minimized and left <= x < right and top <= y < bottom:
                    return window
            return None

    def widget_at(self, window: Window, x: int, y: int) -> Widget | None:
        """Элемент окна под точкой экрана: верхний из тех, что отзываются на щелчок (надпись внутри строки
        списка — щелчок по строке), иначе просто верхний; фон (Pane) не в счёт."""
        hits = [
            w
            for w in reversed(window.widgets())
            if w.kind != "Pane" and _inside(window.screen_rect(w.rect), x, y)
        ]
        active = [w for w in hits if w.on_click or w.on_double or w.editable]
        return (active or hits or [None])[0]

    def toast_at(self, x: int, y: int) -> Toast | None:
        for toast, rect in self.visible_toasts():
            if rect[0] <= x < rect[2] and rect[1] <= y < rect[3]:
                return toast
        return None

    def visible_toasts(self) -> list[tuple[Toast, Rect]]:
        """Уведомления на экране (справа внизу, новые — снизу) и где они."""
        now = self.now()
        shown = [t for t in self.toasts if t.until > now][-3:]
        out: list[tuple[Toast, Rect]] = []
        bottom = SCREEN[3] - TASKBAR - 12
        for toast in reversed(shown):
            rect = (SCREEN[2] - 392, bottom - 104, SCREEN[2] - 12, bottom)
            out.append((toast, rect))
            bottom -= 116
        return out

    def taskbar_buttons(self) -> list[tuple[Window, Rect]]:
        mains = [w for w in self.windows if w.kind == "main"]
        start = SCREEN[2] // 2 - (len(mains) + 1) * 26
        out = []
        for i, window in enumerate(mains, start=1):
            left = start + i * 52
            out.append((window, (left, SCREEN[3] - TASKBAR + 4, left + 44, SCREEN[3] - 4)))
        return out

    def toast(
        self,
        app: str,
        title: str,
        body: str = "",
        *,
        on_click: Callable[[], Any] | None = None,
        show_s: float = 8.0,
    ) -> Toast:
        with self.lock:
            self._toast_id += 1
            toast = Toast(self._toast_id, app, title, body, self.now(), self.now() + show_s, on_click)
            self.toasts.append(toast)
            self.record("toast", f"{app}: {title} — {body}", app=app)
            return toast

    # --- ввод ----------------------------------------------------------------------------------------
    def focused(self, window: Window) -> Widget | None:
        widget = window.widget(window.focus) if window.focus else None
        return widget if widget is not None and widget.editable else None

    def type_text(self, text: str, how: str = "type") -> None:
        """Текст — в поле с фокусом окна впереди (как настоящая клавиатура)."""
        with self.lock:
            window = self.foreground
            if window is None:
                self.record("type", text, how=how, field="", lost=True)
                return
            widget = self.focused(window)
            if widget is None:
                self.record("type", text, window=window, how=how, field="", lost=True)
                return
            if widget.kind == "Edit" and not widget.multiline:
                text = text.replace("\r", "").replace("\n", " ")
            old = "" if window.selected_all else window.values.get(widget.key, widget.value)
            caret = f"caret:{widget.key}"
            if widget.kind == "Document" and caret in window.data and not window.selected_all:
                at = max(
                    0, min(int(window.data[caret]), len(old))
                )  # курсор внутри текста (у открытого файла — 0)
                self._set_value(window, widget, old[:at] + text + old[at:])
                window.data[caret] = at + len(text)
            else:
                window.data.pop(caret, None)  # курсор — в конце
                self._set_value(window, widget, old + text)
            self.record("type", text, window=window, how=how, field=widget.name or widget.key)

    def set_text(self, window: Window, widget: Widget, text: str) -> None:
        """UI Automation: ValuePattern.SetValue — содержимое поля заменяется, фокус — в поле."""
        with self.lock:
            window.focus = widget.key
            window.data.pop(f"caret:{widget.key}", None)
            self._set_value(window, widget, text)
            self.record("type", text, window=window, how="uia", field=widget.name or widget.key)

    def _set_value(self, window: Window, widget: Widget, text: str) -> None:
        window.values[widget.key] = text
        window.selected_all = False
        window.app.changed(window, widget.key)
        self.touch()

    def press(self, combo: str) -> None:
        with self.lock:
            window = self.foreground
            self.record("key", combo, window=window)
            if self._global_key(combo, window) or window is None:
                return
            if window.app.key(window, combo):
                self.touch()
                return
            self._default_key(window, combo)
            self.touch()

    def _global_key(self, combo: str, window: Window | None) -> bool:
        if combo in ("win", "win+s", "ctrl+esc"):
            start = next((a for a in self.apps if getattr(a, "start", False)), None)
            if start is not None:
                start.launch()
            return True
        if combo == "win+e":
            self.launch("explorer.exe")
            return True
        if combo in ("win+d", "win+m"):
            for w in self.windows:
                w.minimized = True
            self.touch()
            return True
        if combo == "alt+tab":
            visible = [w for w in self.windows if not w.minimized]
            if len(visible) >= 2:
                self.front(visible[-2])
            return True
        if combo == "alt+f4":
            if window is not None:
                self.close(window)
            return True
        return False

    def _default_key(self, window: Window, combo: str) -> None:
        widget = self.focused(window)
        if combo in ("tab", "shift+tab"):
            fields = [w for w in window.widgets() if w.editable]
            if fields:
                keys = [w.key for w in fields]
                i = keys.index(window.focus) if window.focus in keys else -1
                window.focus = keys[(i + (1 if combo == "tab" else -1)) % len(keys)]
                window.selected_all = True
            return
        if combo == "ctrl+a" and widget is not None:
            window.selected_all = True
            return
        caret = f"caret:{widget.key}" if widget is not None else ""
        if combo in ("ctrl+end", "ctrl+home") and widget is not None and widget.kind == "Document":
            if combo == "ctrl+home":
                window.data[caret] = 0
            else:
                window.data.pop(caret, None)
            window.selected_all = False
            return
        if combo in ("backspace", "delete") and widget is not None:
            value = window.values.get(widget.key, widget.value)
            at = int(window.data.get(caret, len(value)))
            if window.selected_all:
                self._set_value(window, widget, "")
            elif at > 0:
                self._set_value(window, widget, value[: at - 1] + value[at:])
                if caret in window.data:
                    window.data[caret] = at - 1
            return
        if combo == "enter":
            if widget is not None and widget.on_enter is not None:
                widget.on_enter()
            elif widget is not None and widget.kind == "Document":
                self._set_value(window, widget, window.values.get(widget.key, widget.value) + "\n")
            return
        if combo == "esc" and window.kind in ("dialog", "popup"):
            self.close(window)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        """Настоящий щелчок мышью: уведомление, панель задач или окно под точкой (оно выходит вперёд)."""
        with self.lock:
            self.cursor = (x, y)
            toast = self.toast_at(x, y)
            if toast is not None:
                toast.until = 0.0
                self.record("click", f"уведомление «{toast.title}»", app=toast.app)
                if toast.on_click is not None:
                    toast.on_click()
                self.touch()
                return
            if y >= SCREEN[3] - TASKBAR:
                for window, rect in self.taskbar_buttons():
                    if rect[0] <= x < rect[2]:
                        self.front(window)
                        self.record("click", f"панель задач: «{window.title}»", window=window)
                        return
                self.record("click", "панель задач")
                return
            window = self.window_at(x, y)
            if window is None:
                self.record("click", f"рабочий стол ({x}, {y})")
                return
            dialog = self.modal(window)
            if dialog is not None:  # окно с открытым диалогом не нажать: диалог мигает впереди
                self.front(dialog)
                self.record("click", f"мимо диалога «{dialog.title}»", window=window)
                return
            if window is not self.foreground:
                self.front(window)
            if button != "left":
                self.record("click", f"{button} ({x}, {y})", window=window)
                return
            widget = self.widget_at(window, x, y)
            if widget is None:
                self.record("click", f"({x}, {y})", window=window)
                return
            self.click_widget(window, widget, double=double, via="mouse")

    def click_widget(
        self, window: Window, widget: Widget, *, double: bool = False, via: str = "mouse"
    ) -> str:
        with self.lock:
            self.record(
                "click", widget.name or widget.text or widget.key, window=window, via=via, double=double
            )
            if not widget.enabled:
                return "элемент недоступен"
            if widget.editable:
                window.focus = widget.key
                window.selected_all = False
                if widget.kind == "Document":
                    window.data.pop(f"caret:{widget.key}", None)  # щелчок под текстом — курсор в конце
            elif widget.kind in LIST_KINDS:
                window.focus = widget.key  # пункт списка забирает фокус у поля: Delete и Enter — уже ему
            action = widget.on_double if double and widget.on_double is not None else widget.on_click
            if action is not None:
                action()
            self.touch()
            return "переключено" if widget.style in ("on", "off") else "нажато"

    # --- система -------------------------------------------------------------------------------------
    def find_app(self, target: str) -> App | None:
        want = norm(target).rsplit("\\", 1)[-1]
        for app in self.apps:
            if want in (norm(app.name), norm(app.exe), norm(app.path).rsplit("\\", 1)[-1]) or norm(
                target
            ) == norm(app.path):
                return app
        return None

    def launch(self, target: str) -> App | None:
        with self.lock:
            app = self.find_app(target)
            self.record("launch", target, app=app)
            if app is not None:
                app.launch()
            return app

    def open_url(self, url: str) -> None:
        with self.lock:
            self.record("url", url)
            for app in self.apps:
                if app.url(url):
                    return
            browser = next((a for a in self.apps if getattr(a, "browser", False)), None)
            if browser is not None:
                browser.url(url)

    def real_folder(self, path: str) -> str | None:
        """Папка мира с этим путём без учёта регистра (как в Windows; %USERPROFILE% — домашняя) или None."""
        want = (
            path.strip().strip('"').replace("/", "\\").replace("%USERPROFILE%", HOME).rstrip("\\").casefold()
        )
        return next((f for f in self.folders if f.casefold() == want), None)

    def real_file(self, path: str) -> str | None:
        want = path.strip().strip('"').replace("/", "\\").replace("%USERPROFILE%", HOME).casefold()
        return next((f for f in self.files if f.casefold() == want), None)

    def open_path(self, path: str) -> None:
        with self.lock:
            folder = self.real_folder(path)
            if folder is not None:  # «открой загрузки»: папка — в Проводнике, это не «открытый файл»
                self.record("open", folder, folder=True)
                for app in self.apps:
                    opener = getattr(app, "open_folder", None)
                    if callable(opener) and opener(folder):
                        return
                return
            path = self.real_file(path) or path
            self.opened.append(path)
            self.record("open", path)
            for app in self.apps:
                opener = getattr(app, "open_file", None)
                if callable(opener) and opener(path):
                    return

    def folder_items(self, folder: str) -> list[tuple[str, bool]]:
        """Что лежит в папке: (путь, папка ли) — папки первыми, по имени."""
        prefix = folder.rstrip("\\") + "\\"
        dirs = {f for f in self.folders if f.startswith(prefix) and "\\" not in f[len(prefix) :]}
        for path in self.files:
            rest = path[len(prefix) :] if path.startswith(prefix) else ""
            if "\\" in rest:
                dirs.add(prefix + rest.split("\\", 1)[0])
        files = {p for p in self.files if p.startswith(prefix) and "\\" not in p[len(prefix) :]}
        return sorted(((d, True) for d in dirs), key=lambda i: norm(i[0])) + sorted(
            ((f, False) for f in files), key=lambda i: norm(i[0])
        )

    def installed(self) -> list[AppInfo]:
        return [info for info in (app.info() for app in self.apps) if info is not None]


def _inside(rect: Rect, x: int, y: int) -> bool:
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]
