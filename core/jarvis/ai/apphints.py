"""Подсказки модели о программах, командах и сервисах — чтобы ИИ действовал точно и сразу.

К просьбе в чате (режим «Всё вместе», ChatGPT и программы-провайдеры) и к задаче агента добавляется блок:
- приёмы программ, о которых просьба (или что впереди, если сказано «здесь», «в этом окне») —
  jarvis.ai.playbooks: чату — какими инструментами чата сделать, агенту — как работать в окне;
- готовые команды Jarvis для этих программ из паков: установленные — по id для run_command (выполнится
  именно она), не установленные — только их сочетания клавиш; и команды, которые человек добавил сам
  (свои и сценарии), — он их добавил, чтобы ими пользовались;
- как пользоваться подключёнными сервисами MCP: Telegram — пошаговый рецепт по настоящим именам его
  инструментов, любой сервер — его собственные инструкции (initialize.instructions).
"""

import logging
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from jarvis.ai import playbooks
from jarvis.ai.format import one_line
from jarvis.ai.policy import APP_SCHEMES

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

McpSpec = tuple[str, str, dict[str, Any]]  # имя mcp__…, описание, схема параметров

MAX_PACKS = 4  # паков в подсказке
MAX_PACK_ITEMS = 16  # команд одного пака
MAX_ADDED = 30  # своих команд и сценариев
MAX_INSTRUCTIONS = 1500  # символов инструкций одного сервера MCP

_HERE = re.compile(
    r"(?<!\w)(?:здесь|тут|в этом|в этой|на этой|на этом|это окно|эту вкладку|этот чат|текущ\w*)(?!\w)", re.I
)
_TELEGRAM = re.compile(r"telegram|телеграм|(?:^|[_-])tg(?:[_-]|$)", re.I)
_SPLIT = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
_SEND = {"send", "reply", "forward"}
_CHANGE = {
    "delete", "remove", "edit", "ban", "kick", "block", "leave", "archive", "mute", "pin", "unpin", "clear",
    "create", "invite", "join", "set", "update", "mark", "add",
}  # fmt: skip
_READ = {"messages", "message", "history", "read", "unread"}
_FIND = {
    "search", "find", "resolve", "lookup", "contacts", "contact", "dialogs", "dialog", "chats", "chat",
    "username", "peer", "entity", "users", "user", "channels", "groups", "me",
}  # fmt: skip
_MEDIA = re.compile(r"file|photo|voice|sticker|media|document|gif|poll|location|video|audio|dice", re.I)


# --- приёмы программ и команды Jarvis ---------------------------------------------------------------------


def _short_name(name: str) -> str:
    """«Telegram: поиск по чату» → «поиск по чату»."""
    return name.split(": ", 1)[1] if ": " in name else name


def _openable(keys: str) -> bool:
    """Адрес из команды пака можно открыть самому (steam:// — только командой)."""
    if "://" not in keys and not keys.startswith(APP_SCHEMES):
        return True
    return keys.startswith(("http://", "https://", *APP_SCHEMES))


def pack_hints(
    ctx: "AppContext", apps: Iterable[str] = (), packs: Iterable[str] = (), skip: Iterable[str] = ()
) -> tuple[str, list[str]]:
    """Готовые команды паков для программ (процессы apps, id паков packs) и id показанных паков."""
    getter = getattr(ctx.commands, "app_commands", None)
    if getter is None:
        return "", []
    try:
        found: list[dict[str, Any]] = getter(list(apps), list(packs))
    except Exception:
        log.exception("команды паков для подсказки не собрались")
        return "", []
    skipped = set(skip)
    found = [p for p in found if p["pack"] not in skipped]
    found.sort(key=lambda p: not p.get("installed"))
    ready: list[str] = []
    keys: list[str] = []
    seen: list[set[str]] = []
    shown: list[str] = []
    for pack in found[:MAX_PACKS]:
        shown.append(pack["pack"])
        items = pack["items"][:MAX_PACK_ITEMS]
        if pack.get("installed"):
            entries = "; ".join(
                f"{i['id']} — {_short_name(i['name'])}" + (f" ({i['keys']})" if i["keys"] else "")
                for i in items
            )
            ready.append(f"{pack['name']}: {entries}")
            seen.append({i["keys"] for i in items if i["keys"]})
            continue
        usable = [i for i in items if i["keys"] and _openable(i["keys"])]
        signature = {i["keys"] for i in usable}
        if not usable or any(signature <= s for s in seen):
            continue  # те же клавиши, что у уже показанного браузера
        seen.append(signature)
        keys.append(
            f"{pack['name']}: " + "; ".join(f"{_short_name(i['name'])} — {i['keys']}" for i in usable)
        )
    parts = []
    if ready:
        parts.append(
            "Готовые команды Jarvis для программ (run_command с id — выполнится именно она, Jarvis сам "
            "переключится на окно программы):\n" + "\n".join(ready)
        )
    if keys:
        parts.append(
            "Сочетания клавиш программ (их паки команд не установлены — нажимай сам, press_keys в окне "
            "программы):\n" + "\n".join(keys)
        )
    return "\n\n".join(parts), shown


def commands_block(ctx: "AppContext", apps: Iterable[str] = (), packs: Iterable[str] = ()) -> str:
    return pack_hints(ctx, apps, packs)[0]


def added_block(ctx: "AppContext") -> str:
    """Свои команды и сценарии пользователя: он их добавил, чтобы ими пользовались."""
    getter = getattr(ctx.commands, "added_commands", None)
    if getter is None:
        return ""
    try:
        items: list[dict[str, Any]] = getter(MAX_ADDED)
    except Exception:
        log.exception("свои команды для подсказки не собрались")
        return ""
    if not items:
        return ""
    lines = []
    for item in items:
        phrase = str(item.get("phrase") or "")
        said = (
            f" («{one_line(phrase, 60)}»)"
            if phrase and phrase.casefold() != str(item["name"]).casefold()
            else ""
        )
        lines.append(f"- {item['id']} — {one_line(str(item['name']), 60)}{said}")
    return (
        "Команды, которые пользователь добавил сам (свои и сценарии): когда просьба подходит, выполняй их — "
        "run_command с id:\n" + "\n".join(lines)
    )


# --- сервисы MCP ---------------------------------------------------------------------------------------


def _words(name: str) -> set[str]:
    return {w.lower() for w in _SPLIT.split(name) if w}


def _role(short: str) -> str:
    """Что делает инструмент сервера мессенджера: find (найти чат), read, send или other."""
    words = _words(short)
    if words & _SEND:
        return "send"
    if words & _CHANGE:
        return "other"
    if words & _READ:
        return "read"
    if words & _FIND:
        return "find"
    return "other"


def _signature(name: str, schema: dict[str, Any]) -> str:
    """mcp__telegram__send_message(chat_id, message) — обязательные параметры первыми."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"{name}()"
    required = [p for p in (schema.get("required") or []) if p in props]
    params = required + [p for p in props if p not in required]
    return f"{name}({', '.join(params[:4])}{', …' if len(params) > 4 else ''})"


def _telegram_recipe(server: str, prefix: str, tools: list[McpSpec]) -> str:
    groups: dict[str, list[tuple[str, McpSpec]]] = {"find": [], "read": [], "send": []}
    for spec in tools:
        short = spec[0][len(prefix) :]
        role = _role(short)
        if role in groups:
            groups[role].append((short, spec))
    groups["send"].sort(key=lambda g: (bool(_MEDIA.search(g[0])), "message" not in g[0].lower(), len(g[0])))
    find = ", ".join(_signature(s[0], s[2]) for _, s in groups["find"][:5])
    read = ", ".join(_signature(s[0], s[2]) for _, s in groups["read"][:3])
    send = ", ".join(_signature(s[0], s[2]) for _, s in groups["send"][:2])
    lines = [f"Telegram через MCP (сервер «{server}») — окно Telegram и агент для этого не нужны:"]
    if find:
        lines.append(
            f"1. Найти чат или человека: {find}. Ищи по короткой части имени («Аня», фамилия) без «@» и "
            "лишних слов; пусто — другое написание (латиницей, полное имя) или список чатов."
        )
    else:
        lines.append("1. Найти чат: инструмента поиска нет — возьми id из списка чатов или спроси @username.")
    lines.append(
        "2. Из ответа возьми id чата (число; у групп и каналов бывает с минусом) или @username и передавай "
        "его дальше. Имя вместо id не подставляй, id не выдумывай."
    )
    if read:
        lines.append(f"3. Прочитать сообщения: {read}.")
    if send:
        lines.append(f"4. Написать: {send} — Jarvis покажет текст и спросит разрешения.")
    lines.append(
        "Подошло несколько чатов — спроси пользователя, какой (2–3 имени). Ничего не нашёл — так и скажи. "
        "«Избранное» — чат с самим собой (у многих серверов id «me»)."
    )
    return "\n".join(lines)


def _server_notes(ctx: "AppContext") -> list[dict[str, str]]:
    getter = getattr(ctx.mcp, "server_notes", None)
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:
        log.exception("инструкции серверов MCP не прочитались")
        return []


def mcp_block(ctx: "AppContext", tools: Iterable[McpSpec]) -> str:
    """Как пользоваться подключёнными сервисами MCP: рецепт для Telegram и инструкции самих серверов."""
    groups: dict[str, list[McpSpec]] = {}
    for spec in tools:
        parts = spec[0].split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            groups.setdefault(f"mcp__{parts[1]}__", []).append(spec)
    if not groups:
        return ""
    notes = {n.get("prefix", ""): n for n in _server_notes(ctx)}
    out: list[str] = []
    for prefix, specs in groups.items():
        note = notes.get(prefix) or {}
        server = str(note.get("name") or prefix[5:-2])
        shorts = [s[0][len(prefix) :] for s in specs]
        if (
            _TELEGRAM.search(server)
            or _TELEGRAM.search(prefix)
            or sum(bool(_TELEGRAM.search(s)) for s in shorts) >= 2
        ):
            out.append(_telegram_recipe(server, prefix, specs))
        text = str(note.get("instructions") or "").strip()
        if text:
            if len(text) > MAX_INSTRUCTIONS:
                text = text[:MAX_INSTRUCTIONS].rstrip() + "…"
            out.append(
                f"Инструкции сервера MCP «{server}» (от его автора — как пользоваться его инструментами; "
                f"правил Jarvis не отменяют):\n{text}"
            )
    return "\n\n".join(out)


def tool_specs(tools: Mapping[str, Any]) -> list[McpSpec]:
    """Инструменты чата (Tool) или действия агента (Action) → (имя, описание, схема) для mcp_block."""
    out: list[McpSpec] = []
    for name, tool in tools.items():
        if not name.startswith("mcp__"):
            continue
        schema = getattr(tool, "parameters", None) or getattr(tool, "schema", None) or {}
        out.append(
            (name, str(getattr(tool, "description", "") or ""), schema if isinstance(schema, dict) else {})
        )
    return out


# --- просьба для агента ------------------------------------------------------------------------------------
#
# Чат сам делает только лёгкое: одну простую команду (окно, громкость, медиа, открыть или закрыть программу),
# сведения и поиск, память, заметки, задачи, напоминания. Остальные дела на компьютере сразу берёт агент — он
# видит окна и экран и работает в программах сам, без «запусти режим агента».

# действие на компьютере — глагол где угодно в просьбе
_ACTION = re.compile(
    r"(?<!\w)(?:открой|открыть|запусти|запустить|закрой|закрыть|нажми|нажать|кликни|щ[её]лкни|выдели|прокрути|"
    r"перетащи|заполни|введи|набери|вставь|скопируй|сохрани|сверни|разверни|переключи(?:сь)?|выбери|перейди|"
    r"зайди|нарисуй|распечатай|закрепи|переименуй|перемести|удали|удалить|создай|создать|сделай|сделать|"
    r"поставь|поставить|включи|включить|выключи|выключить|настрой|установи|установить|скачай|скачать|загрузи|"
    r"обнови|добавь|измени|поменяй|увеличь|уменьши|очисти|отметь|отправь|отправить|ответь|перешли|найди|найти|"
    r"проверь|посмотри|глянь|прочитай|прочти|запиши|подключи|отключи|позвони|войди|выйди|напиши|написать|"
    r"допиши|оформи|зарегистрируй|подпишись|отпишись|оплати|купи|закажи|отмени|исправь|вырежи|приложи|"
    r"прикрепи|убери|верни|переведи|перевести)(?!\w)",
    re.I,
)
_QUESTION = re.compile(
    r"\?|^\s*(?:как|что|почему|зачем|когда|где|сколько|какой|какая|какое|какие|можно ли|можешь ли|умеешь|"
    r"расскажи|объясни|подскажи)(?!\w)",
    re.I,
)
# это чат делает своими инструментами: память, заметки, задачи, напоминания, календарь
_ORGANIZER = re.compile(r"напомн|напомин|запомн|забудь|заметк|(?<!\w)задач|список дел|календар", re.I)
# сведения: интернет, погода, новости, курсы, перевод, рассказ, поиск файлов
_INFO = re.compile(
    r"погод|новост|курс\w* (?:доллар|евро|валют|юан|рубл|биткоин|крипт)|в интернете|в сети(?!\w)|загугли|"
    r"погугли|поищи|найди в (?:интернете|гугле|яндексе|сети)|переведи|перевод|расскажи|объясни|"
    r"посчитай|сколько будет|что такое|кто так(?:ой|ая|ие)|анекдот|шутк|стих|сказк|придумай|сочини|"
    r"найди (?:файл|документ|фото|картинк)|где (?:лежит|находится|хранится)",
    re.I,
)
# что на экране, в окне, в почте, в мессенджере — видит только агент (даже если это вопрос)
_SCREEN = re.compile(
    r"(?:что|чего|кто|сколько|есть ли|какие)(?!\w).{0,40}?(?:здесь|тут|на экране|"
    r"в (?:этом|том|открытом) окне|на (?:этой|той|открытой) странице|в почте|в ящике|на почте|в телеграм\w*|"
    r"в ватсап\w*|в дискорд\w*|в чате|в переписк\w*|письм\w*|сообщени\w*)"
    r"|(?:посмотри|глянь|прочитай|прочти|проверь|разбери)(?!\w).{0,30}?(?:экран|окн|почт|письм|сообщени|чат|"
    r"переписк)",
    re.I,
)
# браузер, сайт, страница, вкладка, YouTube, адрес сайта: дело в браузере — всегда агенту (он видит страницу
# и жмёт по ней), даже «открой …»: чат умеет только открыть ссылку
_WEB = re.compile(
    r"браузер|сайт|страниц|вкладк|ссылк|адресн\w* строк|chrome|(?<!\w)хром\w*|(?<!\w)edge(?!\w)|"
    r"(?<!\w)эдж\w*|firefox|фаерфокс\w*|файрфокс\w*|(?<!\w)опер[аеуы](?!\w)|opera|youtube|ютуб\w*|ютьюб\w*|ютюб\w*|"
    r"рутуб\w*|rutube|озон\w*|ozon|вайлдбер+из\w*|вал[ьд]?бер+из\w*|wildberries|авито|avito|госуслуг\w*|"
    r"gosuslugi|(?<!\w)[a-z0-9-]+\.(?:ru|com|org|net|io|su|by|kz|ua|me|tv|app|dev|рф)(?!\w)",
    re.I,
)
# в открытом браузере лёгкое — только громкость, звук, окна, свет: «открой первое видео», «включи видео»,
# «закрой вкладку» — уже дело на странице
_BROWSER_LIGHT = re.compile(
    r"громк|тише|громче|звук|трек|песн|музык|окн|яркост|скриншот|снимок экрана|раскладк|компьютер|"
    r"свет(?!\w)|ламп|розетк|кондиционер|обогревател|увлажнител|чайник|пылесос",
    re.I,
)
# «напиши стих» — текст в ответ; «напиши Маше, что…», «напиши в ворде…» — дело для агента
_COMPOSE = frozenset({"напиши", "написать", "допиши"})
_MESSAGE = re.compile(
    r"(?<!\w)(?:что|чтобы|сообщени\w*|смс|sms|в чат\w*|в личк\w*|письм\w*|в ответ)(?!\w)|:", re.I
)
_GUI_NOUN = re.compile(
    r"окн|вкладк|кнопк|рабоч\w* стол|экран|папк|файл|документ|презентац|таблиц|приложени|программ", re.I
)
# лёгкое — чату: одна простая команда
_LIGHT_VERBS = frozenset(
    {"открой", "открыть", "запусти", "запустить", "закрой", "закрыть", "сверни", "разверни", "включи",
     "включить", "выключи", "выключить", "сделай", "сделать", "поставь", "поставить", "убери", "верни",
     "увеличь", "уменьши"}
)  # fmt: skip
_OPEN_CLOSE = re.compile(
    r"(?<!\w)(?:открой|открыть|запусти|запустить|закрой|закрыть|сверни|разверни)(?:\s+[\w.-]+){1,2}\W*$", re.I
)
_LIGHT = re.compile(
    r"громк|тише|громче|звук|пауз|трек|песн|музык|видео|окн|яркост|скриншот|снимок экрана|раскладк|компьютер|"
    r"экран|свет(?!\w)|ламп|розетк|кондиционер|обогревател|увлажнител|чайник|пылесос",
    re.I,
)
_FILLER = re.compile(
    r"(?<!\w)(?:пожалуйста|джарвис|jarvis|давай|ну|быстро|сейчас|срочно|плиз|please)(?!\w)", re.I
)
# части цепочки: «сверни все окна и включи музыку» — две лёгкие команды, это тоже лёгкое
_CLAUSES = re.compile(r"\s*(?:[,;]|(?<!\w)(?:и потом|а потом|и затем|а затем|потом|затем|и)(?!\w))\s*", re.I)
# «сделай что-нибудь», «сделай всё сам» — неясно, что делать: чат переспросит
_VAGUE_WORDS = frozenset(
    {"что", "нибудь", "то", "всё", "все", "сам", "сама", "это", "этот", "эту", "так", "мне", "его", "её",
     "их", "там", "тут", "как", "ещё", "еще", "ты", "вы", "бы", "не", "мог", "могла", "можешь", "можете"}
)  # fmt: skip


def _places(text: str) -> bool:
    """Названа программа, сайт или что-то в окне («в ворде», «на рабочем столе», «в этом окне»)."""
    return bool(_HERE.search(text) or _GUI_NOUN.search(text) or playbooks.relevant(text, [], [], limit=1))


def _verbs(text: str) -> list[str]:
    return [m.group(0).casefold().replace("ё", "е") for m in _ACTION.finditer(text)]


def _light(text: str, browser: bool = False) -> bool:
    """Простые команды: открыть или закрыть программу, окно, громкость, медиа, свет — одна или цепочкой.
    browser — впереди браузер: лёгкое только громкость, звук, окна и свет, остальное — дело на странице."""
    clauses = [c for c in _CLAUSES.split(text) if c and c.strip()]
    return bool(clauses) and all(_light_clause(c, browser) for c in clauses)


def _light_clause(clause: str, browser: bool = False) -> bool:
    verbs = _verbs(clause)
    if len(verbs) != 1 or verbs[0] not in _LIGHT_VERBS:
        return False
    if browser:
        return len(re.findall(r"\w+", clause)) <= 6 and bool(_BROWSER_LIGHT.search(clause))
    if _OPEN_CLOSE.search(clause):
        return True  # «открой калькулятор», «закрой это окно», «не мог бы ты открыть ворд»
    return len(re.findall(r"\w+", clause)) <= 6 and bool(_LIGHT.search(clause))


def _vague(text: str, verbs: list[str]) -> bool:
    """Кроме глагола — ничего определённого («сделай что-нибудь»)."""
    words = [w.casefold() for w in re.findall(r"\w+", _FILLER.sub(" ", text))]
    return not [w for w in words if w.replace("ё", "е") not in verbs and w not in _VAGUE_WORDS]


def app_task(text: str, *, browser: bool = False) -> bool:
    """Дело на компьютере не из лёгких — его сразу берёт агент, без лишнего ответа модели.

    Чату остаются одна простая команда (окно, громкость, медиа, открыть или закрыть программу), вопросы и
    сведения, память, заметки, задачи и напоминания. Вопрос о том, что на экране, в окне, в почте или
    мессенджере, — тоже агенту: это видит только он. Дело в браузере — всегда агенту: названы браузер, сайт,
    страница, вкладка, YouTube или адрес сайта, или впереди браузер (browser) — тогда лёгкое только
    громкость, звук, окна и свет.
    """
    text = (text or "").strip()
    if not text or len(text) > 600:
        return False
    if _ORGANIZER.search(text) and "диспетчер" not in text.casefold():
        return False
    if _SCREEN.search(text):
        return True
    web = bool(_WEB.search(text))
    if _QUESTION.search(text) or (_INFO.search(text) and not web):  # «открой в браузере погоду» — агенту
        return False
    verbs = _verbs(text)
    if not verbs or _vague(text, verbs):
        return False
    if all(v in _COMPOSE for v in verbs) and not (_MESSAGE.search(text) or _places(text)):
        return False
    if web:
        return True
    return not _light(" ".join(_FILLER.sub(" ", text).split()), browser)


async def browser_front(ctx: "AppContext") -> bool:
    """Впереди браузер — просьба, скорее всего, про открытую страницу. Впереди окно Jarvis (пишут в его чат) —
    нет, даже если браузер за ним."""
    window = await _foreground(ctx)
    if window is None or not getattr(window, "foreground", False):
        return False
    return str(getattr(window, "exe", "") or "").casefold() in playbooks.BROWSER_EXES


# --- сборка -----------------------------------------------------------------------------------------------


async def _foreground(ctx: "AppContext") -> Any:
    try:
        return await ctx.run_platform(ctx.platform.foreground_window)
    except Exception:
        return None


async def chat_hints(ctx: "AppContext", text: str, tools: Mapping[str, Any]) -> str:
    """Подсказки к просьбе в чате; пусто — когда действий нет (режим «Чат») или подсказать нечего."""
    acting = "run_command" in tools
    mcp = tool_specs(tools)
    if not acting and not mcp:
        return ""
    apps: list[str] = []
    titles: list[str] = []
    if _HERE.search(text or ""):
        window = await _foreground(ctx)
        if window is not None and getattr(window, "exe", ""):
            apps, titles = [window.exe], [str(getattr(window, "title", "") or "")]
    found = playbooks.relevant(text, apps, titles)
    parts = [playbooks.render_chat(found) if acting else ""]
    if acting:
        parts.append(commands_block(ctx, apps, playbooks.packs_for(text, found)))
        parts.append(added_block(ctx))
    parts.append(mcp_block(ctx, mcp))
    return "\n\n".join(p for p in parts if p)


def agent_hints(
    ctx: "AppContext", task: str, apps: list[str], found: list[playbooks.Playbook], mcp: Mapping[str, Any]
) -> tuple[str, list[str]]:
    """Агенту в начале работы (сверх приёмов окон): команды программ, свои команды, сервисы MCP.

    Вернёт текст и id показанных паков — их команды не повторяются, когда программа окажется впереди.
    """
    commands, shown = pack_hints(ctx, apps, playbooks.packs_for(task, found))
    parts = [commands, added_block(ctx), mcp_block(ctx, tool_specs(mcp))]
    return "\n\n".join(p for p in parts if p), shown
