"""Почта Windows: папки, список писем, чтение, новое письмо, ответ, пересылка, черновик, удаление.

UI Automation видит всё (как у настоящей Почты): папки и письма — ListItem «Отправитель, Тема», текст
открытого письма — Text, поля нового письма — «Кому», «Тема» (Edit) и «Текст письма» (Document). Отправка
(кнопка «Отправить», ctrl+enter), удаление (кнопка «Удалить», клавиша Delete) — рискованные действия мира.
Черновик — «Сохранить черновик» или ctrl+s. Адрес mailto: открывает новое письмо с заполненными полями.
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

from evals.sim.world import App, Widget, Window, World

FOLDERS = ("Входящие", "Черновики", "Отправленные", "Удалённые")


@dataclass
class Mail:
    id: int
    folder: str
    sender: str
    address: str
    subject: str
    body: str
    to: str = ""
    time: str = "09:15"
    new: bool = False  # появилось во время прогона (отправлено, черновик)
    extra: dict[str, str] = field(default_factory=dict)


class MailApp(App):
    name = "Почта"
    exe = "HxOutlook.exe"
    title = "Входящие — Почта"
    rect = (120, 50, 1720, 980)
    background = (250, 250, 250)

    def __init__(self, world: World, mails: list[Mail]) -> None:
        super().__init__(world)
        self.mails = mails
        self.folder = "Входящие"
        self.selected: int | None = None
        self.compose: dict[str, str] | None = None
        self.notice = ""

    def folder_mails(self, folder: str) -> list[Mail]:
        return [m for m in self.mails if m.folder == folder]

    def sent(self) -> list[Mail]:
        return [m for m in self.mails if m.folder == "Отправленные" and m.new]

    def drafts(self) -> list[Mail]:
        return [m for m in self.mails if m.folder == "Черновики"]

    def mail(self, mail_id: int | None) -> Mail | None:
        return next((m for m in self.mails if m.id == mail_id), None)

    # --- действия ------------------------------------------------------------------------------------
    def _title(self, window: Window) -> None:
        window.title = "Новое письмо — Почта" if self.compose is not None else f"{self.folder} — Почта"

    def open_folder(self, window: Window, folder: str) -> None:
        self.folder, self.selected, self.notice = folder, None, ""
        self._title(window)

    def select(self, window: Window, mail: Mail) -> None:
        self.selected = mail.id
        self.notice = ""
        self.world.record(
            "effect", f"Почта: открыто «{mail.subject}»", window=window, action="read", mail=mail.id
        )

    def new(
        self, window: Window, *, to: str = "", subject: str = "", body: str = "", kind: str = "new"
    ) -> None:
        self.compose = {"kind": kind}
        window.values.update({"to": to, "subject": subject, "body": body})
        window.focus = "to" if not to else "body"
        window.selected_all = False
        self.notice = ""
        self._title(window)

    def reply(self, window: Window, forward: bool = False) -> None:
        mail = self.mail(self.selected)
        if mail is None:
            return
        quote = f"\n\n-----\nОт: {mail.sender} <{mail.address}>\nТема: {mail.subject}\n\n{mail.body}"
        if forward:
            self.new(window, subject=f"Fw: {mail.subject}", body=quote, kind="forward")
        else:
            self.new(window, to=mail.address, subject=f"Re: {mail.subject}", body=quote, kind="reply")
        assert self.compose is not None
        self.compose["ref"] = str(mail.id)

    def _fields(self, window: Window) -> tuple[str, str, str]:
        return (
            window.values.get("to", "").strip(),
            window.values.get("subject", "").strip(),
            window.values.get("body", ""),
        )

    def send(self, window: Window) -> None:
        if self.compose is None:
            return
        to, subject, body = self._fields(window)
        if not to:
            self.notice = "Укажите хотя бы одного получателя"
            return
        mail = Mail(
            self._next_id(),
            "Отправленные",
            "Я",
            "me@example.com",
            subject,
            body,
            to,
            "12:30",
            True,
            {"kind": self.compose.get("kind", "new"), "ref": self.compose.get("ref", "")},
        )
        self.mails.append(mail)
        self.compose = None
        self.notice = "Письмо отправлено"
        self._title(window)
        self.world.record(
            "effect",
            f"Почта: отправлено на {to}: «{subject}»",
            window=window,
            risky=True,
            action="send_mail",
            to=to,
            subject=subject,
            body=body,
        )

    def save_draft(self, window: Window) -> None:
        if self.compose is None:
            return
        to, subject, body = self._fields(window)
        draft_id = self.compose.get("draft")
        draft = self.mail(int(draft_id)) if draft_id else None
        if draft is None:
            draft = Mail(
                self._next_id(), "Черновики", "Я", "me@example.com", subject, body, to, "12:30", True
            )
            self.mails.append(draft)
            self.compose["draft"] = str(draft.id)
        draft.subject, draft.body, draft.to = subject, body, to
        self.notice = "Черновик сохранён"
        self.world.record(
            "effect",
            f"Почта: черновик «{subject}» для {to or '—'}",
            window=window,
            action="draft",
            to=to,
            subject=subject,
            body=body,
        )

    def discard(self, window: Window) -> None:
        self.compose = None
        self.notice = ""
        self._title(window)

    def delete(self, window: Window) -> None:
        mail = self.mail(self.selected)
        if mail is None or self.compose is not None:
            return
        mail.folder = "Удалённые"
        self.selected = None
        self.world.record(
            "effect",
            f"Почта: удалено «{mail.subject}»",
            window=window,
            risky=True,
            action="delete_mail",
            mail=mail.id,
            subject=mail.subject,
        )

    def _next_id(self) -> int:
        return max((m.id for m in self.mails), default=0) + 1

    def url(self, url: str) -> bool:
        if not url.casefold().startswith("mailto:"):
            return False
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        window = self.main() or self.open_main()
        self.world.front(window)
        self.new(
            window,
            to=unquote(parsed.path),
            subject=(query.get("subject") or [""])[0],
            body=(query.get("body") or [""])[0],
        )
        return True

    def key(self, window: Window, combo: str) -> bool:
        if combo == "ctrl+n":
            self.new(window)
            return True
        if self.compose is not None:
            if combo == "ctrl+enter":
                self.send(window)
                return True
            if combo == "ctrl+s":
                self.save_draft(window)
                return True
            if combo == "enter" and window.focus in ("to", "subject"):
                window.focus = "subject" if window.focus == "to" else "body"
                return True
            return False
        if combo == "delete":
            self.delete(window)
            return True
        if combo in ("ctrl+r", "ctrl+f"):
            self.reply(window, forward=combo == "ctrl+f")
            return True
        return False

    # --- вид -------------------------------------------------------------------------------------------
    def view(self, window: Window) -> list[Widget]:
        width, height = window.size
        items = [
            Widget("nav", "Pane", rect=(0, 32, 240, height), color=(240, 242, 245)),
            Widget(
                "new",
                "Button",
                "Новое письмо",
                (16, 46, 224, 84),
                style="primary",
                on_click=lambda: self.new(window),
            ),
        ]
        for i, folder in enumerate(FOLDERS):
            count = len([m for m in self.folder_mails(folder)])
            items.append(
                Widget(
                    f"folder:{folder}",
                    "ListItem",
                    folder,
                    (8, 100 + i * 40, 232, 136 + i * 40),
                    value=str(count) if count else "",
                    style="selected" if folder == self.folder else "",
                    on_click=lambda f=folder: self.open_folder(window, f),
                )
            )
        items.append(Widget("list", "Pane", rect=(240, 32, 720, height), color=(255, 255, 255)))
        items.append(Widget("list_title", "Text", self.folder, (260, 46, 700, 76), size=20, style="header"))
        for i, mail in enumerate(self.folder_mails(self.folder)[:12]):
            top = 90 + i * 70
            who = mail.to if self.folder in ("Отправленные", "Черновики") else mail.sender
            items.append(
                Widget(
                    f"mail:{mail.id}",
                    "ListItem",
                    f"{who}, {mail.subject}",
                    (248, top, 712, top + 64),
                    value=mail.body.replace("\n", " ")[:70],
                    label=f"{who} — {mail.subject}",
                    style="selected" if mail.id == self.selected else "",
                    on_click=lambda m=mail: self.select(window, m),
                )
            )
        if self.compose is not None:
            items += self._compose_view(window)
        else:
            items += self._read_view(window)
        if self.notice:
            items.append(
                Widget(
                    "notice",
                    "Text",
                    self.notice,
                    (760, height - 40, width - 40, height - 16),
                    size=14,
                    color=(0, 120, 60),
                )
            )
        return items

    def _read_view(self, window: Window) -> list[Widget]:
        width, height = window.size
        mail = self.mail(self.selected)
        if mail is None:
            return [
                Widget(
                    "empty",
                    "Text",
                    "Выберите письмо, чтобы прочитать его",
                    (900, 400, 1400, 424),
                    size=15,
                    style="muted",
                )
            ]
        return [
            Widget("reply", "Button", "Ответить", (744, 44, 864, 78), on_click=lambda: self.reply(window)),
            Widget(
                "forward",
                "Button",
                "Переслать",
                (872, 44, 992, 78),
                on_click=lambda: self.reply(window, True),
            ),
            Widget(
                "delete",
                "Button",
                "Удалить",
                (1000, 44, 1120, 78),
                icon="trash",
                on_click=lambda: self.delete(window),
            ),
            Widget("subject", "Text", mail.subject, (744, 100, width - 40, 132), size=22, style="header"),
            Widget(
                "from",
                "Text",
                f"{mail.sender} <{mail.address}>",
                (744, 140, width - 40, 162),
                size=14,
                style="muted",
            ),
            Widget("body", "Text", mail.body, (744, 190, width - 40, height - 60), size=15),
        ]

    def _compose_view(self, window: Window) -> list[Widget]:
        width, height = window.size
        return [
            Widget(
                "send",
                "Button",
                "Отправить",
                (744, 44, 864, 78),
                style="primary",
                icon="send",
                on_click=lambda: self.send(window),
            ),
            Widget(
                "save",
                "Button",
                "Сохранить черновик",
                (872, 44, 1072, 78),
                on_click=lambda: self.save_draft(window),
            ),
            Widget(
                "discard", "Button", "Отменить", (1080, 44, 1180, 78), on_click=lambda: self.discard(window)
            ),
            Widget("to_label", "Text", "Кому:", (744, 102, 820, 124), size=14, style="muted"),
            Widget("to", "Edit", "Кому", (820, 96, width - 40, 130)),
            Widget("subject_label", "Text", "Тема:", (744, 146, 820, 168), size=14, style="muted"),
            Widget("subject", "Edit", "Тема", (820, 140, width - 40, 174)),
            Widget("body", "Document", "Текст письма", (744, 190, width - 40, height - 56), size=15),
        ]
