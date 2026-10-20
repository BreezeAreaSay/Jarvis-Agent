"""Проводник и просмотр открытых файлов.

Проводник: папки слева (TreeItem), адресная строка (ctrl+l, alt+d, f4), поиск по папке (ctrl+e, ctrl+f),
файлы — ListItem: щелчок выделяет, двойной щелчок или Enter открывает, Delete — в Корзину (рискованное
действие), alt+up и backspace — на уровень выше. Медленная папка (slow: путь → секунды мира) первый раз
показывает «Работаем над этим…» без файлов — как Проводник с большой или сетевой папкой.

Viewer — что открывается двойным щелчком: документ Word, PDF в Edge, картинка; текст — в Блокноте (его
открывает сам Блокнот).
"""

import os

from evals.sim.world import FOLDERS, HOME, App, Widget, Window, World, norm

HOME_TITLE = "Главная"


def folder_title(path: str) -> str:
    return FOLDERS.get(path) or (HOME_TITLE if not path else path.rsplit("\\", 1)[-1])


def file_type(name: str) -> str:
    ext = os.path.splitext(name)[1].casefold()
    return {
        ".docx": "Документ Microsoft Word",
        ".xlsx": "Лист Microsoft Excel",
        ".pdf": "Документ PDF",
        ".txt": "Текстовый документ",
        ".jpg": "Файл JPG",
        ".png": "Файл PNG",
        ".zip": "Сжатая ZIP-папка",
        ".exe": "Приложение",
    }.get(ext, "Файл")


class Explorer(App):
    name = "Проводник"
    exe = "explorer.exe"
    title = "Главная — Проводник"
    rect = (200, 100, 1500, 900)
    single = False

    def __init__(self, world: World, *, slow: dict[str, float] | None = None) -> None:
        super().__init__(world)
        self.slow = dict(slow or {})
        self.recycled: list[str] = []

    @property
    def path(self) -> str:
        return "C:\\Windows\\explorer.exe"

    def open_main(self, **data: object) -> Window:
        folder = str(data.get("folder") or "")
        window = self.world.open(
            self, f"{folder_title(folder)} — Проводник", self.rect, data={"folder": "", "history": []}
        )
        self.go(window, folder, remember=False)
        return window

    def go(self, window: Window, folder: str, *, remember: bool = True) -> None:
        old = str(window.data.get("folder") or "")
        if remember and old != folder:
            window.data.setdefault("history", []).append(old)
        window.data.update(folder=folder, selected="", query="", error="")
        window.values["search"] = ""
        window.values["address"] = folder or HOME_TITLE
        delay = self.slow.pop(folder, 0.0)
        window.data["ready_at"] = self.world.now() + delay
        if delay:
            self.world.after(delay, self.world.touch)
        window.title = f"{folder_title(folder)} — Проводник"
        window.focus = ""
        self.world.record(
            "effect", f"Проводник: «{folder or HOME_TITLE}»", window=window, action="folder", folder=folder
        )

    def loading(self, window: Window) -> bool:
        return self.world.now() < float(window.data.get("ready_at") or 0.0)

    def items(self, window: Window) -> list[tuple[str, bool]]:
        folder = str(window.data.get("folder") or "")
        if self.loading(window):
            return []
        query = norm(str(window.data.get("query") or ""))
        base = self.world.folder_items(folder) if folder else [(path, True) for path in FOLDERS]
        if not query:
            return base
        root = folder or HOME
        found = [(p, False) for p in self.world.files if p.startswith(root + "\\")]
        found += [(p, True) for p in self.world.folders if p.startswith(root + "\\")]
        return sorted((i for i in found if query in norm(i[0].rsplit("\\", 1)[-1])), key=lambda i: norm(i[0]))

    # --- действия ------------------------------------------------------------------------------------
    def open_item(self, window: Window, path: str, is_dir: bool) -> None:
        if is_dir:
            self.go(window, path)
        else:
            self.world.open_path(path)

    def select(self, window: Window, path: str) -> None:
        window.data["selected"] = path

    def recycle(self, window: Window) -> None:
        path = str(window.data.get("selected") or "")
        if not path or path not in self.world.files:
            return
        self.world.files.pop(path)
        self.recycled.append(path)
        window.data["selected"] = ""
        self.world.record(
            "effect", f"Проводник: в Корзину «{path}»", window=window, risky=True, action="delete", path=path
        )

    def navigate(self, window: Window) -> None:
        text = window.values.get("address", "").strip().strip('"').replace("%USERPROFILE%", HOME).rstrip("\\")
        titles = {norm(title): path for path, title in FOLDERS.items()}
        path = titles.get(norm(text), text)
        if norm(text) in (norm(HOME_TITLE), ""):
            path = ""
        if path and path not in self.world.folders:
            if path in self.world.files:
                self.world.open_path(path)
                return
            window.data["error"] = f"Не удаётся найти «{text}»"
            window.values["address"] = text
            return
        self.go(window, path)

    def search(self, window: Window) -> None:
        window.data["query"] = window.values.get("search", "")
        window.data["selected"] = ""
        if window.data["query"]:
            window.title = f"Результаты поиска в «{folder_title(str(window.data['folder']))}» — Проводник"

    def changed(self, window: Window, key: str) -> None:
        if key == "search":
            self.search(window)

    def key(self, window: Window, combo: str) -> bool:
        if combo in ("ctrl+l", "alt+d", "f4"):
            window.focus, window.selected_all = "address", True
            return True
        if combo in ("ctrl+e", "ctrl+f"):
            window.focus, window.selected_all = "search", True
            return True
        if window.focus == "address" and combo == "enter":
            self.navigate(window)
            return True
        if window.focus == "search" and combo == "enter":
            self.search(window)
            window.focus = ""
            return True
        editing = window.focus in ("address", "search")
        if combo == "enter" and not editing:
            selected = str(window.data.get("selected") or "")
            if selected:
                self.open_item(window, selected, selected in self.world.folders or selected in FOLDERS)
            return True
        if combo in ("alt+up", "backspace") and not editing:
            folder = str(window.data.get("folder") or "")
            self.go(window, folder.rsplit("\\", 1)[0] if folder and folder not in FOLDERS else "")
            return True
        if combo == "alt+left":
            history = window.data.get("history") or []
            if history:
                self.go(window, history.pop(), remember=False)
            return True
        if combo == "delete" and not editing:
            self.recycle(window)
            return True
        if combo in ("down", "up") and not editing:
            paths = [p for p, _ in self.items(window)]
            if paths:
                current = str(window.data.get("selected") or "")
                i = paths.index(current) if current in paths else -1
                i = max(0, min(len(paths) - 1, i + (1 if combo == "down" else -1)))
                window.data["selected"] = paths[i]
            return True
        return False

    # --- вид -------------------------------------------------------------------------------------------
    def view(self, window: Window) -> list[Widget]:
        width, height = window.size
        folder = str(window.data.get("folder") or "")
        items = [
            Widget(
                "back",
                "Button",
                "Назад",
                (10, 40, 44, 72),
                label="‹",
                style="flat",
                on_click=lambda: self.key(window, "alt+left"),
            ),
            Widget(
                "up",
                "Button",
                "Вверх",
                (48, 40, 82, 72),
                label="↑",
                style="flat",
                on_click=lambda: self.key(window, "alt+up"),
            ),
            Widget(
                "address",
                "Edit",
                "Адресная строка",
                (90, 40, width - 330, 72),
                on_enter=lambda: self.navigate(window),
            ),
            Widget(
                "search",
                "Edit",
                f"Поиск: {folder_title(folder)}",
                (width - 320, 40, width - 12, 72),
                placeholder=f"Поиск: {folder_title(folder)}",
                icon="search",
                on_enter=lambda: self.search(window),
            ),
            Widget("nav", "Pane", rect=(0, 80, 240, height), color=(249, 249, 249)),
        ]
        items.append(
            Widget(
                "nav:home",
                "TreeItem",
                HOME_TITLE,
                (8, 90, 232, 122),
                icon="gear",
                style="selected" if not folder else "",
                on_click=lambda: self.go(window, ""),
            )
        )
        for i, (path, title) in enumerate(FOLDERS.items(), start=1):
            items.append(
                Widget(
                    f"nav:{path}",
                    "TreeItem",
                    title,
                    (8, 90 + i * 36, 232, 122 + i * 36),
                    icon="folder",
                    style="selected" if path == folder else "",
                    on_click=lambda p=path: self.go(window, p),
                )
            )
        for i, (title, left) in enumerate(
            (("Имя", 260), ("Дата изменения", 760), ("Тип", 960), ("Размер", 1160))
        ):
            items.append(
                Widget(
                    f"col{i}", "Text", title, (left, 88, left + 190, 108), uia=False, size=13, style="muted"
                )
            )
        error = str(window.data.get("error") or "")
        if error:
            items.append(
                Widget("error", "Text", error, (260, 120, width - 20, 144), size=14, color=(196, 43, 28))
            )
        if self.loading(window):
            items.append(Widget("loading", "Text", "Работаем над этим…", (260, 130, 700, 154), size=15))
            items.append(
                Widget(
                    "spinner",
                    "Image",
                    "",
                    (230, 128, 252, 150),
                    uia=False,
                    icon="spinner",
                    color=(0, 103, 192),
                )
            )
            return items
        listed = self.items(window)
        selected = str(window.data.get("selected") or "")
        for i, (path, is_dir) in enumerate(listed[:18]):
            top = 116 + i * 36
            name = FOLDERS.get(path) if not folder and path in FOLDERS else path.rsplit("\\", 1)[-1]
            kind = "Папка с файлами" if is_dir else file_type(name or "")
            items.append(
                Widget(
                    f"item:{path}",
                    "ListItem",
                    name or path,
                    (252, top, width - 12, top + 34),
                    icon="folder" if is_dir else "file",
                    style="selected" if path == selected else "",
                    on_click=lambda p=path: self.select(window, p),
                    on_double=lambda p=path, d=is_dir: self.open_item(window, p, d),
                )
            )
            items.append(
                Widget(
                    f"meta:{path}",
                    "Text",
                    f"18.10.2026 14:{10 + i:02d}",
                    (760, top + 9, 950, top + 27),
                    uia=False,
                    size=13,
                    style="muted",
                )
            )
            items.append(
                Widget(
                    f"type:{path}",
                    "Text",
                    kind,
                    (960, top + 9, 1150, top + 27),
                    uia=False,
                    size=13,
                    style="muted",
                )
            )
        if not listed:
            empty = "Ничего не найдено" if window.data.get("query") else "Эта папка пуста."
            items.append(Widget("empty", "Text", empty, (260, 130, 700, 154), size=14, style="muted"))
        items.append(
            Widget(
                "count",
                "Text",
                f"Элементов: {len(listed)}",
                (252, height - 26, 600, height - 8),
                size=12,
                style="muted",
            )
        )
        return items


class Viewer(App):
    """Что открывается двойным щелчком по файлу (кроме текста — его открывает Блокнот)."""

    name = "Microsoft Word"
    exe = "WINWORD.EXE"
    title = "Word"
    rect = (240, 70, 1640, 980)
    background = (232, 232, 236)
    single = False
    KINDS = {
        ".docx": ("WINWORD.EXE", "Word"),
        ".doc": ("WINWORD.EXE", "Word"),
        ".pdf": ("msedge.exe", "Microsoft Edge"),
        ".xlsx": ("EXCEL.EXE", "Excel"),
        ".jpg": ("Photos.exe", "Фотографии"),
        ".png": ("Photos.exe", "Фотографии"),
    }

    def __init__(self, world: World) -> None:
        super().__init__(world)

    def open_file(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].casefold()
        exe, app = self.KINDS.get(ext, ("", ""))
        if not exe:
            return False
        name = path.rsplit("\\", 1)[-1]
        window = self.world.open(self, f"{name} — {app}", self.rect, data={"path": path, "exe": exe})
        window.values["doc"] = self.world.files.get(path, "")
        return True

    def view(self, window: Window) -> list[Widget]:
        width, height = window.size
        name = str(window.data.get("path", "")).rsplit("\\", 1)[-1]
        return [
            Widget("page", "Pane", rect=(200, 50, width - 200, height - 20), color=(255, 255, 255)),
            Widget("name", "Text", name, (240, 80, width - 240, 110), size=20, style="header"),
            Widget(
                "doc",
                "Document",
                "Документ",
                (240, 130, width - 240, height - 60),
                value=window.values.get("doc", ""),
                size=15,
            ),
        ]
