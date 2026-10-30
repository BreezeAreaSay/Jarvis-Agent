"""Политика действий ИИ: насколько рискован инструмент и что с ним делать — выполнить, спросить или отказать.

Один источник правды для чата (Ai._call_tool), агента (Agent._act) и каталога инструментов в настройках:
человек может для любого инструмента выбрать «Разрешено», «Спрашивать» или «Запрещено» (ai.tool_policy);
без выбора действует правило по риску — рискованное (отправка, удаление, оплата, запуск кода) спрашивается.
"""

import contextlib
import re
import unicodedata
from typing import Any, Literal

Risk = Literal["read", "act", "risky"]
Decision = Literal["allow", "ask", "deny"]

# встроенные инструменты чата: read — только читает; act — меняет что-то у человека; risky — всегда спросить
BUILTIN_RISK: dict[str, Risk] = {
    "list_commands": "read",
    "web_search": "read",
    "fetch_page": "read",
    "get_weather": "read",
    "get_news": "read",
    "get_rates": "read",
    "list_memory": "read",
    "list_notes": "read",
    "list_tasks": "read",
    "list_events": "read",
    "remember": "act",
    "forget": "act",
    "add_note": "act",
    "add_task": "act",
    "complete_task": "act",
    "add_event": "act",
    "set_reminder": "act",
    "open_desk": "act",
    "set_volume": "act",
    "media_control": "act",
    "run_command": "act",
    "launch_app": "act",
    "open_url": "act",
    "type_text": "act",
    "press_keys": "act",
    "run_agent": "act",
    "start_background_task": "act",
}

# слова в названии инструмента MCP (send_message, deleteChat, get-history…): глагол риска где угодно в имени
_RISKY_VERBS = {
    "send", "publish", "reply", "forward", "tweet", "repost",
    "delete", "remove", "erase", "purge", "wipe", "drop", "destroy", "trash",
    "kick", "ban", "leave", "unsubscribe",
    "pay", "buy", "purchase", "checkout", "transfer", "withdraw", "donate",
    "exec", "execute", "eval", "spawn", "shell", "upload", "overwrite", "invite", "approve", "deploy",
}  # fmt: skip
# бывают и существительным (get_post, list_orders): рискованны, только когда имя с них начинается
_RISKY_FIRST = {"post", "comment", "share", "react", "clear", "run", "order", "block"}
_READ_WORDS = {
    "get", "list", "search", "find", "read", "fetch", "query", "show", "view", "describe", "lookup",
    "status", "info", "history", "resolve", "check", "count", "inspect", "browse", "preview", "download",
    "me", "whoami", "unread", "recent", "dialogs", "chats", "contacts", "members", "participants",
}  # fmt: skip
_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def mcp_short_name(name: str) -> str:
    """mcp__telegram__send_message → send_message."""
    return name.split("__", 2)[2] if name.startswith("mcp__") and name.count("__") >= 2 else name


def _words(name: str) -> list[str]:
    return [w.lower() for w in _SPLIT_RE.split(mcp_short_name(name)) if w]


def tool_risk(name: str, description: str = "") -> Risk:
    """Риск инструмента по имени: для встроенных — таблица, для MCP и плагинов — по словам в имени."""
    if name in BUILTIN_RISK:
        return BUILTIN_RISK[name]
    words = _words(name)
    if any(w in _RISKY_VERBS for w in words) or (words and words[0] in _RISKY_FIRST):
        return "risky"
    if words and words[0] in _READ_WORDS:
        return "read"
    if words and all(w in _READ_WORDS for w in words):
        return "read"
    return "act"


def decision(ai: Any, name: str, risk: Risk) -> Decision:
    """Что делать с вызовом: выбор человека в ai.tool_policy, иначе — по риску."""
    chosen = (getattr(ai, "tool_policy", None) or {}).get(name)
    if chosen in ("allow", "ask", "deny"):
        return chosen
    return "ask" if risk == "risky" else "allow"


RISK_TITLES: dict[Risk, str] = {
    "read": "Только читает",
    "act": "Выполняет действия",
    "risky": "Рискованное — спрашивает",
}


# --- клавиши, окна и текст: общие правила для агента, инструментов чата и команд от ИИ -------------------

_KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "return": "enter",
    "del": "delete",
    "esc": "escape",
    "windows": "win",
    "cmd": "win",
    "super": "win",
    "option": "alt",
}
RISKY_KEYS = frozenset(
    {"shift+delete", "ctrl+shift+delete", "win+r", "win+x", "ctrl+enter", "alt+s", "ctrl+alt+delete"}
)
SEND_KEYS = frozenset({"enter", "ctrl+enter", "alt+s"})
CLOSE_KEYS = frozenset({"alt+f4", "ctrl+w", "ctrl+f4", "ctrl+q"})

SHELL_EXES = frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "openconsole.exe",
     "conhost.exe", "bash.exe", "wsl.exe", "mintty.exe", "regedit.exe", "mmc.exe"}
)  # fmt: skip
SHELL_TITLE = re.compile(
    r"командная строка|command prompt|powershell|windows terminal|терминал|\bterminal\b|"
    r"^выполнить$|^run$|редактор реестра|registry editor",
    re.I,
)
MESSENGER_EXES = frozenset(
    {"telegram.exe", "ayugram.exe", "discord.exe", "whatsapp.exe", "whatsapp.root.exe", "slack.exe",
     "viber.exe", "skype.exe", "ms-teams.exe", "teams.exe", "signal.exe", "element.exe", "zoom.exe",
     "hxoutlook.exe", "outlook.exe", "olk.exe", "thunderbird.exe", "vk messenger.exe", "max.exe"}
)  # fmt: skip
MESSENGER_TITLE = re.compile(
    r"telegram|whatsapp|discord|slack|viber|skype|teams|signal|messenger|вконтакте|\bvk\b|gmail|outlook|"
    r"почта|\bmail\b|instagram|facebook|twitter|\bx\.com\b|одноклассники",
    re.I,
)
CARD_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")


def normalize_combo(combo: str) -> str:
    """«Ctrl + Shift + Del» → «ctrl+shift+delete»."""
    parts = [p.strip().casefold() for p in (combo or "").replace(" ", "").split("+") if p.strip()]
    return "+".join(_KEY_ALIASES.get(p, p) for p in parts)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    """В тексте номер банковской карты (13–19 цифр, проходит проверку Луна)."""
    for match in CARD_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def is_shell_window(title: str, exe: str = "") -> bool:
    """Окно, где набранный текст выполняется как команда: терминал, «Выполнить», редактор реестра."""
    return (exe or "").casefold() in SHELL_EXES or bool(SHELL_TITLE.search((title or "").strip()))


def is_messenger(title: str, exe: str = "") -> bool:
    """Окно, где Enter отправляет написанное людям: мессенджеры и почта (и их веб-версии в браузере)."""
    return (exe or "").casefold() in MESSENGER_EXES or bool(MESSENGER_TITLE.search(title or ""))


_HOTKEY_RE = re.compile(r"^(?:(?:ctrl|alt|shift|win|cmd|control|super)\+)+[^+\s]+$", re.I)


def jarvis_hotkeys(settings: Any) -> frozenset[str]:
    """Горячие клавиши самого Jarvis (окно, «слушать», микрофон, плагины): ИИ их не нажимает."""
    out: set[str] = set()
    system = getattr(settings, "system", None)
    for name in ("hotkey_show", "hotkey_listen", "hotkey_mic"):
        value = str(getattr(system, name, "") or "")
        if value:
            out.add(normalize_combo(value))
    plugins = getattr(settings, "plugins", None) or {}
    for state in plugins.values() if isinstance(plugins, dict) else []:
        config = state.get("config") if isinstance(state, dict) else getattr(state, "config", None)
        for value in config.values() if isinstance(config, dict) else []:
            if isinstance(value, str) and _HOTKEY_RE.match(value.replace(" ", "")):
                out.add(normalize_combo(value))
    return frozenset(out)


# сайты, куда данные в адресе (поисковый запрос) уходят по делу: ссылки на них открываются без вопросов
SEARCH_HOSTS = re.compile(
    r"(^|\.)(google\.[a-z.]+|yandex\.[a-z]+|ya\.ru|bing\.com|duckduckgo\.com|youtube\.com|wikipedia\.org|"
    r"maps\.google\.[a-z.]+|2gis\.ru|ozon\.ru|wildberries\.ru|market\.yandex\.ru|github\.com|avito\.ru|"
    r"kinopoisk\.ru|rutube\.ru|twitch\.tv|vk\.com|soundcloud\.com|aliexpress\.(?:ru|com)|"
    r"store\.steampowered\.com)$",
    re.I,
)
# адреса программ, которые ИИ может открывать (кроме http и https): открывают нужное место в программе,
# но сами ничего не отправляют и не запускают (tg://resolve — чат, whatsapp://send — чат с текстом в поле)
APP_SCHEMES = ("tg://", "ms-settings:", "spotify:", "mailto:", "whatsapp://")


# русские буквы, неотличимые от латинских (набор как в Chrome): «аррӏе.com» из них — подделка под apple.com
_LATIN_ALIKE = frozenset("асԁеһіјӏорԛѕԝхуъьҽпгѵѡ")
# алфавиты, буквы которых путаются с латинскими: их смесь в одном имени — признак подделки (японское или
# китайское имя с латиницей — обычное дело)
_ALIKE_SCRIPTS = frozenset({"LATIN", "CYRILLIC", "GREEK", "ARMENIAN"})
# зоны, где имя сайта из одних русских букв — обычное дело
_CYRILLIC_ZONES = frozenset({"ru", "su", "by", "kz", "ua", "bg", "rs", "mk", "mn", "kg", "tj", "uz"})


def _script(ch: str) -> str:
    return unicodedata.name(ch, "?").split(" ", 1)[0]


def unicode_host(host: str) -> str:
    """Имя сайта буквами: «xn--e1afmkfd.xn--p1ai» → «пример.рф» (то, что человек видит в адресе)."""
    labels = []
    for label in host.casefold().split("."):
        if label.startswith("xn--"):
            with contextlib.suppress(UnicodeError, ValueError):
                label = label[4:].encode("ascii").decode("punycode")
        labels.append(label)
    return ".".join(labels)


def ascii_host(host: str) -> str:
    """Настоящее имя сайта, которое видит браузер: «пример.рф» → «xn--e1afmkfd.xn--p1ai» (punycode)."""
    labels = []
    for label in unicode_host(host).split("."):
        if not label.isascii():
            try:
                label = label.encode("idna").decode("ascii")
            except UnicodeError:
                label = "xn--" + label.encode("punycode").decode("ascii")
        labels.append(label)
    return ".".join(labels)


def lookalike_host(host: str) -> bool:
    """Имя сайта похоже на подделку: в одной части буквы разных алфавитов («gооgle.com» с русскими «о») или
    часть целиком из русских двойников латинских букв не в русской зоне («аррӏе.com»)."""
    labels = [label for label in unicode_host(host).split(".") if label]
    if not labels or all(label.isascii() for label in labels):
        return False
    zone = labels[-1]
    for i, label in enumerate(labels):
        letters = [ch for ch in label if ch.isalpha()]
        scripts = {_script(ch) for ch in letters}
        if len(scripts & _ALIKE_SCRIPTS) > 1:
            return True
        last = i == len(labels) - 1 and len(labels) > 1
        if (
            not last
            and scripts == {"CYRILLIC"}
            and zone.isascii()
            and zone not in _CYRILLIC_ZONES
            and all(ch in _LATIN_ALIKE for ch in letters)
        ):
            return True
    return False


def link_note(host: str) -> str:
    """Что сказать об имени сайта в вопросе «Открыть …?»: для имён не латиницей — настоящее имя (punycode),
    для похожих на подделку — предупреждение; обычное имя — ""."""
    shown = unicode_host(host)
    if shown.isascii():
        return ""
    real = ascii_host(shown)
    if lookalike_host(shown):
        return (
            f"Осторожно: имя сайта «{shown}» похоже на подделку — в нём буквы другого алфавита, похожие "
            f"на латинские. Настоящее имя — {real}."
        )
    return f"Настоящее имя сайта — {real}."
