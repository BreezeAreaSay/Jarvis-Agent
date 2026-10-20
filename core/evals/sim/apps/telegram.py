"""Telegram Desktop: список чатов, поиск, открытый чат, отправка.

Как настоящий: UI Automation видит только поле «Поиск» и поле сообщения — список чатов, результаты поиска,
шапка чата и сообщения нарисованы (их видно только на картинке). Заголовок окна — всегда «Telegram»,
имени чата в нём нет. Поиск: набрал имя — список результатов, Enter открывает первый (down / up
выделяют другой — Enter откроет его), щелчок — тот, по которому щёлкнули. В открытом чате фокус в поле
сообщения, Enter отправляет, shift+enter — новая строка, ctrl+f — поиск по чату, esc — закрыть чат. Адрес
tg://resolve?domain=имя открывает чат по @username.

Отправка — рискованное действие мира (risky): прогон сверяет его с разрешением человека.
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from evals.sim.world import App, Widget, Window, World, norm


@dataclass
class Message:
    text: str
    out: bool = False  # исходящее
    time: str = "12:00"
    new: bool = False  # отправлено во время прогона


@dataclass
class Chat:
    name: str
    username: str = ""
    messages: list[Message] = field(default_factory=list)
    unread: int = 0

    @property
    def sent(self) -> list[str]:
        """Что отправлено в этот чат во время прогона."""
        return [m.text for m in self.messages if m.new and m.out]


class Telegram(App):
    name = "Telegram"
    exe = "Telegram.exe"
    title = "Telegram"
    background = (231, 235, 240)

    def __init__(self, world: World, chats: list[Chat]) -> None:
        super().__init__(world)
        self.chats = chats
        self.open_chat: str | None = None
        self.highlight = -1  # выделенный результат поиска (-1 — ещё не выбирали: Enter откроет первый)
        self.chat_search = False  # ctrl+f в открытом чате

    @property
    def path(self) -> str:
        return "C:\\Users\\me\\AppData\\Roaming\\Telegram Desktop\\Telegram.exe"

    def chat(self, name: str) -> Chat:
        return next(c for c in self.chats if c.name == name)

    def sent(self) -> dict[str, list[str]]:
        """Чат → отправленное во время прогона (только чаты, куда что-то ушло)."""
        return {c.name: c.sent for c in self.chats if c.sent}

    def results(self, window: Window) -> list[Chat]:
        query = norm(window.values.get("search", ""))
        if not query:
            return []
        words = query.split()
        return [c for c in self.chats if all(w in norm(f"{c.name} {c.username}") for w in words)]

    # --- действия ------------------------------------------------------------------------------------
    def open(self, window: Window, name: str) -> None:
        self.open_chat = name
        self.chat_search = False
        window.values["search"] = ""
        window.values.setdefault(f"draft:{name}", "")
        window.values["message"] = window.values.get(f"draft:{name}", "")
        window.focus = "message"
        window.selected_all = False
        self.chat(name).unread = 0
        self.world.record(
            "effect", f"Telegram: открыт чат «{name}»", window=window, action="open_chat", chat=name
        )
        self.world.emit("tg.opened", name)

    def send(self, window: Window) -> None:
        text = window.values.get("message", "").strip()
        if not text or self.open_chat is None:
            return
        chat = self.chat(self.open_chat)
        chat.messages.append(Message(text, out=True, time="12:30", new=True))
        window.values["message"] = ""
        window.values[f"draft:{chat.name}"] = ""
        self.world.record(
            "effect",
            f"Telegram: отправлено в «{chat.name}»: «{text}»",
            window=window,
            risky=True,
            action="send",
            chat=chat.name,
            text=text,
        )
        self.world.emit("tg.sent", chat.name, text)

    def changed(self, window: Window, key: str) -> None:
        if key == "search":
            self.highlight = -1
        elif key == "message" and self.open_chat is not None:
            window.values[f"draft:{self.open_chat}"] = window.values.get("message", "")

    def key(self, window: Window, combo: str) -> bool:
        if window.focus == "search":
            results = self.results(window)
            if combo == "enter" and results:
                self.open(window, results[min(max(self.highlight, 0), len(results) - 1)].name)
                return True
            if combo in ("down", "up") and results:
                step = 1 if combo == "down" else -1
                self.highlight = max(0, min(len(results) - 1, self.highlight + step))
                return True
            if combo == "esc":
                window.values["search"] = ""
                window.focus = "message" if self.open_chat else ""
                return True
        if window.focus == "message" and self.open_chat is not None:
            if combo == "enter":
                self.send(window)
                return True
            if combo == "shift+enter":
                window.values["message"] = window.values.get("message", "") + "\n"
                self.changed(window, "message")
                return True
        if combo == "ctrl+f":
            if self.open_chat is not None:
                self.chat_search = True
                window.focus = "chat_search"
            else:
                window.focus = "search"
            window.selected_all = True
            return True
        if combo == "esc":
            if self.chat_search:
                self.chat_search = False
                window.focus = "message"
            elif self.open_chat is not None:
                self.open_chat = None
                window.focus = ""
            return True
        if combo == "ctrl+0":
            saved = next((c for c in self.chats if c.name == "Избранное"), None)
            if saved is not None:
                self.open(window, saved.name)
            return True
        return False

    def url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "tg":
            return False
        domain = (parse_qs(parsed.query).get("domain") or [""])[0].lstrip("@").casefold()
        window = self.main() or self.open_main()
        self.world.front(window)
        chat = next((c for c in self.chats if c.username and c.username.casefold() == domain), None)
        if chat is not None:
            self.open(window, chat.name)
        return True

    # --- вид -------------------------------------------------------------------------------------------
    def view(self, window: Window) -> list[Widget]:
        width, height = window.size
        items: list[Widget] = [
            Widget("left", "Pane", rect=(0, 32, 380, height), color=(255, 255, 255)),
            Widget("menu", "Button", "", (10, 42, 50, 82), uia=False, icon="menu", style="flat"),
            Widget(
                "search",
                "Edit",
                "Поиск",
                (58, 44, 368, 80),
                placeholder="Поиск",
                icon="search",
                on_enter=None,
            ),
        ]
        results = self.results(window)
        if window.values.get("search"):
            items.append(
                Widget(
                    "found", "Text", "Чаты и контакты", (20, 96, 360, 116), uia=False, size=13, style="muted"
                )
            )
            for i, chat in enumerate(results):
                top = 120 + i * 66
                items.append(
                    self._row(window, chat, f"result:{chat.name}", top, selected=i == self.highlight)
                )
            if not results:
                items.append(
                    Widget(
                        "nothing", "Text", "Ничего не найдено", (20, 130, 360, 150), uia=False, style="muted"
                    )
                )
        else:
            for i, chat in enumerate(self.chats):
                top = 92 + i * 66
                items.append(
                    self._row(window, chat, f"chat:{chat.name}", top, selected=chat.name == self.open_chat)
                )
        if self.open_chat is None:
            items.append(
                Widget(
                    "hint",
                    "Text",
                    "Выберите, кому хотели бы написать",
                    (720, 470, 1060, 492),
                    uia=False,
                    style="muted",
                )
            )
            return items
        chat = self.chat(self.open_chat)
        items += [
            Widget("head", "Pane", rect=(380, 32, width, 92), color=(255, 255, 255)),
            Widget("head_name", "Text", chat.name, (400, 42, 900, 64), uia=False, size=16, style="bold"),
            Widget(
                "head_seen", "Text", "был(а) недавно", (400, 66, 700, 84), uia=False, size=12, style="muted"
            ),
        ]
        if self.chat_search:
            items.append(
                Widget(
                    "chat_search",
                    "Edit",
                    "Поиск по чату",
                    (900, 46, width - 20, 80),
                    placeholder="Поиск по чату",
                    icon="search",
                )
            )
        bottom = height - 84
        for i, message in enumerate(reversed(chat.messages[-8:])):
            lines = max(1, len(message.text) // 52 + message.text.count("\n") + 1)
            box_h = 22 * lines + 30
            top = bottom - box_h
            if top < 100:
                break
            box_w = min(560, 40 + 11 * max(len(line) for line in message.text.split("\n")))
            left = width - 30 - box_w if message.out else 400
            items.append(
                Widget(
                    f"msg:{len(chat.messages) - i}",
                    "Text",
                    message.text,
                    (left, top, left + box_w, bottom),
                    uia=False,
                    value=message.time,
                    style="bubble-out" if message.out else "bubble-in",
                    size=15,
                )
            )
            bottom = top - 10
        items += [
            Widget("composer", "Pane", rect=(380, height - 66, width, height), color=(255, 255, 255)),
            Widget(
                "message",
                "Edit",
                "Написать сообщение…",
                (396, height - 58, width - 64, height - 10),
                placeholder="Написать сообщение…",
                multiline=True,
            ),
            Widget(
                "send",
                "Button",
                "",
                (width - 56, height - 58, width - 10, height - 10),
                uia=False,
                icon="send",
                style="flat",
                on_click=lambda: self.send(window),
            ),
        ]
        return items

    def _row(self, window: Window, chat: Chat, key: str, top: int, *, selected: bool) -> Widget:
        last = chat.messages[-1].text if chat.messages else ""
        prefix = "Вы: " if chat.messages and chat.messages[-1].out else ""
        return Widget(
            key,
            "ListItem",
            chat.name,
            (8, top, 372, top + 62),
            value=(prefix + last).replace("\n", " ")[:60] or " ",
            uia=False,
            icon="avatar",
            style="selected" if selected else "",
            on_click=lambda: self.open(window, chat.name),
        )
