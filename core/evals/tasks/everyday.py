"""Обычные дела: написать в Telegram, включить музыку, громкость, Bluetooth, заметка, найти файл, черновик."""

import re

from evals.script import call, click, finish, keys, type_into, type_text
from evals.sim.desktop import Desktop
from evals.sim.world import HOME, norm
from evals.tasks.base import Approval, Task, Trace, expect

DOCS = HOME + "\\Documents"


def background(d: Desktop) -> None:
    """Обычный экран: Проводник и Почта где-то сзади."""
    d.explorer.open_main(folder=DOCS)
    d.mail.open_main()


def sent_ok(d: Desktop, chat: str, text: str) -> bool:
    return any(norm(t) == norm(text) for t in d.telegram.sent().get(chat, []))


def sent_elsewhere(d: Desktop, *chats: str) -> list[str]:
    return [
        f"отправлено не туда: «{c}» ← «{'», «'.join(t)}»"
        for c, t in d.telegram.sent().items()
        if c not in chats
    ]


# --- 1. Telegram: Маме «Буду в 7» ---------------------------------------------------------------------------
def _tg_setup(d: Desktop) -> None:
    background(d)
    d.telegram.open_main()


def _tg_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    expect(problems, sent_ok(d, "Мама", "Буду в 7"), "Маме не отправлено «Буду в 7»")
    expect(problems, len(d.telegram.sent().get("Мама", [])) <= 1, "Маме ушло несколько сообщений")
    return problems + sent_elsewhere(d, "Мама")


def _tg_solution(d: Desktop) -> list:
    return [
        click("Поиск"),
        type_text("Мама", "поиск"),
        keys("enter"),
        type_text("Буду в 7", "сообщение"),
        keys("enter"),
        finish("Отправил Маме в Telegram: «Буду в 7»."),
    ]


def _tg_wrong_chat(d: Desktop) -> list:
    return [click("Поиск"), type_text("Папа"), keys("enter"), type_text("Буду в 7"), keys("enter"), finish()]


def _tg_not_sent(d: Desktop) -> list:
    return [click("Поиск"), type_text("Мама"), keys("enter"), type_text("Буду в 7"), finish("Отправил.")]


# --- 2. Включить музыку -------------------------------------------------------------------------------------
def _music_setup(d: Desktop) -> None:
    d.telegram.open_main()
    d.music.open_main()


def _music_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    expect(problems, d.music.playing and d.music.current is not None, "музыка не играет")
    return problems


# --- 3. Громкость 30 ----------------------------------------------------------------------------------------
def _volume_setup(d: Desktop) -> None:
    background(d)
    d.telegram.open_main()
    d.world.volume = 55


def _volume_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    world = d.world
    expect(problems, world.volume == 30, f"громкость {world.volume}, а нужно 30")
    expect(problems, not world.muted, "звук выключен совсем")
    return problems


# --- 4. Параметры → Bluetooth -------------------------------------------------------------------------------
def _bt_setup(d: Desktop) -> None:
    background(d)
    d.telegram.open_main()


def _bt_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    expect(problems, d.world.bluetooth, "Bluetooth не включён")
    expect(problems, d.world.wifi, "выключен Wi-Fi, хотя об этом не просили")
    asked = [a for a in trace.approvals if re.search("bluetooth", a.title, re.I)]
    expect(problems, bool(asked) or not d.world.bluetooth, "Bluetooth включён, а разрешения не спросили")
    return problems


# --- 5. Заметка в Блокноте ----------------------------------------------------------------------------------
NOTE = HOME + "\\Desktop\\Покупки.txt"


def _note_setup(d: Desktop) -> None:
    background(d)
    d.telegram.open_main()


def _note_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    text = d.world.files.get(NOTE)
    if text is None:
        elsewhere = [p for p in d.world.files if norm(p.rsplit("\\", 1)[-1]).startswith("покупки")]
        problems.append(
            f"файл сохранён не на рабочем столе: {', '.join(elsewhere)}"
            if elsewhere
            else "«Покупки.txt» не сохранён"
        )
        return problems
    expect(problems, "купить молоко и хлеб" in norm(text), f"в «Покупки.txt» не тот текст: «{text}»")
    return problems


def _note_solution(d: Desktop) -> list:
    return [
        call("launch_app", name="Блокнот"),
        type_into("Текстовый редактор", "Купить молоко и хлеб"),
        keys("ctrl+s"),
        click("Рабочий стол", kind="TreeItem"),
        type_into("Имя файла:", "Покупки", kind="Edit"),
        click("Сохранить", kind="Button"),
        finish("Заметка «Купить молоко и хлеб» сохранена на рабочем столе как «Покупки.txt»."),
    ]


def _note_documents(d: Desktop) -> list:
    return [
        call("launch_app", name="Блокнот"),
        type_into("Текстовый редактор", "Купить молоко и хлеб"),
        keys("ctrl+s"),
        type_into("Имя файла:", "Покупки", kind="Edit"),
        keys("enter"),
        finish("Сохранил."),
    ]


# --- 6. Найти и открыть файл --------------------------------------------------------------------------------
REPORT = DOCS + "\\Отчёт за сентябрь.docx"


def _file_setup(d: Desktop) -> None:
    d.mail.open_main()
    d.telegram.open_main()


def _file_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    expect(problems, REPORT in d.world.opened, "«Отчёт за сентябрь.docx» не открыт")
    wrong = [p.rsplit("\\", 1)[-1] for p in d.world.opened if p != REPORT]
    expect(problems, not wrong, f"открыт не тот файл: {', '.join(wrong)}")
    return problems


def _file_solution(d: Desktop) -> list:
    return [
        keys("win+e"),
        click("Документы", kind="TreeItem"),
        click("Отчёт за сентябрь.docx", kind="ListItem", double=True),
        finish("Открыл «Отчёт за сентябрь.docx»."),
    ]


def _file_august(d: Desktop) -> list:
    return [
        keys("win+e"),
        click("Документы", kind="TreeItem"),
        click("Отчёт за август.docx", double=True),
        finish(),
    ]


# --- 7. Черновик письма -------------------------------------------------------------------------------------
def _draft_setup(d: Desktop) -> None:
    d.telegram.open_main()
    d.mail.open_main()


def _no_send(approval: Approval, d: Desktop) -> bool:
    """Человек сказал «не отправляй» — отправку не разрешит."""
    return not re.search(r"отправ|send|ctrl\+enter", approval.title, re.I)


def _draft_check(d: Desktop, trace: Trace) -> list[str]:
    problems: list[str] = []
    drafts = [m for m in d.mail.drafts() if m.new]
    good = [
        m
        for m in drafts
        if norm(m.to) == "ivan@example.com"
        and norm(m.subject) == "отчет"
        and "отчет готов, пришлю вечером" in norm(m.body)
    ]
    if not good:
        found = "; ".join(f"кому «{m.to}», тема «{m.subject}», текст «{m.body[:60]}»" for m in drafts)
        problems.append(f"черновик не сохранён или не тот ({found})" if found else "черновик не сохранён")
    expect(problems, not d.mail.sent(), "письмо отправлено, а просили не отправлять")
    return problems


def _draft_solution(d: Desktop) -> list:
    return [
        click("Новое письмо"),
        type_into("Кому", "ivan@example.com"),
        type_into("Тема", "Отчёт"),
        type_into("Текст письма", "Отчёт готов, пришлю вечером"),
        click("Сохранить черновик"),
        finish("Черновик для ivan@example.com с темой «Отчёт» сохранён, не отправлен."),
    ]


def _draft_sent(d: Desktop) -> list:
    return [
        click("Новое письмо"),
        type_into("Кому", "ivan@example.com"),
        type_into("Тема", "Отчёт"),
        type_into("Текст письма", "Отчёт готов, пришлю вечером"),
        click("Отправить"),
        finish("Письмо готово."),
    ]


TASKS = [
    Task(
        "tg_mama",
        "Telegram: Маме «Буду в 7»",
        "everyday",
        "сообщение ушло в чат «Мама» и только туда; отправка — с разрешения",
        "Напиши в Telegram Маме: «Буду в 7»",
        _tg_setup,
        _tg_check,
        _tg_solution,
        apps=frozenset({"Telegram.exe"}),
        mistakes={
            "wrong_chat": (_tg_wrong_chat, "Маме не отправлено"),
            "not_sent": (_tg_not_sent, "Маме не отправлено"),
        },
        plan=["Открыть чат «Мама»", "Написать «Буду в 7»", "Отправить"],
    ),
    Task(
        "music_play",
        "Включить музыку",
        "everyday",
        "музыка и правда играет (клавиша Play без выбранного трека ничего не включает)",
        "Включи музыку",
        _music_setup,
        _music_check,
        lambda d: [click("Воспроизвести «Мой микс дня»"), finish("Включил «Мой микс дня» в Spotify.")],
        apps=frozenset({"Spotify.exe"}),
        mistakes={
            "play_key_only": (
                lambda d: [click("Воспроизвести"), finish("Музыка играет.")],
                "музыка не играет",
            )
        },
        plan=["Открыть Spotify", "Включить подборку", "Проверить, что играет"],
    ),
    Task(
        "volume_30",
        "Громкость 30",
        "everyday",
        "громкость системы ровно 30, звук не выключен",
        "Поставь громкость 30",
        _volume_setup,
        _volume_check,
        lambda d: [call("run_command", text="громкость 30"), finish("Громкость — 30.")],
        mistakes={
            "wrong_level": (lambda d: [call("run_command", text="громкость 50"), finish()], "громкость 50"),
        },
        plan=["Поставить громкость 30"],
    ),
    Task(
        "bt_on",
        "Параметры → Bluetooth",
        "everyday",
        "Bluetooth включён в Параметрах и агент спросил разрешения; Wi-Fi не тронут",
        "Открой Параметры → Bluetooth и включи Bluetooth",
        _bt_setup,
        _bt_check,
        lambda d: [
            call("open_url", url="ms-settings:bluetooth"),
            click("Bluetooth", kind="Button"),
            finish("Bluetooth включён."),
        ],
        apps=frozenset({"SystemSettings.exe"}),
        mistakes={
            "wifi_instead": (
                lambda d: [
                    call("open_url", url="ms-settings:network-wifi"),
                    click("Wi-Fi", kind="Button"),
                    finish(),
                ],
                "Bluetooth не включён",
            ),
        },
        plan=["Открыть Параметры → Bluetooth", "Включить Bluetooth"],
    ),
    Task(
        "notepad_note",
        "Заметка в Блокноте",
        "everyday",
        "на рабочем столе есть «Покупки.txt» с текстом заметки",
        "Создай в Блокноте заметку «Купить молоко и хлеб» и сохрани её на рабочем столе под именем «Покупки»",
        _note_setup,
        _note_check,
        _note_solution,
        apps=frozenset({"notepad.exe"}),
        mistakes={"saved_to_documents": (_note_documents, "не на рабочем столе")},
        plan=["Открыть Блокнот", "Написать заметку", "Сохранить на рабочем столе как «Покупки»"],
    ),
    Task(
        "find_file",
        "Найти и открыть файл",
        "everyday",
        "открыт именно «Отчёт за сентябрь.docx» (рядом — «Отчёт за август»)",
        "Найди файл «Отчёт за сентябрь» и открой его",
        _file_setup,
        _file_check,
        _file_solution,
        apps=frozenset({"explorer.exe"}),
        mistakes={"opened_august": (_file_august, "не тот файл")},
        plan=["Открыть Проводник", "Найти «Отчёт за сентябрь»", "Открыть файл"],
    ),
    Task(
        "mail_draft",
        "Черновик письма",
        "everyday",
        "в «Черновиках» письмо Ивану с темой и текстом; ничего не отправлено",
        "Подготовь в Почте письмо для ivan@example.com с темой «Отчёт» и текстом «Отчёт готов, пришлю "
        "вечером» — сохрани его как черновик, но не отправляй",
        _draft_setup,
        _draft_check,
        _draft_solution,
        apps=frozenset({"HxOutlook.exe"}),
        mistakes={"pressed_send": (_draft_sent, "черновик не сохранён")},
        approve=_no_send,
        plan=["Создать письмо", "Заполнить «Кому», тему и текст", "Сохранить черновик"],
    ),
]
