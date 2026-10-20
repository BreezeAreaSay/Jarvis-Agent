"""Имитатор рабочего стола (core/evals/sim): окна и фокус, ввод с клавиатуры и мышью, UI Automation,
картинка экрана, адреса, звук и музыка, медленная папка, «Сохранить как»."""

import pytest

from evals.sim.apps.notepad import resolve_path
from evals.sim.desktop import Desktop
from evals.sim.platform import SimPlatform
from evals.sim.ui import SimToasts, SimUi
from evals.sim.world import HOME, norm
from jarvis.ai.uia import UiError


@pytest.fixture
def desktop() -> Desktop:
    return Desktop.build()


def test_windows_focus_and_typing(desktop: Desktop) -> None:
    world = desktop.world
    desktop.mail.open_main()
    tg = desktop.telegram.open_main()
    platform = SimPlatform(world)
    windows = platform.list_windows()
    assert [w.title for w in windows] == ["Telegram", "Входящие — Почта"]  # сверху вниз
    assert windows[0].foreground and not windows[1].foreground
    assert platform.foreground_window().exe == "Telegram.exe"  # type: ignore[union-attr]
    # фокуса нет — набранное никуда не попало, но в журнале мира есть
    platform.type_text("потеряно")
    assert world.log[-1].kind == "type" and world.log[-1].data["lost"]
    # щелчок мышью по полю «Поиск» — фокус в нём, набор — туда; Enter открывает найденный чат
    left, top, right, bottom = tg.screen_rect(tg.widget("search").rect)  # type: ignore[union-attr]
    platform.mouse_click((left + right) // 2, (top + bottom) // 2)
    platform.type_text("мама")
    assert tg.values["search"] == "мама"
    platform.press_keys("enter")
    assert desktop.telegram.open_chat == "Мама" and tg.focus == "message"
    platform.type_text("Привет")
    platform.press_keys("shift+enter")
    platform.type_text("как дела")
    platform.press_keys("enter")
    assert desktop.telegram.sent() == {"Мама": ["Привет\nкак дела"]}
    send = world.effects("send")[0]
    assert send.risky and send.exe == "Telegram.exe"
    # alt+tab и окно по заголовку
    platform.press_keys("alt+tab")
    assert world.foreground is desktop.mail.main()
    platform.window_action("focus", "Telegram")
    assert world.foreground is tg


def test_snapshot_shows_only_ui_automation(desktop: Desktop) -> None:
    world = desktop.world
    tg = desktop.telegram.open_main()
    ui = SimUi(world)
    snap = ui.snapshot()
    assert (snap.window, snap.app, snap.pid) == ("Telegram", "Telegram.exe", tg.pid)
    # кнопки заголовка и поле «Поиск»; список чатов — только картинкой
    assert [(e.id, e.kind, e.name) for e in snap.elements] == [
        ("e1", "Button", "Свернуть"),
        ("e2", "Button", "Развернуть"),
        ("e3", "Button", "Закрыть"),
        ("e4", "Edit", "Поиск"),
    ]
    search = snap.elements[3]
    assert search.rect == tg.screen_rect(tg.widget("search").rect)  # type: ignore[union-attr]
    assert ui.set_text("e4", "Аня") == "текст вставлен" and tg.values["search"] == "Аня"
    assert ui.element_at(*search.center) == ("Поиск", "Edit")
    assert ui.owner_at(*search.center) == (tg.pid, "Telegram")
    with pytest.raises(UiError):
        ui.click("e9")
    desktop.telegram.open(tg, "Мама")
    names = [e.name for e in ui.snapshot().elements]
    assert names[3:] == ["Поиск", "Написать сообщение…"]
    # нажатие через UI Automation — «в фоне»: окно вперёд не выходит
    mail = desktop.mail.open_main()
    world.front(mail)
    snap = ui.snapshot(hwnd=tg.hwnd)
    message = next(e for e in snap.elements if e.name.startswith("Написать"))
    ui.click(message.id)
    assert world.foreground is mail and tg.focus == "message"
    world.close(tg, force=True)
    with pytest.raises(UiError, match="Окно закрылось"):
        ui.click(message.id)


def test_title_bar_modal_dialog_and_start_menu(desktop: Desktop) -> None:
    world = desktop.world
    platform = SimPlatform(world)
    ui = SimUi(world)
    popup = desktop.zoom.popup()
    close = next(e for e in ui.snapshot().elements if e.name == "Закрыть")
    platform.mouse_click(*close.center)  # крестик в заголовке закрывает окно
    assert popup not in world.windows
    editor = desktop.notepad.open_main()
    platform.type_text("черновик")
    platform.press_keys("ctrl+s")
    dialog = world.foreground
    assert dialog is not None and dialog.kind == "dialog"
    platform.mouse_click(editor.rect[0] + 300, editor.rect[3] - 60)  # мимо диалога — он остаётся впереди
    platform.window_action("focus", editor.hwnd)
    assert world.foreground is dialog and editor.values["doc"] == "черновик"
    world.close(dialog, force=True)
    # «Пуск»: win, набрать, Enter — запуск лучшего совпадения; ввод туда — не «чужое окно»
    platform.press_keys("win")
    assert world.foreground is not None and world.foreground.exe == "SearchHost.exe"
    platform.type_text("параметры")
    platform.press_keys("enter")
    assert world.foreground.exe == "SystemSettings.exe"  # type: ignore[union-attr]
    assert not any(w.exe == "SearchHost.exe" for w in world.windows)
    platform.press_keys("win")
    platform.type_text("отчёт за сентябрь")
    platform.press_keys("enter")
    assert world.opened == [HOME + "\\Documents\\Отчёт за сентябрь.docx"]
    assert "Поиск Windows" not in {a.name for a in platform.list_apps()}


def test_cursor_in_document(desktop: Desktop) -> None:
    world = desktop.world
    platform = SimPlatform(world)
    ui = SimUi(world)
    editor = desktop.notepad.open_main(path=HOME + "\\Documents\\Дела.txt")
    platform.type_text("Сначала: ")  # у открытого файла курсор в начале
    assert editor.values["doc"].startswith("Сначала: Купить корм коту")
    platform.press_keys("ctrl+end")
    platform.type_text("\nВ конце")
    assert editor.values["doc"].endswith("Оплатить интернет\nВ конце")
    platform.press_keys("ctrl+home")
    platform.press_keys("backspace")  # в начале — стирать нечего
    platform.type_text("!")
    assert editor.values["doc"].startswith("!Сначала")
    doc = next(e for e in ui.snapshot().elements if e.kind == "Document")
    ui.click(doc.id)  # щелчок под текстом — курсор в конце
    platform.type_text(".")
    assert editor.values["doc"].endswith("В конце.")


def test_folders_and_paths(desktop: Desktop) -> None:
    world = desktop.world
    platform = SimPlatform(world)
    platform.open_path(HOME.lower() + "\\downloads")  # «открой загрузки»: папка — в Проводнике, не «файл»
    window = world.foreground
    assert window is not None and window.title == "Загрузки — Проводник" and world.opened == []
    platform.open_path("c:\\users\\me\\documents\\отчёт за август.docx")
    assert world.opened == [HOME + "\\Documents\\Отчёт за август.docx"]  # путь — как он есть в мире


def test_picture_of_the_screen_follows_the_world(desktop: Desktop) -> None:
    tg = desktop.telegram.open_main()
    platform = SimPlatform(desktop.world)
    pixels, width, height = platform.screen_pixels((0, 0, 1920, 1080))
    assert (width, height) == (1920, 1080) and len(pixels) == width * height * 4
    box = tg.screen_rect(tg.widget("search").rect)  # type: ignore[union-attr]
    before = platform.screen_pixels(box)[0]
    assert platform.screen_pixels(box)[0] == before  # мир не менялся — картинка та же (из кэша)
    tg.values["search"] = "Аня Смирнова"
    desktop.world.touch()
    assert platform.screen_pixels(box)[0] != before  # набранное видно
    png = platform.screenshot(tg.hwnd)
    assert png.startswith(b"\x89PNG")
    # уведомление — на экране справа внизу, щелчок по нему — его действие
    clicked: list[bool] = []
    toast = desktop.world.toast("Telegram", "Аня", "Привет", on_click=lambda: clicked.append(True))
    (shown, rect), *_ = desktop.world.visible_toasts()
    assert shown is toast
    assert SimUi(desktop.world).owner_at(rect[0] + 5, rect[1] + 5)[1] == "Новое уведомление"
    platform.mouse_click(rect[0] + 10, rect[1] + 10)
    assert clicked == [True] and not desktop.world.visible_toasts()


async def test_toasts_for_the_agent(desktop: Desktop) -> None:
    source = SimToasts(desktop.world)
    assert await source.access(False) == "allowed"
    desktop.world.toast("Telegram", "Мама", "Позвони")
    (toast,) = await source.fetch()
    assert (toast.app, toast.title, toast.body) == ("Telegram", "Мама", "Позвони")


def test_addresses_open_programs(desktop: Desktop) -> None:
    world = desktop.world
    platform = SimPlatform(world)
    platform.open_url("tg://resolve?domain=team_chat")
    assert desktop.telegram.open_chat == "Работа" and world.foreground is desktop.telegram.main()
    platform.open_url("ms-settings:bluetooth")
    assert desktop.settings.page == "bluetooth" and world.foreground.exe == "SystemSettings.exe"  # type: ignore[union-attr]
    platform.open_url("mailto:ivan@example.com?subject=Привет")
    mail = desktop.mail.main()
    assert mail is not None and mail.values["to"] == "ivan@example.com" and mail.values["subject"] == "Привет"
    platform.open_url("https://example.com/page")
    assert world.foreground.title == "example.com — Microsoft Edge"  # type: ignore[union-attr]
    platform.launch(desktop.notepad.path)
    assert world.foreground.exe == "notepad.exe"  # type: ignore[union-attr]
    assert {"Telegram", "Блокнот", "Параметры", "Почта"} <= {a.name for a in platform.list_apps()}
    platform.open_path(HOME + "\\Documents\\Отчёт за сентябрь.docx")
    assert world.foreground.title == "Отчёт за сентябрь.docx — Word"  # type: ignore[union-attr]
    assert world.opened == [HOME + "\\Documents\\Отчёт за сентябрь.docx"]


def test_volume_and_music(desktop: Desktop) -> None:
    platform = SimPlatform(desktop.world)
    assert platform.volume_set(30) == 30 and desktop.world.volume == 30
    assert platform.now_playing() is None  # плеер не открыт
    desktop.music.open_main()
    assert platform.now_playing().status == "stopped"  # type: ignore[union-attr]
    platform.media_key("play_pause")  # трек не выбран — клавиша ничего не включает
    assert not desktop.music.playing
    desktop.music.play("Для бега")
    info = platform.now_playing()
    assert info is not None and (info.status, info.title) == ("playing", "Run")
    platform.media_key("play_pause")
    assert platform.now_playing().status == "paused"  # type: ignore[union-attr]


def test_slow_folder_and_explorer_keys(desktop: Desktop) -> None:
    world = desktop.world
    downloads = HOME + "\\Downloads"
    desktop.explorer.slow[downloads] = 8.0
    window = desktop.explorer.open_main(folder=downloads)
    ui = SimUi(world)
    assert [e.name for e in ui.snapshot().elements][-1] == "Работаем над этим…"
    world._t0 -= 9  # прошло 9 секунд мира
    names = [e.name for e in ui.snapshot().elements]
    assert "Счёт №125 от 15.10.pdf" in names
    window = desktop.explorer.open_main(folder=HOME + "\\Downloads")  # уже догрузилась — сразу
    assert not desktop.explorer.loading(window)
    platform = SimPlatform(world)
    platform.press_keys("ctrl+l")
    platform.type_text("%USERPROFILE%\\Documents")
    platform.press_keys("enter")
    assert window.data["folder"] == HOME + "\\Documents" and window.title == "Документы — Проводник"
    platform.press_keys("ctrl+e")
    platform.type_text("отчёт")
    found = [e.name for e in ui.snapshot().elements if e.kind == "ListItem"]
    assert found == ["Отчёт за август.docx", "Отчёт за сентябрь.docx"]
    platform.press_keys("enter")  # из поля поиска — в список
    platform.press_keys("down")
    platform.press_keys("delete")  # в Корзину — рискованное действие
    assert world.effects("delete")[0].risky and HOME + "\\Documents\\Отчёт за август.docx" not in world.files


def test_notepad_save_as_and_unsaved_question(desktop: Desktop) -> None:
    world = desktop.world
    platform = SimPlatform(world)
    ui = SimUi(world)
    editor = desktop.notepad.open_main()
    platform.type_text("Купить молоко")
    assert editor.title == "*Без имени — Блокнот"
    platform.press_keys("ctrl+s")
    dialog = world.foreground
    assert dialog is not None and dialog.title == "Сохранение" and dialog.kind == "dialog"
    snap = ui.snapshot()
    desk = next(e for e in snap.elements if e.name == "Рабочий стол")
    ui.click(desk.id)
    platform.type_text("в никуда")  # фокус ушёл в дерево папок: в поле имени ничего не попало
    assert dialog.values["name"] == "*.txt"
    platform.press_keys("tab")  # tab — в поле имени, «*.txt» выделено: набор заменит его
    platform.type_text("Покупки")
    platform.press_keys("enter")
    assert world.files[HOME + "\\Desktop\\Покупки.txt"] == "Купить молоко"
    assert world.foreground is editor and editor.title == "Покупки — Блокнот"
    platform.type_text("!")
    platform.press_keys("alt+f4")  # несохранённое — вопрос, окно не закрылось
    question = world.foreground
    assert question is not None and question.kind == "dialog" and editor in world.windows
    no = next(e for e in ui.snapshot().elements if e.name == "Не сохранять")
    ui.click(no.id)
    assert editor not in world.windows
    assert resolve_path("%USERPROFILE%\\Desktop\\a", HOME) == HOME + "\\Desktop\\a.txt"
    assert resolve_path("Рабочий стол\\b.md", HOME + "\\Documents") == HOME + "\\Desktop\\b.md"


def test_mail_compose_send_and_delete(desktop: Desktop) -> None:
    world = desktop.world
    window = desktop.mail.open_main()
    ui = SimUi(world)

    def press(name: str) -> None:
        ui.click(next(e for e in ui.snapshot().elements if e.name == name).id)

    press("Розыгрыш, Вы выиграли iPhone!")
    body = next(e for e in ui.snapshot().elements if e.kind == "Text" and "990" in e.name)
    assert body.name.startswith("Поздравляем")  # текст письма виден UI Automation
    press("Удалить")
    assert desktop.mail.mail(3).folder == "Удалённые"  # type: ignore[union-attr]
    press("Новое письмо")
    assert window.title == "Новое письмо — Почта"
    snap = ui.snapshot()
    to = next(e for e in snap.elements if e.name == "Кому")
    ui.set_text(to.id, "ivan@example.com")
    press("Сохранить черновик")
    assert [m.to for m in desktop.mail.drafts()] == ["ivan@example.com"]
    press("Отправить")
    (sent,) = desktop.mail.sent()
    assert sent.to == "ivan@example.com"
    actions = [(e.data["action"], e.risky) for e in world.effects() if e.data.get("action") != "read"]
    assert actions == [("delete_mail", True), ("draft", False), ("send_mail", True)]


def test_norm() -> None:
    assert norm(" «Буду  в 7.» ") == "буду в 7"
    assert norm("Отчёт") == "отчет"
