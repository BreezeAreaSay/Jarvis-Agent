"""Инструменты фоновой задачи: поиск и чтение страниц, хранилище заметок, план и ход работы, вопрос человеку,
инструменты серверов MCP. Браузер агента добавляет свои (jarvis.ai.browser.task_tools).

Экрана, мыши, клавиатуры, команд Jarvis и запуска программ здесь нет: задача работает в фоне и не мешает
человеку. Всё, что пришло извне (страницы, поиск, заметки, ответы MCP), отдаётся модели в обёртке «данные, а
не инструкции» (untrusted).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.ai.agent import Outcome
from jarvis.ai.format import one_line
from jarvis.ai.policy import decision, tool_risk
from jarvis.ai.tools import mcp_tool, untrusted
from jarvis.ai.web import check_public_url, site_name
from jarvis.background import measure
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.background.runner import Session

log = logging.getLogger(__name__)

READ_CHUNK = 12_000  # знаков страницы за раз (web_read, part)
MAX_MCP_RESULT = 8000
SEARCH_RESULTS = 6

Run = Callable[["Session", dict[str, Any]], Awaitable[Outcome]]


@dataclass
class TaskTool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    run: Run
    title: Callable[[dict[str, Any]], str]  # «Ищу: …» — в ход работы и на карточку
    timeout: float = 120.0
    risk: str = "read"  # read — только смотрит; act — меняет что-то (заметку, сайт)
    schema: dict[str, Any] | None = None  # готовая схема параметров (инструменты MCP)

    def spec(self) -> dict[str, Any]:
        parameters = self.schema or {
            "type": "object",
            "properties": self.properties,
            "required": self.required,
        }
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": parameters},
        }


def _s(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _arg(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return "" if value is None else str(value).strip()


def _int(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(args.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


# --- интернет ---------------------------------------------------------------------------------------------


async def _web_search(s: "Session", args: dict[str, Any]) -> Outcome:
    query = _arg(args, "query")
    if not query:
        return Outcome(False, "Ошибка: пустой запрос")
    results = await s.ctx.ai.web.search(query, _int(args, "max_results", SEARCH_RESULTS, 1, 10))
    if not results:
        return Outcome(True, "Ничего не найдено — попробуй другие слова или язык запроса.", f"Поиск: {query}")
    return Outcome(True, untrusted("Результаты поиска", results), f"Поиск: {query} — {len(results)}")


async def _web_read(s: "Session", args: dict[str, Any]) -> Outcome:
    url = check_public_url(_arg(args, "url"))
    part = _int(args, "part", 1, 1, 50)
    page = await s.ctx.ai.web.fetch_page(url, limit=part * READ_CHUNK)
    text = str(page.get("text") or "")
    chunk = text[(part - 1) * READ_CHUNK : part * READ_CHUNK]
    more = bool(page.get("truncated")) or len(text) > part * READ_CHUNK
    if not chunk:
        return Outcome(
            False, f"Ошибка: на странице нет части {part}" if part > 1 else "Ошибка: страница пустая"
        )
    s.task.add_source(str(page.get("title") or site_name(page["url"])), str(page["url"]))
    tail = f"\n(Это часть {part}. Дальше — web_read с part={part + 1}.)" if more else ""
    body = {"title": page.get("title") or "", "url": page["url"], "text": chunk}
    return Outcome(True, untrusted(f"Текст страницы {page['url']}", body) + tail, f"Читаю: {site_name(url)}")


# --- хранилище заметок ----------------------------------------------------------------------------------


async def _vault_search(s: "Session", args: dict[str, Any]) -> Outcome:
    query = _arg(args, "query")
    found = await s.ctx.run_io(s.vault.search, query, _int(args, "limit", 10, 1, 30))
    if not found:
        return Outcome(True, "В заметках ничего не нашлось.", f"Ищу в заметках: {query}")
    return Outcome(True, untrusted("Найдено в заметках", found), f"Ищу в заметках: {query}")


async def _vault_read(s: "Session", args: dict[str, Any]) -> Outcome:
    path, text, cut = await s.ctx.run_io(s.vault.read, _arg(args, "path"))
    tail = "\n(Заметка длинная — показано начало.)" if cut else ""
    return Outcome(True, untrusted(f"Заметка {path}", text) + tail, f"Читаю заметку {path}")


async def _vault_list(s: "Session", args: dict[str, Any]) -> Outcome:
    items = await s.ctx.run_io(s.vault.listing, _arg(args, "folder"))
    return Outcome(True, "\n".join(items) or "(пусто)", "Смотрю папку заметок")


async def _vault_write(s: "Session", args: dict[str, Any]) -> Outcome:
    path = _arg(args, "path")
    content = str(args.get("content") or "")
    mode = _arg(args, "mode") or "replace"
    if mode not in ("replace", "append", "create"):
        mode = "replace"
    if not content.strip():
        return Outcome(False, "Ошибка: пустой текст заметки")
    note = s.task.note or s.vault.task_note(s.task)
    if path.strip().replace("\\", "/").removesuffix(".md") == note.removesuffix(".md"):
        return Outcome(False, "Ошибка: заметку задачи ведёт Jarvis — результат запиши в отдельную заметку")
    written = await s.ctx.run_io(s.vault.write, path, content, mode)
    s.written.append(written)
    verb = {"append": "Дописал", "create": "Создал"}.get(mode, "Записал")
    return Outcome(True, f"{verb} заметку {written}", f"{verb} [[{written.removesuffix('.md')}]]")


async def _measure_add(s: "Session", args: dict[str, Any]) -> Outcome:
    table = _arg(args, "table")
    if not table:
        return Outcome(False, "Ошибка: нужно название таблицы замеров (table), например «Курс доллара»")
    hourly = s.task.repeat.get("kind") == "hours"
    result = await s.ctx.run_io(lambda: measure.add_row(s.vault, table, args.get("values"), hourly=hourly))
    s.task.add_table(result["path"])
    s.written.append(result["path"])
    row = "; ".join(f"{k}: {v}" for k, v in result["row"].items() if v)
    verb = "Обновил" if result["replaced"] else "Записал"
    chart = " График обновлён." if result["chart"] else ""
    return Outcome(
        True,
        f"{verb} замер в {result['path']}: {row}. Строк в таблице: {result['rows']}.{chart}",
        f"{verb} замер: {one_line(row, 160)} → [[{result['path'].removesuffix('.md')}]]",
    )


# --- план, ход работы, вопрос ---------------------------------------------------------------------------


def _steps(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [line for line in raw.splitlines() if line.strip()]
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            text, done = str(item.get("text") or item.get("step") or ""), bool(item.get("done"))
        else:
            text, done = str(item), False
        text = one_line(text.lstrip("-*[] x").strip(), 200)
        if text:
            out.append({"text": text, "done": done})
    return out[:20]


async def _update_plan(s: "Session", args: dict[str, Any]) -> Outcome:
    steps = _steps(args.get("steps"))
    if not steps:
        return Outcome(False, "Ошибка: нужен список шагов: [{text, done}]")
    s.task.plan = steps
    done = sum(1 for x in steps if x["done"])
    lines = "\n".join(f"{'[x]' if x['done'] else '[ ]'} {x['text']}" for x in steps)
    return Outcome(True, f"План обновлён ({done} из {len(steps)}):\n{lines}", "")


async def _report(s: "Session", args: dict[str, Any]) -> Outcome:
    text = _arg(args, "text")
    if not text:
        return Outcome(False, "Ошибка: пустой текст")
    s.task.add_log(text)
    return Outcome(True, "Записано в ход работы.", "")


async def _ask_user(s: "Session", args: dict[str, Any]) -> Outcome:
    question = _arg(args, "question")
    if not question:
        return Outcome(False, "Ошибка: нет вопроса")
    raw = args.get("options")
    options = [one_line(str(o), 60) for o in raw if str(o).strip()][:6] if isinstance(raw, list) else []
    answer = await s.service.ask(s, question, options)
    return Outcome(True, f"Ответ человека: «{one_line(answer, 800)}»", f"Вопрос: {one_line(question, 80)}")


# --- серверы MCP --------------------------------------------------------------------------------------


def mcp_tools(ctx: Any, taken: set[str], limit: int = 48) -> list[TaskTool]:
    """Инструменты подключённых серверов MCP: рискованные (отправить, удалить, оплатить…) — с разрешения."""
    try:
        specs = ctx.mcp.tools()
    except Exception:
        log.exception("инструменты MCP недоступны фоновой задаче")
        return []
    out: list[TaskTool] = []
    for spec in specs or []:
        tool = mcp_tool(ctx, spec)
        if tool is None or tool.name in taken:
            continue
        out.append(_mcp(tool))
        if len(out) >= limit:
            break
    return out


def _mcp(tool: Any) -> TaskTool:
    title = tool.title_for({})

    async def run(s: "Session", args: dict[str, Any]) -> Outcome:
        ai = s.ctx.settings.value.ai
        risk = tool_risk(tool.name, tool.description)
        choice = decision(ai, tool.name, risk)
        if choice == "deny":
            return Outcome(False, f"Ошибка: «{title}» запрещён в настройках Jarvis AI")
        if choice == "ask" or risk == "risky":
            detail = " · ".join(f"{k}: «{one_line(str(v), 100)}»" for k, v in list(args.items())[:4])
            await s.service.approve(
                s, f"{title}?", detail, f"mcp:{tool.name}:{json.dumps(args, sort_keys=True)}"
            )
        text = str(await s.ctx.mcp.call(tool.name, args) or "")
        if len(text) > MAX_MCP_RESULT:
            text = text[:MAX_MCP_RESULT] + "… [обрезано]"
        return Outcome(True, untrusted("Ответ сервера MCP", text), title)

    return TaskTool(
        tool.name,
        tool.description,
        {},
        [],
        run,
        lambda a: title,
        timeout=600.0,
        risk="act",
        schema=tool.parameters,
    )


# --- набор -------------------------------------------------------------------------------------------------


@dataclass
class Toolset:
    tools: dict[str, TaskTool] = field(default_factory=dict)

    def add(self, *tools: TaskTool) -> None:
        for tool in tools:
            self.tools[tool.name] = tool

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values()] + [FINISH_SPEC]


FINISH_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Закончить работу над задачей: status — done (сделано), partial (сделано не всё), failed (не "
            "вышло), waiting (ждёшь ответа человека — продолжишь после него); summary — коротко, что "
            "сделано и где результат; result — путь главной заметки с результатом (если она есть)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["done", "partial", "failed", "waiting"]},
                "summary": _s("Итог для человека, по-русски, коротко"),
                "result": _s("Путь заметки с результатом в хранилище (необязательно)"),
            },
            "required": ["summary"],
        },
    },
}


def base_tools(*, web_search: bool = True, web_read: bool = True) -> list[TaskTool]:
    """Инструменты без браузера. web_search / web_read — выключаются, если у провайдера есть свои (Claude
    Code, Gemini CLI, Codex)."""
    tools: list[TaskTool] = []
    if web_search:
        tools.append(
            TaskTool(
                "web_search",
                "Поиск в интернете: заголовки, адреса и короткие выдержки результатов.",
                {
                    "query": _s("Запрос"),
                    "max_results": {"type": "integer", "description": "Сколько результатов (1–10)"},
                },
                ["query"],
                _web_search,
                lambda a: f"Ищу: {one_line(_arg(a, 'query'), 60)}",
                timeout=40.0,
            )
        )
    if web_read:
        tools.append(
            TaskTool(
                "web_read",
                "Прочитать страницу по адресу — её основной текст (без меню и рекламы). Длинная — по частям "
                "(part). Страница с входом, кнопками и формами — браузер (browser_*), если он есть.",
                {"url": _s("Адрес https://…"), "part": {"type": "integer", "description": "Часть (с 1)"}},
                ["url"],
                _web_read,
                lambda a: f"Читаю {site_name(_arg(a, 'url')) or _arg(a, 'url')}",
                timeout=45.0,
            )
        )
    tools += [
        TaskTool(
            "vault_search",
            "Найти заметки в хранилище человека по словам (имя и текст заметки).",
            {"query": _s("Слова для поиска"), "limit": {"type": "integer", "description": "Сколько (1–30)"}},
            ["query"],
            _vault_search,
            lambda a: f"Ищу в заметках: {one_line(_arg(a, 'query'), 60)}",
        ),
        TaskTool(
            "vault_read",
            "Прочитать заметку хранилища по пути (например «Проекты/План.md»).",
            {"path": _s("Путь внутри хранилища")},
            ["path"],
            _vault_read,
            lambda a: f"Читаю заметку {one_line(_arg(a, 'path'), 60)}",
        ),
        TaskTool(
            "vault_list",
            "Список папок и заметок в папке хранилища (пусто — корень).",
            {"folder": _s("Папка внутри хранилища")},
            [],
            _vault_list,
            lambda a: "Смотрю папку заметок",
        ),
        TaskTool(
            "vault_write",
            "Записать заметку (Markdown) в свою папку хранилища: path — например «Исследования/Рынок "
            "ноутбуков» (своя папка добавится сама), mode — replace (целиком), append (дописать в конец), "
            "create (новая; имя занято — с номером).",
            {
                "path": _s("Путь в своей папке"),
                "content": _s("Текст заметки в Markdown"),
                "mode": {"type": "string", "enum": ["replace", "append", "create"]},
            },
            ["path", "content"],
            _vault_write,
            lambda a: f"Пишу заметку {one_line(_arg(a, 'path'), 60)}",
            risk="act",
        ),
        TaskTool(
            "measure_add",
            "Записать замер — числа на дату (цена, курс, подписчики, метрика) — строкой в таблицу замеров "
            "Замеры/<table> своей папки. Дата ставится сама (замер за прошлый день — колонка «Дата», "
            "например 28.10.2026); за ту же дату строка заменяется. От раза к разу — та же таблица и те же "
            "колонки (с единицами: «Курс ЦБ, ₽»); новая колонка добавится сама. График Jarvis рисует сам.",
            {
                "table": _s("Название таблицы, например «Курс доллара»"),
                "values": {
                    "type": "array",
                    "description": "Значения: колонка и значение (числа — как пишут люди: 81,42)",
                    "items": {
                        "type": "object",
                        "properties": {"column": {"type": "string"}, "value": {"type": "string"}},
                        "required": ["column", "value"],
                    },
                },
            },
            ["table", "values"],
            _measure_add,
            lambda a: f"Записываю замер: {one_line(_arg(a, 'table'), 60)}",
            risk="act",
        ),
        TaskTool(
            "update_plan",
            "План задачи — весь список шагов с отметками: [{text, done}]. Составь в начале и обновляй по "
            "ходу.",
            {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}},
                        "required": ["text"],
                    },
                }
            },
            ["steps"],
            _update_plan,
            lambda a: "Обновляю план",
        ),
        TaskTool(
            "report",
            "Записать в ход работы, что сделано или найдено (одна-две фразы) — человек видит это в Jarvis.",
            {"text": _s("Что сделано или найдено")},
            ["text"],
            _report,
            lambda a: one_line(_arg(a, "text"), 80),
        ),
        TaskTool(
            "ask_user",
            "Спросить человека и дождаться ответа: когда неясно, что именно нужно, или перед необратимым. "
            "Коротко; options — варианты ответа, если они есть.",
            {"question": _s("Вопрос"), "options": {"type": "array", "items": {"type": "string"}}},
            ["question"],
            _ask_user,
            lambda a: f"Спрашиваю: {one_line(_arg(a, 'question'), 60)}",
            timeout=24 * 3600.0,
        ),
    ]
    return tools


def check_url(url: str) -> str:
    """Публичный адрес https/http (локальная сеть и сам компьютер — нельзя)."""
    try:
        return check_public_url(url)
    except UserFacingError:
        raise
    except Exception as e:
        raise UserFacingError(f"Ошибка: адрес не подходит: {e}") from e
