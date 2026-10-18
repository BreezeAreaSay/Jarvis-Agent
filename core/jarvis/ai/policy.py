"""Политика действий ИИ: насколько рискован инструмент и что с ним делать — выполнить, спросить или отказать.

Один источник правды для чата (Ai._call_tool), агента (Agent._act) и каталога инструментов в настройках:
человек может для любого инструмента выбрать «Разрешено», «Спрашивать» или «Запрещено» (ai.tool_policy);
без выбора действует правило по риску — рискованное (отправка, удаление, оплата, запуск кода) спрашивается.
"""

import re
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
