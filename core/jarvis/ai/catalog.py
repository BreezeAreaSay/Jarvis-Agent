"""Каталог инструментов Jarvis AI для человека: что ИИ может делать, где, с каким риском и с каким разрешением.

describe(ctx) собирает список из ToolRegistry чата (встроенные, плагины, подключённые серверы MCP) и действий
агента (AGENT_ACTIONS). Риск — jarvis.ai.policy.tool_risk, итоговое решение — jarvis.ai.policy.decision с
учётом выбора человека в settings.ai.tool_policy (ключ — policy_key; у действий агента — «agent.<имя>», чтобы
«нажать клавиши» в чате и у агента настраивались отдельно). Каталог только описывает: проверяют разрешения
чат (Ai._call_tool) и агент (Agent._act).
"""

import logging
from typing import TYPE_CHECKING, Any

from jarvis.ai import policy

if TYPE_CHECKING:
    from jarvis.ai.tools import Tool
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

AGENT_PREFIX = "agent."

GROUPS: dict[str, str] = {
    "web": "Интернет и сведения",
    "memory": "Память, заметки и дела",
    "computer": "Компьютер",
    "agent": "Агент: работа в окнах программ",
}

# встроенные инструменты чата: имя → (название, что делает — простыми словами, группа)
BUILTIN_INFO: dict[str, tuple[str, str, str]] = {
    "web_search": ("Поиск в интернете", "Ищет свежую информацию; ответ — со ссылками на источники", "web"),
    "fetch_page": ("Чтение страниц", "Открывает найденную страницу и читает её текст (только чтение)", "web"),
    "get_weather": ("Погода", "Погода сейчас и прогноз для вашего города", "web"),
    "get_news": ("Новости", "Свежие новости по теме", "web"),
    "get_rates": ("Курсы валют", "Курсы доллара, евро, юаня и других валют", "web"),
    "list_commands": ("Список команд", "Подбирает подходящую команду Jarvis", "web"),
    "remember": ("Запомнить", "Записывает факт о вас в память — когда вы просите «запомни»", "memory"),
    "forget": ("Забыть", "Удаляет факт из памяти по вашей просьбе", "memory"),
    "list_memory": ("Что я помню", "Читает, что записано в памяти", "memory"),
    "add_note": ("Новая заметка", "Создаёт заметку", "memory"),
    "list_notes": ("Поиск заметок", "Находит и читает ваши заметки", "memory"),
    "add_task": ("Новая задача", "Добавляет дело в список задач", "memory"),
    "complete_task": ("Отметить задачу", "Отмечает задачу выполненной", "memory"),
    "list_tasks": ("Список дел", "Читает список задач", "memory"),
    "add_event": ("Событие в календарь", "Добавляет встречу, созвон или день рождения", "memory"),
    "list_events": ("Календарь", "Читает события календаря", "memory"),
    "set_reminder": ("Напоминание", "Ставит напоминание на время или через N минут", "memory"),
    "run_command": (
        "Команды Jarvis",
        "Выполняет команды, как голосом: громкость, окна, музыка, сайты. Опасные команды спросят сами",
        "computer",
    ),
    "open_desk": ("Столы", "Показывает стол Jarvis с виджетами", "computer"),
    "launch_app": ("Запуск программ", "Открывает программу из меню «Пуск»", "computer"),
    "open_url": ("Открыть сайт", "Открывает ссылку в браузере", "computer"),
    "set_volume": ("Громкость", "Ставит громкость звука", "computer"),
    "media_control": ("Музыка и видео", "Пауза, следующий и предыдущий трек", "computer"),
    "type_text": ("Ввод текста", "Печатает текст в окне, которое сейчас активно", "computer"),
    "press_keys": ("Сочетания клавиш", "Нажимает клавиши в активном окне: «Ctrl+C», «Enter»…", "computer"),
    "run_agent": ("Агент", "Запускает агента для многошаговой задачи в окнах программ", "computer"),
}

# действия агента («работает сам»): что он умеет в окнах — для каталога и разрешений «agent.<имя>»
AGENT_ACTIONS: list[dict[str, Any]] = [
    {"name": "ui_snapshot", "title": "Смотреть окно", "risk": "read",
     "description": "Читает список кнопок, полей и текста в окне программы"},
    {"name": "screenshot", "title": "Снимок окна", "risk": "read",
     "description": "Делает снимок окна, с которым работает, — если модель понимает картинки"},
    {"name": "focus_window", "title": "Переключать окна", "risk": "act",
     "description": "Выводит нужную программу на передний план"},
    {"name": "ui_click", "title": "Нажимать кнопки", "risk": "act",
     "description": "Нажимает кнопки и пункты меню по названию. «Отправить», «Удалить», «Оплатить» — только с разрешения"},
    {"name": "click_point", "title": "Щелчок мышью", "risk": "act",
     "description": "Щёлкает в точке окна, когда у кнопки нет названия"},
    {"name": "ui_type", "title": "Заполнять поля", "risk": "act",
     "description": "Вводит текст в поле по названию. Пароли и платёжные данные — только с разрешения"},
    {"name": "type_text", "title": "Печатать текст", "risk": "act",
     "description": "Печатает текст туда, где стоит курсор"},
    {"name": "scroll", "title": "Прокрутка", "risk": "act", "description": "Прокручивает окно или список"},
    {"name": "press_keys", "title": "Сочетания клавиш", "risk": "act",
     "description": "Нажимает клавиши в окне программы. Опасные сочетания — только с разрешения"},
    {"name": "launch_app", "title": "Запуск программ", "risk": "act",
     "description": "Открывает программу, если её ещё нет"},
    {"name": "open_url", "title": "Открыть сайт", "risk": "act", "description": "Открывает ссылку в браузере"},
    {"name": "run_command", "title": "Команды Jarvis", "risk": "act",
     "description": "Выполняет команду Jarvis, как голосом"},
    {"name": "wait", "title": "Подождать", "risk": "read", "description": "Ждёт, пока окно догрузится"},
    {"name": "finish", "title": "Отчёт", "risk": "read", "description": "Заканчивает задачу и пишет, что сделано"},
]  # fmt: skip


def _modes(tool: "Tool") -> list[str]:
    return ["chat", "all", "agent"] if tool.in_chat_mode else ["all", "agent"]


def _override(ai: Any, key: str) -> str | None:
    value = (getattr(ai, "tool_policy", None) or {}).get(key)
    return value if value in ("allow", "ask", "deny") else None


def _entry(ai: Any, key: str, risk: policy.Risk, **fields: Any) -> dict[str, Any]:
    default: policy.Decision = "ask" if risk == "risky" else "allow"
    return {
        **fields,
        "policy_key": key,
        "risk": risk,
        "risk_title": policy.RISK_TITLES[risk],
        "default": default,
        "override": _override(ai, key),
        "decision": policy.decision(ai, key, risk),
    }


def _available(ai: Any, tool: "Tool") -> bool:
    if tool.needs == "web":
        return bool(ai.web_search)
    if tool.needs == "memory":
        return bool(ai.memory)
    return True


def describe(ctx: "AppContext") -> dict[str, Any]:
    """{items, groups, agent_confirm}: все инструменты, которые ИИ может вызвать, и действия агента."""
    from jarvis.ai.tools import ToolRegistry, mcp_tool

    ai = ctx.settings.value.ai
    items: list[dict[str, Any]] = []
    groups = dict(GROUPS)
    registry = getattr(ctx.ai, "tools", None)
    if isinstance(registry, ToolRegistry):
        for tool in registry.builtin.values():
            title, plain, group = BUILTIN_INFO.get(tool.name, (tool.name, tool.description, "computer"))
            items.append(
                _entry(
                    ai,
                    tool.name,
                    policy.tool_risk(tool.name),
                    name=tool.name,
                    title=title,
                    description=plain,
                    source="builtin",
                    group=group,
                    modes=_modes(tool),
                    available=_available(ai, tool),
                    timeout=tool.timeout,
                )
            )
        for provider, tools in _plugin_tools(registry):
            group = f"plugin:{provider}"
            groups.setdefault(group, f"Плагин «{provider}»")
            for tool in tools:
                items.append(
                    _entry(
                        ai,
                        tool.name,
                        policy.tool_risk(tool.name, tool.description),
                        name=tool.name,
                        title=tool.title_for({}),
                        description=tool.description,
                        source=group,
                        group=group,
                        modes=_modes(tool),
                        available=True,
                        timeout=tool.timeout,
                    )
                )
    try:
        specs = ctx.mcp.tools()
    except Exception:
        log.exception("инструменты MCP для каталога не получены")
        specs = []
    for spec in specs:
        tool = mcp_tool(ctx, spec)
        if tool is None:
            continue
        full = tool.title_for({})
        server, _, short = full.partition(": ")
        if not short:
            server, short = "сервер MCP", full
        group = f"mcp:{server}"
        groups.setdefault(group, f"MCP: {server}")
        items.append(
            _entry(
                ai,
                tool.name,
                policy.tool_risk(tool.name, tool.description),
                name=tool.name,
                title=short,
                description=tool.description,
                source=group,
                group=group,
                modes=["all", "agent"],
                available=True,
                timeout=tool.timeout,
            )
        )
    for action in AGENT_ACTIONS:
        key = AGENT_PREFIX + action["name"]
        items.append(
            _entry(
                ai,
                key,
                action["risk"],
                name=action["name"],
                title=action["title"],
                description=action["description"],
                source="agent",
                group="agent",
                modes=["agent"],
                available=True,
                timeout=None,
            )
        )
    order = list(groups)
    items.sort(key=lambda i: order.index(i["group"]) if i["group"] in order else len(order))
    return {
        "items": items,
        "groups": [{"id": gid, "title": title} for gid, title in groups.items()],
        "agent_confirm": ai.agent_confirm,
    }


def _plugin_tools(registry: Any) -> list[tuple[str, list["Tool"]]]:
    out: list[tuple[str, list[Tool]]] = []
    for name, provider in list(getattr(registry, "_providers", {}).items()):
        try:
            tools = [t for t in provider() or [] if getattr(t, "name", "") not in registry.builtin]
        except Exception:
            log.exception("инструменты «%s» для каталога не получены", name)
            continue
        if tools:
            out.append((name, tools))
    return out
