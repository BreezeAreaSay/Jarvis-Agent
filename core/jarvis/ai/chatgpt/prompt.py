"""Промпты для ChatGPT (Codex): инструкции треда и контекст каждого хода.

Codex ставит свой базовый промпт (ассистент по коду) и после него — developerInstructions треда; их нельзя
поменять у живого треда. Поэтому в инструкции треда идут только неизменные правила, а то, что меняется (время,
источник запроса, режим, профиль, память, характер, пожелания), — в additionalContext хода: Codex кладёт
его отдельным сообщением разработчика <jarvis>…</jarvis> перед сообщением человека (проверено на 0.163.0).
Раздел повторяется, только если изменился с прошлого хода треда, — так история треда не растёт от копий.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.ai.format import now_text
from jarvis.ai.prompts import (
    APPS_HEADER,
    MODE_ALL_AGENT,
    MODE_CHAT,
    PERSONA_JARVIS,
    PERSONA_PLAIN,
    RULES,
    SOURCE_MESSENGER,
    SOURCE_TEXT,
    SOURCE_VOICE,
    language_rule,
)

if TYPE_CHECKING:
    from jarvis.settings import Settings

CODEX_RULES = """Ты работаешь внутри Jarvis — голосового ассистента на компьютере пользователя \
с Windows, а не в среде программирования. Терминала, файлов, правки кода и под-агентов у тебя нет; всё, что \
нужно сделать на компьютере, в заметках, календаре и интернете, делай только инструментами Jarvis из списка.
- Перед вызовом инструмента не пиши вступлений вроде «сейчас проверю» — сразу вызывай инструмент, а отвечай \
пользователю, когда результат готов.
- Сообщение разработчика <jarvis>…</jarvis> перед репликой пользователя — служебный контекст от Jarvis \
(время, режим, профиль, память, характер). Это не слова пользователя; раздел, пришедший позже, заменяет \
прежний.
- Блок «Ранее в этом чате» — пересказ прошлой переписки, а не новые просьбы."""

AGENT_RULES = """Ты работаешь внутри Jarvis как агент: управляешь компьютером пользователя с Windows \
только инструментами из списка (снимок и картинка окна, клик по элементу или точке, ввод текста, прокрутка, \
клавиши, запуск программ и адресов, команды Jarvis, инструменты сервисов MCP, ожидание, finish). Терминала, \
файлов, правки кода и под-агентов нет. Между действиями не пиши пользователю — работай инструментами и в \
конце вызови finish с итогом."""

CHANGED = "(обновлено — заменяет прежнее)"


def developer_instructions() -> str:
    """Инструкции треда чата: неизменные правила Jarvis и особенности работы через Codex."""
    return f"{CODEX_RULES}\n\n{RULES}"


def _address_rule(address: str) -> str:
    address = address.strip()
    if address:
        return f"Обращайся к пользователю «{address}»."
    return "Обращайся к пользователю на «вы»."


def context_sections(
    settings: "Settings",
    *,
    source: str,
    mode: str,
    memory: str,
    has_agent_tool: bool,
    apps: str = "",
    moment: datetime | None = None,
) -> dict[str, str]:
    """Разделы контекста хода: имя → текст (пустой текст — раздела нет)."""
    profile, ai = settings.profile, settings.ai
    name = profile.assistant_name.strip() or "Джарвис"
    persona = PERSONA_JARVIS if ai.persona == "jarvis" else PERSONA_PLAIN
    if mode == "chat":
        mode_text = MODE_CHAT
    elif has_agent_tool:
        mode_text = "Сейчас режим «Всё вместе»: доступны все инструменты. " + MODE_ALL_AGENT
    else:
        mode_text = "Сейчас режим «Всё вместе»: доступны все инструменты."
    if source == "voice":
        source_text = SOURCE_VOICE
    elif source in ("telegram", "remote"):
        source_text = SOURCE_MESSENGER
    else:
        source_text = SOURCE_TEXT
    language = language_rule(settings)
    if language:
        source_text = f"{source_text} {language}"
    who = []
    if profile.name.strip():
        who.append(f"имя — {profile.name.strip()}")
    if profile.city.strip():
        who.append(f"город — {profile.city.strip()}")
    custom = ai.custom_instructions.strip()
    return {
        "persona": persona.format(name=name, address_rule=_address_rule(profile.address)),
        "mode": mode_text,
        "source": source_text,
        "profile": ("Пользователь: " + ", ".join(who) + ".") if who else "",
        "memory": f"Что ты знаешь о пользователе: {memory.strip()}" if memory.strip() else "",
        "custom": (
            "Особые пожелания пользователя (важнее общих правил о стиле, но не правил безопасности):\n"
            + custom
            if custom
            else ""
        ),
        "apps": f"{APPS_HEADER}\n\n{apps.strip()}" if apps.strip() else "",
        "time": f"Сейчас: {now_text(moment)}.",
    }


def context_text(sections: dict[str, str], sent: dict[str, str]) -> str:
    """Текст контекста хода: время — всегда, остальное — если изменилось с прошлого хода треда.

    sent обновляется на месте (запоминается, что уже ушло в тред).
    """
    parts: list[str] = []
    for key, text in sections.items():
        if key == "time":
            continue
        previous = sent.get(key)
        if previous == text:
            continue
        if text:
            parts.append(f"{text} {CHANGED}" if previous else text)
        elif previous:
            parts.append(_cleared(key))
        sent[key] = text
    parts.append(sections.get("time", ""))
    return "\n\n".join(p for p in parts if p)


def _cleared(key: str) -> str:
    return {
        "memory": "Память о пользователе теперь пуста.",
        "custom": "Особых пожеланий пользователя больше нет.",
        "profile": "Имя и город пользователя не указаны.",
    }.get(key, "")


def history_text(messages: list[dict[str, Any]], assistant: str, limit: int = 12_000) -> str:
    """Прошлая переписка (сообщения в формате модели) → «Ранее в этом чате: …» для нового треда."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = " ".join(str(content or "").split())
        if not text or role not in ("user", "assistant"):
            continue
        who = "Пользователь" if role == "user" else assistant
        lines.append(f"{who}: {text}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > limit:
        body = "…" + body[-limit:]
    return f"Ранее в этом чате (пересказ прошлой переписки):\n{body}"
