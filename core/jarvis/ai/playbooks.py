"""Как работать с программами: короткие проверенные приёмы для агента (клавиши, адреса, где что лежит в окне).

Агент получает подсказки тех программ, что открыты или упомянуты в задаче, а когда по ходу работы впереди
оказывается новая программа — её подсказку с ближайшим снимком окна. Подсказки — это знания Jarvis, не данные
из окон: им можно следовать. Не уверены в сочетании клавиш — не пишем его: неверная подсказка хуже никакой.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    exes: tuple[str, ...]  # имена процессов (без регистра)
    words: re.Pattern[str]  # слова в задаче или заголовке окна
    text: str


def _w(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        "telegram",
        "Telegram",
        ("telegram.exe", "ayugram.exe", "kotatogram.exe", "64gram.exe"),
        _w(r"телеграм|телеграмм|телег|telegram|\bтг\b|\btg\b"),
        """\
- Окно Telegram почти не видно UI Automation: в снимке окна мало элементов. Работай по снимку экрана
(screenshot) — click_point, type_text — и клавишами.
- Если подключены инструменты MCP для Telegram (их имена начинаются с mcp__ и говорят о чатах и сообщениях),
читай и пиши через них, а не через окно: сначала найди чат (поиск контактов, список чатов, поиск по имени
пользователя), возьми его id из ответа и только потом отправляй. id не выдумывай.
- Чат по имени пользователя (@username) открывает адрес: open_url «tg://resolve?domain=username» (без @).
- «Избранное» — ctrl+0. Esc закрывает поиск или открытый чат; alt+↑ и alt+↓ — предыдущий и следующий чат.
- Найти чат по имени: поле «Поиск» над списком чатов слева. Щёлкни его (если открыт чат, сначала Esc), введи
имя, подожди секунду, сделай снимок и щёлкни нужный чат в результатах — Enter не нажимай.
- Открытый чат: поле «Написать сообщение…» внизу. Щёлкни его, введи текст (type_text), Enter отправляет —
Jarvis спросит разрешения. Shift+Enter — новая строка. Перед отправкой убедись по снимку, что открыт нужный
чат (имя в шапке чата).""",
    ),
    Playbook(
        "browser",
        "Браузер",
        ("chrome.exe", "msedge.exe", "firefox.exe", "browser.exe", "opera.exe", "brave.exe", "vivaldi.exe"),
        _w(
            r"браузер|сайт|страниц|google|гугл|яндекс|youtube|ютуб|chrome|хром|edge|firefox|ссылк|вкладк|интернет"
        ),
        """\
- Перейти на адрес — open_url (откроется новая вкладка) или ctrl+l, ввести адрес, Enter.
- Вкладки: ctrl+t — новая, ctrl+w — закрыть, ctrl+tab — следующая, ctrl+shift+t — вернуть закрытую.
- Назад — alt+←, обновить — F5, найти на странице — ctrl+f.
- Поиск: open_url «https://www.google.com/search?q=…» или «https://yandex.ru/search/?text=…»
(запрос — прямо в адресе).
- YouTube: open_url «https://www.youtube.com/results?search_query=…», затем щёлкни видео; в плеере k — пауза,
f — во весь экран, j и l — на 10 с назад и вперёд.
- Элементы страниц в Chrome и Edge хорошо видны в снимке окна: поля и кнопки — по id (ui_type, ui_click).
- Если сайт просит войти, ввести код из SMS или пройти капчу — не пытайся обойти: остановись и попроси
пользователя (finish со статусом partial и что сделать).""",
    ),
    Playbook(
        "explorer",
        "Проводник",
        ("explorer.exe",),
        _w(r"проводник|папк|файл|документ|загрузк|рабочий стол|explorer"),
        """\
- Открыть Проводник — win+e. Адресная строка — ctrl+l: введи путь (например, «%USERPROFILE%\\Downloads»)
и нажми Enter.
- Наверх на уровень — alt+↑, назад — alt+←. Новая папка — ctrl+shift+n, переименовать — F2.
- Копировать/вставить — ctrl+c/ctrl+v, выделить всё — ctrl+a. Delete — в Корзину (Jarvis спросит).
- Файлы в списке — элементы ListItem или DataItem в снимке окна: щёлкай по id, открывай двойным щелчком
(click_point с double) или Enter после выбора.""",
    ),
    Playbook(
        "settings",
        "Параметры Windows",
        ("systemsettings.exe",),
        _w(
            r"параметр|настро\w* windows|wi-?fi|вай-?фай|bluetooth|блютуз|яркост|дисплей|экран|звук|"
            r"обновлени\w* windows|принтер|мыш|клавиатур|язык|время|фон|тем[ауы]\b"
        ),
        """\
- Нужный раздел открывай адресом через open_url: ms-settings:display (экран), ms-settings:sound (звук),
ms-settings:bluetooth, ms-settings:network-wifi, ms-settings:windowsupdate,
ms-settings:personalization-background (фон), ms-settings:defaultapps, ms-settings:printers,
ms-settings:mousetouchpad, ms-settings:regionlanguage, ms-settings:dateandtime, ms-settings:privacy.
- Переключатели и списки видны в снимке окна; меняй только то, о чём просили — Jarvis спросит разрешения.""",
    ),
    Playbook(
        "discord",
        "Discord",
        ("discord.exe", "discordptb.exe", "discordcanary.exe"),
        _w(r"дискорд|discord"),
        """\
- Быстрый переход к серверу, каналу или личным сообщениям — ctrl+k: введи название, выбери строку в списке
(щелчком или Enter).
- Поле сообщения — внизу канала; Enter отправляет (Jarvis спросит), Shift+Enter — новая строка. Esc закрывает
всплывающие окна. alt+↑ и alt+↓ — соседние каналы.""",
    ),
    Playbook(
        "whatsapp",
        "WhatsApp",
        ("whatsapp.exe", "whatsapp.root.exe"),
        _w(r"ватсап|вотсап|whatsapp"),
        """\
- Поиск чатов — поле «Поиск» над списком чатов (ctrl+f): введи имя, щёлкни нужный чат в результатах.
- Поле сообщения — внизу; Enter отправляет (Jarvis спросит), Shift+Enter — новая строка.""",
    ),
    Playbook(
        "office",
        "Microsoft Office",
        ("winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "olk.exe"),
        _w(r"\bворд\b|word|эксел|excel|таблиц|powerpoint|презентац|документ word|outlook|аутлук"),
        """\
- Лента (вкладки и кнопки) и поля хорошо видны в снимке окна — жми по id.
- ctrl+n — новый документ, ctrl+o — открыть, ctrl+s — сохранить (первый раз — окно «Сохранить как»: имя файла
в поле «Имя файла», затем «Сохранить»), F12 — «Сохранить как».
- Excel: перейти к ячейке — поле «Поле имени» слева над таблицей (ui_type «B2», затем Enter) или ctrl+g;
затем type_text значение и Enter. Tab — ячейка справа.
- Outlook: новое письмо — ctrl+n; поля «Кому», «Тема», текст письма; отправка — ctrl+enter или кнопка
«Отправить» (Jarvis спросит).""",
    ),
    Playbook(
        "mail",
        "Почта Windows",
        ("hxoutlook.exe",),
        _w(r"почт|письм|e-?mail|\bmail\b"),
        """\
- Новое письмо — кнопка «Новое письмо» / «Написать» (ctrl+n). Поля «Кому», «Тема» и текст письма — по id.
- Отправка — кнопка «Отправить» или ctrl+enter: Jarvis спросит разрешения и покажет текст.""",
    ),
    Playbook(
        "vscode",
        "VS Code",
        ("code.exe", "code - insiders.exe", "cursor.exe"),
        _w(r"vs ?code|visual studio code|\bкод\b|cursor"),
        """\
- Открыть файл по имени — ctrl+p; команды — ctrl+shift+p; найти — ctrl+f; сохранить — ctrl+s.
- Встроенный терминал (ctrl+`) запускает команды на компьютере: не вводи туда ничего без явной просьбы.""",
    ),
    Playbook(
        "spotify",
        "Spotify",
        ("spotify.exe",),
        _w(r"спотифай|spotify"),
        """\
- Пауза, следующий и предыдущий трек — команды Jarvis (run_command «пауза», «следующий трек»): они
надёжнее кликов.
- Поиск: open_url «spotify:search:название» откроет поиск в приложении; дальше — снимок и щелчок по
результату (воспроизведение — двойной щелчок по треку или кнопка ▶).""",
    ),
    Playbook(
        "notepad",
        "Блокнот",
        ("notepad.exe",),
        _w(r"блокнот|notepad"),
        """\
- Текст — в поле документа (ui_type заменит весь текст; дописать — щёлкни в поле, ctrl+end, type_text).
- Сохранить — ctrl+s; в окне «Сохранить как» — имя в поле «Имя файла» и кнопка «Сохранить».""",
    ),
)

BY_ID = {p.id: p for p in PLAYBOOKS}


def for_exe(exe: str) -> Playbook | None:
    name = (exe or "").strip().casefold()
    return next((p for p in PLAYBOOKS if name in p.exes), None) if name else None


def relevant(task: str, exes: list[str], titles: list[str] | None = None, limit: int = 3) -> list[Playbook]:
    """Подсказки к задаче: по словам задачи — первыми, затем по открытым окнам (активное — первым)."""
    out: list[Playbook] = []
    for playbook in PLAYBOOKS:
        if playbook.words.search(task or ""):
            out.append(playbook)
    for exe in exes:
        playbook = for_exe(exe)
        if playbook is not None and playbook not in out:
            out.append(playbook)
    # мессенджеры в браузере (Telegram Web, Discord в Chrome) узнаются по заголовку окна
    web = [p for p in PLAYBOOKS if p.id in ("telegram", "discord", "whatsapp")]
    for title in titles or []:
        for playbook in web:
            if playbook not in out and playbook.words.search(title):
                out.append(playbook)
    return out[:limit]


def render(playbooks: list[Playbook]) -> str:
    if not playbooks:
        return ""
    return "\n\n".join(f"Как работать в «{p.title}»:\n{p.text}" for p in playbooks)
