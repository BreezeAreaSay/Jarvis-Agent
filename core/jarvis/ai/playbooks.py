"""Как работать с программами и сайтами: короткие проверенные приёмы для агента и для чата.

У каждой программы два текста: text — агенту, как работать в её окне (клавиши, адреса, где что лежит),
chat — модели в чате, какими инструментами чата это сделать (адреса, готовые команды, когда звать агента).
packs — паки команд Jarvis для этой программы: их готовые команды подсказываются вместе с приёмами
(jarvis.ai.apphints). Подсказки выбираются по словам просьбы и по открытым окнам; это знания Jarvis, не
данные из окон, — им можно следовать. Не уверены в сочетании клавиш или адресе — не пишем: неверная
подсказка хуже никакой.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    exes: tuple[str, ...]  # имена процессов (без регистра)
    words: re.Pattern[str]  # слова в задаче или заголовке окна
    text: str  # агенту: как работать в окне
    chat: str = ""  # чату: какими инструментами сделать
    packs: tuple[str, ...] = ()  # паки команд Jarvis для этой программы


def _w(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


# браузеры (имена процессов): Chrome, Edge, Firefox, Яндекс Браузер (browser.exe), Opera, Brave, Vivaldi, Arc
BROWSER_EXES = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "browser.exe",
        "opera.exe",
        "brave.exe",
        "vivaldi.exe",
        "arc.exe",
    }
)


PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        "telegram",
        "Telegram",
        ("telegram.exe", "ayugram.exe", "kotatogram.exe", "64gram.exe", "materialgram.exe"),
        _w(r"телеграм|телег[аеиуо]\b|telegram|\bтг\b|\btg\b"),
        """\
- Подключены инструменты MCP Telegram (имена mcp__…: чаты, сообщения) — читай и пиши через них, окно не нужно.
- Окно Telegram почти не видно UI Automation: работай клавишами и готовыми командами Telegram, а если \
видишь картинки — по снимку экрана (click_point).
- Найти чат по имени — поле «Поиск» вверху левой колонки, над списком чатов (справа от кнопки меню ☰). \
ctrl+f его НЕ открывает: в открытом чате ctrl+f ищет сообщения внутри этого чата. Поле «Поиск» есть среди \
элементов окна — ui_click по нему; нет — со зрением: снимок и click_point по полю «Поиск» слева вверху. \
Затем type_text — коротко имя или фамилия, wait 1 с и щелчок по нужному результату (или down и enter — \
откроется первый найденный).
- Найти сообщение в открытом чате — ctrl+f (поиск по чату); esc закрывает поиск.
- Заголовок окна имени чата не показывает: открытый чат проверяй по картинке (имя в шапке чата). Без зрения \
проверить нельзя — так и напиши в итоге.
- Чат или канал по @username — open_url «tg://resolve?domain=username» (без @): откроется сразу.
- «Избранное» — ctrl+0; папки — ctrl+1, ctrl+2…; соседние чаты — ctrl+tab и ctrl+shift+tab.
- Написать: в открытом чате курсор уже в поле сообщения — type_text; enter отправляет (Jarvis спросит и \
покажет текст), shift+enter — новая строка.""",
        """\
- Есть инструменты MCP Telegram — читай и пиши только через них (порядок — в блоке «Telegram через MCP»).
- Нет — простые действия в окне делай готовыми командами Telegram (run_command с id); чат или канал по \
@username открывает open_url «tg://resolve?domain=username».
- Найти чат по имени и написать в нём — сразу run_agent с полной задачей: кому и что дословно («В Telegram \
открой чат „Аня“ и отправь: „Буду в 7“»).""",
        ("telegram",),
    ),
    Playbook(
        "whatsapp",
        "WhatsApp",
        ("whatsapp.exe", "whatsapp.root.exe"),
        _w(r"ватсап|вотсап|whatsapp"),
        """\
- Чат по номеру — open_url «whatsapp://send?phone=79991234567» (код страны, без «+» и пробелов); \
&text=… вставит текст в поле, но не отправит.
- Поиск чатов — поле «Поиск» над списком (ctrl+f): введи имя, открой нужный чат щелчком или down и enter.
- Поле сообщения — внизу; enter отправляет (Jarvis спросит), shift+enter — новая строка.""",
        """\
- Чат по номеру телефона: open_url «whatsapp://send?phone=79991234567&text=…» — откроется чат с текстом \
в поле (сам не отправится).
- Найти чат по имени и написать — сразу run_agent с полной задачей (кому и что дословно).""",
    ),
    Playbook(
        "discord",
        "Discord",
        ("discord.exe", "discordptb.exe", "discordcanary.exe"),
        _w(r"дискорд|discord"),
        """\
- Сервер, канал или личные сообщения — ctrl+k (быстрый переход): введи название, enter.
- Поле сообщения — внизу канала; enter отправляет (Jarvis спросит), shift+enter — новая строка; esc \
закрывает всплывающие окна.
- Микрофон и звук — ctrl+shift+m и ctrl+shift+d; соседние каналы — alt+up и alt+down.""",
        """\
- Микрофон, звук, каналы — готовые команды Discord (run_command с id).
- Написать в канал или человеку — сразу run_agent с полной задачей (сервер и канал или имя, текст).""",
        ("discord",),
    ),
    Playbook(
        "vk",
        "ВКонтакте",
        (),
        _w(r"вконтакте|\bвк\b|\bvk\b|vk\.com"),
        """\
- Сообщения — open_url «https://vk.com/im»; беседу ищи полем над списком диалогов и открывай щелчком.
- Поле сообщения внизу; enter отправляет (Jarvis спросит). Поиск по сайту — поле «Поиск» вверху страницы.""",
        """\
- Сообщения ВКонтакте — open_url «https://vk.com/im»; написать человеку — сразу run_agent с полной \
задачей.""",
    ),
    Playbook(
        "messenger",
        "Мессенджер",
        ("slack.exe", "viber.exe", "signal.exe", "element.exe", "max.exe"),
        _w(r"мессенджер|вайбер|viber|slack|слак\b|\bsignal\b"),
        """\
- Найти чат — поле поиска над списком чатов (в Slack — ctrl+k): введи имя, открой нужный.
- Поле сообщения — внизу; enter отправляет (Jarvis спросит), shift+enter — новая строка.""",
        "- Найти чат и написать — сразу run_agent с полной задачей (мессенджер, кому, что дословно).",
    ),
    Playbook(
        "browser",
        "Браузер",
        tuple(sorted(BROWSER_EXES)),
        _w(
            r"браузер|сайт|страниц|вкладк|ссылк|\bинтернет|chrome|хром\w*|\bedge\b|эдж|firefox|фаерфокс|"
            r"файрфокс|\bопер[аеуы]\b|opera|brave|vivaldi"
        ),
        """\
- Сайт — open_url (новая вкладка) или ctrl+l, type_text адрес, enter. Поиск — прямо адресом: \
«https://www.google.com/search?q=…», «https://yandex.ru/search/?text=…».
- Вкладки: ctrl+t — новая, ctrl+w — закрыть, ctrl+tab и ctrl+shift+tab — соседние, ctrl+1…ctrl+8 — по \
номеру, ctrl+9 — последняя, ctrl+shift+t — вернуть закрытую.
- alt+left — назад, alt+right — вперёд, f5 — обновить, ctrl+f — найти на странице (esc — закрыть), \
ctrl+plus, ctrl+minus и ctrl+0 — масштаб, f11 — во весь экран.
- ctrl+h — история, ctrl+j — загрузки, ctrl+d — в закладки; инкогнито — ctrl+shift+n (в Firefox — \
ctrl+shift+p).
- Снимок окна браузера: сначала кнопки самого браузера (вкладки, адресная строка), потом «Страница …» — \
ссылки, поля и кнопки её видимой части (жми по id: ui_click, ui_type) — и «Текст страницы на экране»: \
читай его, чтобы понять, что на странице. В Firefox элементов бывает меньше — тогда по картинке.
- После open_url и перехода по ссылке страница грузится: wait 1–2 с, пока в снимке не появятся её элементы. \
Заголовок открытой страницы — в названии окна и в строке «Страница …».
- Нужного не видно — scroll (или page_down) и новый снимок; ctrl+f — найти слово на странице. Окна про \
cookies и подписки закрывай, только если мешают.
- Вход в аккаунт, коды из SMS, капча — не обходи: finish со статусом partial и что сделать пользователю.""",
        """\
- Сайт или поиск — open_url: Google «https://www.google.com/search?q=…», Яндекс \
«https://yandex.ru/search/?text=…», Википедия «https://ru.wikipedia.org/w/index.php?search=…».
- Вкладки и навигация в открытом браузере — готовые команды браузера (run_command с id) или press_keys: \
ctrl+t, ctrl+w, ctrl+shift+t, alt+left, f5.
- Любое дело на странице — найти на сайте, нажать кнопку, включить ролик, заполнить форму — сразу \
run_agent с полной задачей: он видит страницу, ты — нет.""",
        ("chrome", "firefox", "yandex_browser"),
    ),
    Playbook(
        "youtube",
        "YouTube",
        (),
        _w(r"youtube|ютуб|ютьюб|ютюб"),
        """\
- Поиск — open_url «https://www.youtube.com/results?search_query=запрос», wait 1–2 с, потом ui_click по \
ссылке ролика (Hyperlink с названием видео); ролики с пометкой «Реклама» или «Спонсировано» пропускай. \
Играет ли — по картинке или по кнопке «Пауза (k)» в снимке.
- Плеер (фокус на видео, не в поле поиска): k — пауза и пуск, j и l — на 10 с назад и вперёд, f — весь \
экран, t — кинотеатр, m — звук, c — субтитры, shift+n — следующее видео, shift+period и shift+comma — \
быстрее и медленнее, 0 — с начала.
- Буквы печатаются в поиск — нажми esc или щёлкни по видео.""",
        """\
- Найти видео — open_url «https://www.youtube.com/results?search_query=…»; включить конкретное — сразу \
run_agent («включи на YouTube первое видео по запросу …»).
- Пауза, перемотка, весь экран в открытом YouTube — готовые команды YouTube (run_command с id) или \
press_keys: k, l, j, f.""",
        ("youtube",),
    ),
    Playbook(
        "maps",
        "Карты",
        (),
        _w(r"\bкарт[аеуы]\b|на карте|маршрут|доехать|добраться|как пройти|проложи|навигатор|2гис|2gis"),
        """\
- Найти место: Яндекс Карты «https://yandex.ru/maps/?text=…», Google Maps \
«https://www.google.com/maps/search/?api=1&query=…».
- Маршрут: Яндекс «https://yandex.ru/maps/?rtext=Откуда~Куда&rtt=auto» (rtt: auto — машина, mt — \
транспорт, pd — пешком), Google «https://www.google.com/maps/dir/?api=1&origin=…&destination=…\
&travelmode=driving» (walking, transit, bicycling).""",
        """\
- Место — open_url: Яндекс Карты «https://yandex.ru/maps/?text=…», Google Maps \
«https://www.google.com/maps/search/?api=1&query=…».
- Маршрут — open_url «https://yandex.ru/maps/?rtext=Откуда~Куда&rtt=auto» (mt — транспорт, pd — пешком) \
или «https://www.google.com/maps/dir/?api=1&origin=…&destination=…&travelmode=driving».""",
    ),
    Playbook(
        "shopping",
        "Магазины",
        (),
        _w(
            r"озон|ozon|вайлдберриз|wildberries|\bwb\b|яндекс маркет|маркетплейс|авито|avito|aliexpress|"
            r"алиэкспресс|\bкупи|закажи|заказать|товар"
        ),
        """\
- Поиск товара: Ozon «https://www.ozon.ru/search/?text=…», Wildberries \
«https://www.wildberries.ru/catalog/0/search.aspx?search=…», Яндекс Маркет \
«https://market.yandex.ru/search?text=…», Avito «https://www.avito.ru/rossiya?q=…».
- Найти, сравнить и показать — можно; оформление заказа и оплату делает только пользователь: остановись \
перед оплатой и скажи, что осталось.""",
        """\
- Поиск товара — open_url: Ozon «https://www.ozon.ru/search/?text=…», Wildberries \
«https://www.wildberries.ru/catalog/0/search.aspx?search=…», Яндекс Маркет \
«https://market.yandex.ru/search?text=…», Avito «https://www.avito.ru/rossiya?q=…».
- Заказ и оплату делает только пользователь.""",
    ),
    Playbook(
        "video",
        "Видео и кино",
        (),
        _w(r"кинопоиск|kinopoisk|рутуб|rutube|twitch|твич|фильм|сериал"),
        """\
- Поиск: Кинопоиск «https://www.kinopoisk.ru/index.php?kp_query=…», Rutube \
«https://rutube.ru/search/?query=…», Twitch «https://www.twitch.tv/search?term=…».
- В плеерах сайтов обычно: пробел — пауза, f — весь экран, стрелки — перемотка и громкость.""",
        """\
- Поиск — open_url: Кинопоиск «https://www.kinopoisk.ru/index.php?kp_query=…», Rutube \
«https://rutube.ru/search/?query=…», Twitch «https://www.twitch.tv/search?term=…»; включить — run_agent.""",
    ),
    Playbook(
        "music",
        "Музыка",
        ("яндекс музыка.exe",),
        _w(r"яндекс музык|музык|трек|песн|плейлист|моя волна|моей волн"),
        """\
- Пауза, следующий и предыдущий трек — команды Jarvis «пауза», «следующий трек» (медиа-клавиши работают \
и в приложениях, и в браузере).
- Найти в Яндекс Музыке — open_url «https://music.yandex.ru/search?text=…», затем щёлкни трек или \
«Слушать».""",
        """\
- Пауза и треки — run_command «пауза», «следующий трек», «предыдущий трек».
- Найти в Яндекс Музыке — open_url «https://music.yandex.ru/search?text=…»; включить найденное — \
run_agent.""",
        ("yandex_music",),
    ),
    Playbook(
        "spotify",
        "Spotify",
        ("spotify.exe",),
        _w(r"спотифай|spotify"),
        """\
- Пауза, треки, громкость — готовые команды Spotify или команды Jarvis «пауза», «следующий трек».
- Поиск — open_url «spotify:search:запрос» (откроется поиск в приложении), затем снимок и двойной щелчок \
по треку или кнопка ▶.""",
        """\
- Найти — open_url «spotify:search:…» откроет поиск в приложении; включить конкретный трек — run_agent.
- Пауза и треки — run_command «пауза», «следующий трек» или готовые команды Spotify.""",
        ("spotify",),
    ),
    Playbook(
        "mail",
        "Почта",
        ("hxoutlook.exe", "thunderbird.exe"),
        _w(r"почт|письм|e-?mail|\bmail\b|gmail|джимейл|имейл"),
        """\
- Новое письмо: Gmail — open_url «https://mail.google.com/mail/?view=cm&fs=1&to=адрес&su=тема&body=текст» \
(поля заполнятся); почтовая программа по умолчанию — open_url «mailto:адрес?subject=…&body=…».
- Ящики: Gmail «https://mail.google.com», Яндекс Почта «https://mail.yandex.ru», Mail.ru \
«https://e.mail.ru».
- Отправка — кнопка «Отправить» или ctrl+enter: Jarvis спросит и покажет текст.""",
        """\
- Есть инструменты MCP почты — работай через них.
- Нет — открыть ящик: Gmail «https://mail.google.com», Яндекс Почта «https://mail.yandex.ru», Mail.ru \
«https://e.mail.ru»; новое письмо с заполненными полями — open_url \
«https://mail.google.com/mail/?view=cm&fs=1&to=…&su=…&body=…» или «mailto:адрес?subject=…&body=…».
- Написать и отправить письмо целиком — сразу run_agent с полной задачей (кому, тема, текст).""",
    ),
    Playbook(
        "google_docs",
        "Google Документы",
        (),
        _w(
            r"google doc|гугл док|google sheet|гугл табл|google slides|гугл презентац|google диск|гугл диск|"
            r"google drive"
        ),
        """\
- Новый документ — open_url «https://docs.new», таблица — «https://sheets.new», презентация — \
«https://slides.new»; Google Диск — «https://drive.google.com».""",
        """\
- Новый документ — open_url «https://docs.new», таблица — «https://sheets.new», презентация — \
«https://slides.new»; Google Диск — «https://drive.google.com».""",
    ),
    Playbook(
        "translate",
        "Переводчик",
        (),
        _w(r"переведи|перевод|translate"),
        """\
- Переводчик Google — open_url «https://translate.google.com/?sl=auto&tl=en&text=…&op=translate» (tl — \
на какой язык).""",
        """\
- Перевод делай сам, прямо в ответе. Переводчик открывай, только если просят: \
«https://translate.google.com/?sl=auto&tl=en&text=…&op=translate» (tl — на какой язык).""",
    ),
    Playbook(
        "explorer",
        "Проводник",
        ("explorer.exe",),
        _w(r"проводник|папк|\bфайл|рабочий стол|explorer"),
        """\
- Открыть Проводник — win+e. Адресная строка — ctrl+l (или alt+d): введи путь («%USERPROFILE%\\Downloads») \
и enter.
- Поиск в папке — ctrl+e: введи имя и enter. Наверх — alt+up, назад — alt+left, свойства — alt+enter.
- Новая папка — ctrl+shift+n, переименовать — f2, копировать и вставить — ctrl+c и ctrl+v, выделить всё — \
ctrl+a; delete — в Корзину (Jarvis спросит).
- Файлы в списке — элементы ListItem или DataItem снимка: выбери щелчком, открой двойным щелчком или \
enter.""",
        """\
- Папки — команды Jarvis: run_command «открой загрузки», «открой документы», «открой рабочий стол».
- Найти файл — run_command «найди файл <имя>»; не нашлось — run_agent.""",
        ("explorer",),
    ),
    Playbook(
        "settings",
        "Параметры Windows",
        ("systemsettings.exe",),
        _w(
            r"параметр\w* windows|настро\w* windows|wi-?fi|вай-?фай|bluetooth|блютуз|яркост|"
            r"разрешени\w* экрана|"
            r"обновлени\w* windows|принтер|чувствительност\w* мыш|раскладк|обои|тем[ауы] оформлени|"
            r"автозагрузк|ночной свет|программ\w* по умолчанию"
        ),
        """\
- Раздел открывай адресом (open_url): ms-settings:display (экран), ms-settings:nightlight, \
ms-settings:sound, ms-settings:apps-volume (громкость приложений), ms-settings:bluetooth, \
ms-settings:network-wifi, ms-settings:network-vpn, ms-settings:windowsupdate, \
ms-settings:personalization-background (фон), ms-settings:colors (тема и цвета), ms-settings:taskbar, \
ms-settings:defaultapps, ms-settings:appsfeatures (программы), ms-settings:startupapps (автозагрузка), \
ms-settings:notifications, ms-settings:printers, ms-settings:mousetouchpad, ms-settings:keyboard, \
ms-settings:regionlanguage, ms-settings:dateandtime, ms-settings:powersleep, ms-settings:batterysaver, \
ms-settings:storagesense (память), ms-settings:privacy-microphone, ms-settings:privacy-webcam, \
ms-settings:about (о системе).
- Переключатели и списки видны в снимке окна; меняй только то, о чём просили — Jarvis спросит.""",
        """\
- Громкость, яркость, Wi-Fi, Bluetooth — сначала команды Jarvis (run_command), они быстрее.
- Раздел Параметров — open_url адресом: ms-settings:display, ms-settings:sound, ms-settings:bluetooth, \
ms-settings:network-wifi, ms-settings:windowsupdate, ms-settings:personalization-background, \
ms-settings:colors, ms-settings:appsfeatures, ms-settings:startupapps, ms-settings:defaultapps.
- Переключить что-то внутри раздела — run_agent.""",
    ),
    Playbook(
        "word",
        "Word",
        ("winword.exe",),
        _w(r"\bворд\w*|\bword\b|docx"),
        """\
- Лента (вкладки и кнопки) и поля видны в снимке окна — жми по id.
- ctrl+n — новый документ, ctrl+o — открыть, ctrl+s — сохранить (в окне «Сохранить как» — имя в поле \
«Имя файла» и кнопка «Сохранить»), f12 — «Сохранить как».
- Текст печатай type_text в место курсора; ctrl+home и ctrl+end — в начало и конец документа; ctrl+b, \
ctrl+i, ctrl+u — жирный, курсив, подчёркнутый; ctrl+f — найти, ctrl+h — заменить.""",
        """\
- Оформление в открытом документе — готовые команды Word (run_command с id).
- Написать или отредактировать документ — run_agent с полным текстом задачи.""",
        ("word",),
    ),
    Playbook(
        "excel",
        "Excel",
        ("excel.exe",),
        _w(r"эксел|excel|xlsx|\bтаблиц"),
        """\
- К ячейке — ctrl+g (или «Поле имени» слева над таблицей): введи адрес «B2» и enter; затем type_text \
значение и enter (вниз) или tab (вправо).
- f2 — править ячейку, esc — отменить ввод. Формулы начинай с «=» на языке Excel пользователя (в русском \
— «=СУММ(A1:A10)», в английском — «=SUM(A1:A10)»); alt+equal — автосумма.
- ctrl+down и ctrl+right — к краю данных, ctrl+shift+l — фильтр, ctrl+t — таблица, ctrl+s — сохранить.""",
        """\
- Действия в открытой таблице — готовые команды Excel (run_command с id).
- Заполнить таблицу, посчитать, построить диаграмму — run_agent с полной задачей.""",
        ("excel",),
    ),
    Playbook(
        "powerpoint",
        "PowerPoint",
        ("powerpnt.exe",),
        _w(r"powerpoint|пауэрпоинт|паверпоинт|презентаци|слайд"),
        """\
- Показ — f5 (с начала) или shift+f5 (с текущего слайда); right и left — следующий и предыдущий слайд, \
esc — закончить показ.
- Новый слайд — ctrl+m; текст — щелчок по рамке на слайде (по id из снимка), затем type_text; ctrl+s — \
сохранить.""",
        "- Показ и слайды — готовые команды PowerPoint (run_command с id).",
        ("powerpoint",),
    ),
    Playbook(
        "outlook",
        "Outlook",
        ("outlook.exe", "olk.exe"),
        _w(r"outlook|аутлук"),
        """\
- Новое письмо — ctrl+n; поля «Кому», «Тема» и текст — по id; отправка — ctrl+enter или кнопка \
«Отправить» (Jarvis спросит и покажет текст).
- Входящие — ctrl+shift+i; поиск — ctrl+e.""",
        "- Написать письмо в Outlook — сразу run_agent с полной задачей (кому, тема, текст).",
    ),
    Playbook(
        "notepad",
        "Блокнот",
        ("notepad.exe",),
        _w(r"блокнот|notepad"),
        """\
- Текст — в поле документа (ui_type заменит весь текст; дописать — щёлкни в поле, ctrl+end, type_text).
- Сохранить — ctrl+s; в окне «Сохранить как» — имя в поле «Имя файла» и кнопка «Сохранить».""",
        "- Записать текст в Блокнот — run_agent с полным текстом.",
    ),
    Playbook(
        "vscode",
        "VS Code",
        ("code.exe", "code - insiders.exe", "cursor.exe"),
        _w(r"vs ?code|вс ?код|visual studio code|\bcursor\b"),
        """\
- Файл по имени — ctrl+p; команды — ctrl+shift+p; найти — ctrl+f, по всем файлам — ctrl+shift+f; \
сохранить — ctrl+s.
- Встроенный терминал (ctrl+backquote) запускает команды на компьютере: ничего туда не вводи без явной \
просьбы.""",
        "- Действия в редакторе — готовые команды VS Code (run_command с id).",
        ("vscode",),
    ),
    Playbook(
        "zoom",
        "Zoom",
        ("zoom.exe",),
        _w(r"\bзум\w*|zoom|конференци"),
        """\
- Во время встречи: alt+a — микрофон, alt+v — камера, alt+h — чат, alt+u — участники, alt+q — выйти \
(Zoom переспросит).
- Войти по ссылке — open_url её адреса (https://…zoom.us/j/…). Демонстрацию экрана (alt+s) включай только \
по прямой просьбе.""",
        """\
- Микрофон, камера, чат, выход — готовые команды Zoom (run_command с id).
- Войти во встречу — open_url её ссылки.""",
        ("zoom",),
    ),
    Playbook(
        "teams",
        "Microsoft Teams",
        ("ms-teams.exe", "teams.exe"),
        _w(r"\bteams\b|тимс"),
        """\
- Во время звонка: ctrl+shift+m — микрофон, ctrl+shift+o — камера.
- Написать человеку — open_url «https://teams.microsoft.com/l/chat/0/0?users=почта@компании», текст — \
в поле внизу; enter отправляет (Jarvis спросит).""",
        """\
- Чат с человеком — open_url «https://teams.microsoft.com/l/chat/0/0?users=почта@компании»; написать и \
отправить — run_agent с полной задачей.""",
    ),
    Playbook(
        "steam",
        "Steam",
        ("steam.exe", "steamwebhelper.exe"),
        _w(r"\bстим\w*|steam"),
        """\
- Библиотека, магазин, друзья, загрузки — готовые команды Steam (run_command с id).
- Запустить игру — launch_app по названию игры («Counter-Strike 2», «Dota 2»): Jarvis знает установленные \
игры Steam, Epic и GOG и запускает их сразу ссылкой лаунчера — по окну Steam для этого не щёлкай. Покупки и \
оплату не делай.""",
        """\
- Библиотека, магазин, друзья, загрузки — готовые команды Steam (run_command с id).
- Запустить игру — launch_app по её названию (сразу, без кликов по Steam). Покупки — только сам \
пользователь.""",
        ("steam",),
    ),
    Playbook(
        "obs",
        "OBS Studio",
        ("obs64.exe", "obs32.exe", "obs.exe"),
        _w(r"\bobs\b|\bобс\b|запис\w* экран|трансляци|\bстрим"),
        """\
- Запись и трансляция — кнопки «Начать запись» и «Начать трансляцию» справа внизу окна (по id из снимка) \
или готовые команды OBS (сработают, если в OBS заданы эти горячие клавиши). Трансляция показывает экран \
зрителям — Jarvis спросит.
- Сцены и источники — списки внизу окна: выбирай щелчком по названию.""",
        "- Запись, трансляция, сцены — готовые команды OBS (run_command с id); не сработали — run_agent.",
        ("obs",),
    ),
    Playbook(
        "player",
        "Видеоплеер",
        ("vlc.exe", "wmplayer.exe", "microsoft.media.player.exe"),
        _w(r"\bvlc\b|влс|плеер|проигрывател"),
        """\
- VLC: space — пауза, f — весь экран, n и p — следующий и предыдущий, alt+right и alt+left — перемотка, \
ctrl+up и ctrl+down — громкость, ctrl+o — открыть файл.
- Пауза и треки в любом плеере — команды Jarvis «пауза», «следующий трек».""",
        "- Пауза и треки — run_command «пауза», «следующий трек»; остальное в VLC — готовые команды VLC.",
        ("vlc",),
    ),
    Playbook(
        "notion",
        "Notion",
        ("notion.exe",),
        _w(r"notion|ноушн|ноушен"),
        """\
- Поиск страниц — ctrl+p, новая страница — ctrl+n; текст — щелчок в блок и type_text; «/» в пустой \
строке — меню блоков.""",
        "- Действия в Notion — готовые команды Notion (run_command с id); написать страницу — run_agent.",
        ("notion",),
    ),
    Playbook(
        "obsidian",
        "Obsidian",
        ("obsidian.exe",),
        _w(r"obsidian|обсидиан"),
        "- Перейти к заметке — ctrl+o (введи название, enter), новая заметка — ctrl+n, палитра команд — "
        "ctrl+p.",
        "- Найти или написать заметку в Obsidian — run_agent с полной задачей.",
    ),
    Playbook(
        "calculator",
        "Калькулятор",
        ("calculatorapp.exe", "calc.exe"),
        _w(r"калькулятор|calculator"),
        """\
- Выражение вводи с клавиатуры: type_text «12*7+5», затем enter; esc — сброс. Результат — в снимке окна \
(поле «Отображается …»).""",
        "- Посчитать — считай сам, прямо в ответе. Открыть калькулятор — launch_app «Калькулятор».",
    ),
    Playbook(
        "taskmgr",
        "Диспетчер задач",
        ("taskmgr.exe",),
        _w(r"диспетчер задач|task manager"),
        """\
- Открыть — ctrl+shift+esc. Закрывать программы — командой Jarvis «закрой <программу>» (run_command), \
а не «Снять задачу»: так не пропадут несохранённые данные.""",
        "- Закрыть зависшую программу — run_command «закрой <программу>».",
    ),
)

BY_ID = {p.id: p for p in PLAYBOOKS}

# паки команд без отдельной подсказки: по каким словам просьбы их предлагать (по окну — по процессу пака)
PACK_WORDS: dict[str, re.Pattern[str]] = {
    "photoshop": _w(r"photoshop|фотошоп"),
    "illustrator": _w(r"illustrator|иллюстратор"),
    "lightroom": _w(r"lightroom|лайтрум"),
    "premiere": _w(r"premiere|премьер"),
    "after_effects": _w(r"after effects|афтер эффект"),
    "davinci": _w(r"davinci|давинчи|резолв"),
    "blender": _w(r"blender|блендер"),
    "figma": _w(r"figma|фигм"),
    "minecraft": _w(r"minecraft|майнкрафт"),
    "cs2": _w(r"\bcs ?2\b|\bкс\b|counter.?strike|контр.?страйк"),
    "dota2": _w(r"\bdota|\bдот[аеуы]\b"),
}


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
    # мессенджеры и сайты в браузере (Telegram Web, YouTube в Chrome) узнаются по заголовку окна
    web = [p for p in PLAYBOOKS if p.id in ("telegram", "discord", "whatsapp", "youtube", "vk")]
    for title in titles or []:
        for playbook in web:
            if playbook not in out and playbook.words.search(title):
                out.append(playbook)
    return out[:limit]


def packs_for(task: str, found: list[Playbook]) -> list[str]:
    """Паки команд к просьбе: паки найденных подсказок и паки, названные словами просьбы."""
    out = [pack for p in found for pack in p.packs]
    out += [pack for pack, words in PACK_WORDS.items() if words.search(task or "")]
    return list(dict.fromkeys(out))


def render(playbooks: list[Playbook]) -> str:
    """Агенту: как работать в окнах этих программ."""
    if not playbooks:
        return ""
    return "\n\n".join(f"Как работать в «{p.title}»:\n{p.text}" for p in playbooks)


def render_chat(playbooks: list[Playbook]) -> str:
    """Чату: какими инструментами сделать дело в этих программах."""
    parts = [f"Как сделать в «{p.title}»:\n{p.chat}" for p in playbooks if p.chat]
    return "\n\n".join(parts)
