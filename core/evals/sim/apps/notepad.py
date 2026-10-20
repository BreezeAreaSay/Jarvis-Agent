"""Блокнот: документ, меню «Файл», окно «Сохранить как», вопрос о несохранённых изменениях.

Документ — Document для UI Automation (ui_type заменяет весь текст); набор с клавиатуры дописывает в конец
(курсор всегда там). ctrl+s сохраняет файл, у нового — открывает «Сохранить как»: папки слева, поле
«Имя файла:» (в нём уже выделено «*.txt»: набор заменит), «Сохранить». Имя может быть и полным путём
(«%USERPROFILE%\\Desktop\\Покупки.txt»). Несохранённое — «*» в заголовке; закрыть такое окно — вопрос
«Сохранить изменения…?».
"""

import os

from evals.sim.world import FOLDERS, HOME, App, Widget, Window, World, norm

DIALOG = (480, 200, 1440, 860)


def resolve_path(name: str, folder: str) -> str:
    """Имя из поля «Имя файла:» → полный путь (.txt, если расширения нет)."""
    name = name.strip().strip('"')
    for var in ("%USERPROFILE%", "%userprofile%", "~"):
        name = name.replace(var, HOME)
    name = name.replace("/", "\\")
    if "\\" not in name:
        name = folder.rstrip("\\") + "\\" + name
    elif name[1:3] != ":\\":
        name = HOME + "\\" + name.lstrip("\\")
    for path, title in FOLDERS.items():  # «C:\Users\me\Рабочий стол\…» — так тоже понимают
        name = name.replace(HOME + "\\" + title + "\\", path + "\\")
    if not os.path.splitext(name.rsplit("\\", 1)[-1])[1]:
        name += ".txt"
    return name


class Notepad(App):
    name = "Блокнот"
    exe = "notepad.exe"
    title = "Без имени — Блокнот"
    rect = (300, 120, 1400, 860)
    single = False

    def __init__(self, world: World) -> None:
        super().__init__(world)

    @property
    def path(self) -> str:
        return "C:\\Windows\\System32\\notepad.exe"

    # --- окна ------------------------------------------------------------------------------------------
    def open_main(self, **data: object) -> Window:
        path = str(data.get("path") or "")
        text = self.world.files.get(path, "") if path else ""
        window = self.world.open(self, "", self.rect, data={"path": path, "saved": text}, focus="doc")
        window.values["doc"] = text
        self._title(window)
        return window

    def open_file(self, path: str) -> bool:
        if not path.casefold().endswith((".txt", ".log", ".md", ".ini")):
            return False
        self.open_main(path=path)
        return True

    def _title(self, window: Window) -> None:
        if window.kind != "main":
            return
        path = str(window.data.get("path") or "")
        name = os.path.splitext(path.rsplit("\\", 1)[-1])[0] if path else "Без имени"
        dirty = window.values.get("doc", "") != window.data.get("saved", "")
        window.title = f"{'*' if dirty else ''}{name} — Блокнот"

    def changed(self, window: Window, key: str) -> None:
        if key == "doc":
            self._title(window)

    def editor(self, window: Window) -> Window:
        owner = window.data.get("owner")
        return owner if isinstance(owner, Window) else window

    # --- файл ------------------------------------------------------------------------------------------
    def save(self, window: Window, path: str = "") -> None:
        path = path or str(window.data.get("path") or "")
        if not path:
            self.save_as(window)
            return
        text = window.values.get("doc", "")
        self.world.files[path] = text
        window.data.update(path=path, saved=text)
        self._title(window)
        self.world.record(
            "effect", f"Блокнот: сохранён «{path}»", window=window, action="save", path=path, text=text
        )

    def save_as(self, window: Window) -> None:
        path = str(window.data.get("path") or "")
        folder = path.rsplit("\\", 1)[0] if path else HOME + "\\Documents"
        name = path.rsplit("\\", 1)[-1] if path else "*.txt"
        dialog = self.world.open(
            self, "Сохранение", DIALOG, kind="dialog", data={"owner": window, "folder": folder}, focus="name"
        )
        dialog.values["name"] = name
        dialog.selected_all = True

    def confirm_save(self, dialog: Window) -> None:
        name = dialog.values.get("name", "").strip()
        if not name or norm(name) in ("*.txt", "*"):
            dialog.data["error"] = "Введите имя файла"
            return
        path = resolve_path(name, str(dialog.data["folder"]))
        folder = path.rsplit("\\", 1)[0]
        if folder not in self.world.folders and not any(
            p.startswith(folder + "\\") for p in self.world.files
        ):
            dialog.data["error"] = f"Папка «{folder}» не найдена"
            return
        editor = self.editor(dialog)
        self.world.close(dialog, force=True)
        self.save(editor, path)
        self.world.front(editor)

    def ask_close(self, window: Window) -> None:
        dialog = self.world.open(
            self, "Блокнот", (660, 380, 1260, 600), kind="dialog", data={"owner": window, "ask": True}
        )
        dialog.focus = ""

    def close(self, window: Window) -> bool:
        if window.kind == "main" and window.values.get("doc", "") != window.data.get("saved", ""):
            self.ask_close(window)
            return False
        return True

    def _answer_close(self, dialog: Window, save: bool | None) -> None:
        editor = self.editor(dialog)
        self.world.close(dialog, force=True)
        if save is None:
            self.world.front(editor)
            return
        if save:
            if editor.data.get("path"):
                self.save(editor)
                self.world.close(editor, force=True)
            else:
                self.save_as(editor)
            return
        self.world.close(editor, force=True)

    # --- клавиши ---------------------------------------------------------------------------------------
    def key(self, window: Window, combo: str) -> bool:
        if window.kind == "dialog":
            if combo == "enter" and not window.data.get("ask"):
                self.confirm_save(window)
                return True
            return False
        menu = window.data.get("menu")
        if combo == "ctrl+s":
            self.save(window)
            return True
        if combo == "ctrl+shift+s":
            self.save_as(window)
            return True
        if combo == "ctrl+n":
            self.open_main()
            return True
        if combo == "esc" and menu:
            window.data["menu"] = False
            return True
        if combo in ("ctrl+end", "end"):
            window.focus = "doc"
            window.selected_all = False
            return True
        return False

    def menu(self, window: Window, item: str) -> None:
        window.data["menu"] = False
        if item == "save":
            self.save(window)
        elif item == "save_as":
            self.save_as(window)
        elif item == "new":
            self.open_main()
        elif item == "exit":
            self.world.close(window)

    # --- вид -------------------------------------------------------------------------------------------
    def view(self, window: Window) -> list[Widget]:
        if window.kind == "dialog":
            return self._ask_view(window) if window.data.get("ask") else self._save_view(window)
        width, height = window.size
        items = [
            Widget("file", "MenuItem", "Файл", (8, 34, 64, 62), on_click=lambda: self._toggle_menu(window)),
            Widget("edit", "MenuItem", "Правка", (68, 34, 140, 62)),
            Widget("viewmenu", "MenuItem", "Просмотр", (144, 34, 236, 62)),
            Widget("doc", "Document", "Текстовый редактор", (0, 64, width, height - 28), size=16),
        ]
        lines = window.values.get("doc", "").split("\n")
        items.append(
            Widget(
                "status",
                "Text",
                f"Строка {len(lines)}, столбец {len(lines[-1]) + 1}    UTF-8",
                (12, height - 24, 500, height - 6),
                uia=False,
                size=12,
                style="muted",
            )
        )
        if window.data.get("menu"):
            for i, (key, title) in enumerate(
                (
                    ("new", "Создать"),
                    ("open", "Открыть…"),
                    ("save", "Сохранить"),
                    ("save_as", "Сохранить как…"),
                    ("exit", "Выход"),
                )
            ):
                items.append(
                    Widget(
                        f"menu:{key}",
                        "MenuItem",
                        title,
                        (8, 64 + i * 36, 248, 100 + i * 36),
                        color=(249, 249, 249),
                        style="selected",
                        on_click=lambda k=key: self.menu(window, k),
                    )
                )
        return items

    def _toggle_menu(self, window: Window) -> None:
        window.data["menu"] = not window.data.get("menu")

    def _save_view(self, dialog: Window) -> list[Widget]:
        width, height = dialog.size
        folder = str(dialog.data["folder"])
        items = [
            Widget("address", "Text", "› " + FOLDERS.get(folder, folder), (20, 44, width - 20, 70), size=14),
        ]
        for i, (path, title) in enumerate(FOLDERS.items()):
            items.append(
                Widget(
                    f"nav:{path}",
                    "TreeItem",
                    title,
                    (12, 84 + i * 38, 220, 118 + i * 38),
                    icon="folder",
                    style="selected" if path == folder else "",
                    on_click=lambda p=path: self._go(dialog, p),
                )
            )
        for i, (path, is_dir) in enumerate(self.world.folder_items(folder)[:10]):
            if is_dir or path.casefold().endswith(".txt"):
                items.append(
                    Widget(
                        f"item:{path}",
                        "ListItem",
                        path.rsplit("\\", 1)[-1],
                        (240, 84 + i * 34, width - 20, 116 + i * 34),
                        icon="folder" if is_dir else "file",
                        on_click=lambda p=path, d=is_dir: self._pick(dialog, p, d),
                        on_double=lambda p=path, d=is_dir: self._pick(dialog, p, d, True),
                    )
                )
        error = str(dialog.data.get("error") or "")
        items += [
            Widget("name_label", "Text", "Имя файла:", (20, height - 150, 140, height - 128), size=14),
            Widget(
                "name",
                "Edit",
                "Имя файла:",
                (150, height - 158, width - 20, height - 122),
                on_enter=lambda: self.confirm_save(dialog),
            ),
            Widget("type_label", "Text", "Тип файла:", (20, height - 106, 140, height - 84), size=14),
            Widget(
                "type",
                "ComboBox",
                "Тип файла:",
                (150, height - 114, width - 20, height - 78),
                value="Текстовые документы (*.txt)",
            ),
            Widget("error", "Text", error, (20, height - 66, 500, height - 46), size=13, color=(196, 43, 28)),
            Widget(
                "save",
                "Button",
                "Сохранить",
                (width - 280, height - 60, width - 150, height - 22),
                style="primary",
                on_click=lambda: self.confirm_save(dialog),
            ),
            Widget(
                "cancel",
                "Button",
                "Отмена",
                (width - 140, height - 60, width - 20, height - 22),
                on_click=lambda: self.world.close(dialog, force=True),
            ),
        ]
        return items

    def _go(self, dialog: Window, folder: str) -> None:
        dialog.data["folder"] = folder
        dialog.data["error"] = ""

    def _pick(self, dialog: Window, path: str, is_dir: bool, open_: bool = False) -> None:
        if is_dir:
            if open_:
                self._go(dialog, path)
            return
        dialog.values["name"] = path.rsplit("\\", 1)[-1]

    def _ask_view(self, dialog: Window) -> list[Widget]:
        width, height = dialog.size
        editor = self.editor(dialog)
        name = os.path.splitext(str(editor.data.get("path") or "Без имени").rsplit("\\", 1)[-1])[0]
        return [
            Widget(
                "question",
                "Text",
                f"Вы хотите сохранить изменения в файле «{name}»?",
                (24, 56, width - 24, 100),
                size=16,
            ),
            Widget(
                "yes",
                "Button",
                "Сохранить",
                (width - 420, height - 60, width - 290, height - 22),
                style="primary",
                on_click=lambda: self._answer_close(dialog, True),
            ),
            Widget(
                "no",
                "Button",
                "Не сохранять",
                (width - 280, height - 60, width - 150, height - 22),
                on_click=lambda: self._answer_close(dialog, False),
            ),
            Widget(
                "cancel",
                "Button",
                "Отмена",
                (width - 140, height - 60, width - 20, height - 22),
                on_click=lambda: self._answer_close(dialog, None),
            ),
        ]
