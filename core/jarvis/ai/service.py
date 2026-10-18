"""Jarvis AI: чаты с моделью и инструментами — команды ПК, поиск, погода, новости, память, заметки, календарь,
MCP.

Ход ответа идёт событиями (docs/CONTRACTS.md, раздел 4): chat.message (сообщение человека), chat.status («Ищу
актуальную информацию…»), chat.delta (куски текста), chat.tool (карточки действий), chat.message и chat.done
(итог) или chat.error. Модель может вызывать инструменты до восьми раундов подряд; результаты инструментов
попадают в историю чата, поэтому следующий вопрос видит, что уже сделано.

Провайдеры без ключа API — «ChatGPT (подписка)» (jarvis.ai.chatgpt) и программы человека Claude Code,
Gemini CLI, Qwen Code (jarvis.ai.cli; инструменты — с сервера MCP ядра jarvis.mcp.server) — отвечают в тех же
событиях.

Режимы: «chat» — без действий на ПК (поиск, память и заметки можно), «all» — все инструменты, «agent» — задачу
выполняет агент (jarvis.ai.agent). Голосовые запросы, Telegram и пульт пишутся в свой постоянный чат (kind
voice): ответ для голоса — короткий, без markdown. Фраза «агент, …» отправляет задачу агенту в любом режиме.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.ai import policy
from jarvis.ai.chatgpt import ChatGpt
from jarvis.ai.chats import MODES, ChatStore, ui_message
from jarvis.ai.cli import CliProviders
from jarvis.ai.format import chat_title, one_line, speech_text
from jarvis.ai.llm import LlmClient, LlmError, LlmResponse, ToolCall, Usage, user_content
from jarvis.ai.prompts import system_prompt
from jarvis.ai.providers import PRESETS
from jarvis.ai.tools import Tool, ToolEnv, ToolOutput, ToolRegistry, args_detail, confirm
from jarvis.ai.web import WebClient
from jarvis.db import new_id
from jarvis.mcp.server import JarvisMcpServer
from jarvis.platform.base import PlatformError
from jarvis.service_base import BaseService
from jarvis.services import AskResult, Source, UserFacingError

log = logging.getLogger(__name__)

MAX_ROUNDS = 8  # раундов «модель → инструменты» на один ответ
HISTORY_CHARS = 24_000  # история чата для модели (≈ 6–8 тыс. токенов)
VOICE_TURNS = 6  # голосовой чат бесконечен — модель видит только последние реплики…
VOICE_WINDOW_S = 3 * 3600  # …за последние три часа
TOOL_RESULT_CHARS = 12_000
OLD_TOOL_CHARS = 1_200
STORED_TOOL_CHARS = 20_000
MAX_SOURCES = 8
MAX_IMAGES = 4
MAX_IMAGE_CHARS = 8_000_000  # data URL ≈ 6 МБ картинки
STOPPED = "Остановлено"

VOICE_CHATS = {"voice": "Голосовой чат", "telegram": "Telegram", "remote": "Пульт"}
_AGENT_VERBS = (
    "открой|найди|напиши|отправь|зайди|запусти|нажми|заполни|перейди|сделай|создай|проверь|скачай|закрой|"
    "ответь|удали|переименуй|скопируй|посмотри|зарегистрируй|купи|закажи|настрой|установи"
)
AGENT_PHRASE = re.compile(
    rf"^\s*(?:агент|agent)\s*(?:[,:—–-]\s*|\s+(?=(?:сам\s+)?(?:{_AGENT_VERBS})\b))(?P<task>.+)$",
    re.I | re.S,
)
_IMAGE_URL = re.compile(r"^data:image/(?:png|jpe?g|webp|gif);base64,", re.I)


@dataclass
class Turn:
    """Один ответ ассистента: что спросили, куда писать, что уже показано и сделано."""

    chat_id: str
    user_id: str  # id сообщения человека
    reply_id: str  # id будущего ответа ассистента (по нему интерфейс собирает chat.delta)
    text: str
    images: list[str]
    mode: str
    source: Source
    model: str
    kind: str
    agent_task: str = ""
    shown: str = ""  # текст, уже отправленный в интерфейс
    pending: str = ""  # текст, ещё не отправленный событием chat.delta
    flushed_at: float = 0.0
    new_round: bool = True
    rounds: list[str] = field(default_factory=list)  # текст каждого раунда модели
    actions: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    memory: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    status: tuple[str, str] = ("", "")
    finished: bool = False


# --- история для модели --------------------------------------------------------------------------------


def group_turns(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Сообщения чата → обмены: сообщение человека и всё, что ответил ассистент (по meta.turn)."""
    groups: list[list[dict[str, Any]]] = []
    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["role"] == "user":
            group = [row]
            groups.append(group)
            by_user[row["id"]] = group
            continue
        owner = row["meta"].get("turn")
        if owner in by_user:
            by_user[owner].append(row)
        elif groups:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _group_messages(group: list[dict[str, Any]], *, keep_tools: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in group:
        role, meta = row["role"], row["meta"]
        if role == "user":
            content = row["content"]
            if meta.get("images"):
                content = f"{content}\n\n[к сообщению были приложены изображения]".strip()
            out.append({"role": "user", "content": content})
        elif role == "assistant" and row["tool_calls"]:
            if keep_tools:
                out.append(
                    {"role": "assistant", "content": row["content"] or None, "tool_calls": row["tool_calls"]}
                )
        elif role == "tool":
            if keep_tools:
                content = row["content"]
                if len(content) > OLD_TOOL_CHARS:
                    content = content[:OLD_TOOL_CHARS] + "… [обрезано]"
                out.append({"role": "tool", "tool_call_id": row["tool_call_id"] or "", "content": content})
        elif role == "assistant":
            content = (meta.get("llm_content") if keep_tools else None) or row["content"]
            if not keep_tools and meta.get("actions"):
                done = "; ".join(
                    f"{a.get('title')} — {'выполнено' if a.get('ok') else 'ошибка'}" for a in meta["actions"]
                )
                content = f"{content}\n\n(Действия в этом ответе: {done}.)".strip()
            if content.strip():
                out.append({"role": "assistant", "content": content})
    return out


def _merge_user(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if isinstance(a["content"], str) and isinstance(b["content"], str):
        return {"role": "user", "content": f"{a['content']}\n\n{b['content']}".strip()}

    def parts(content: Any) -> list[dict[str, Any]]:
        return content if isinstance(content, list) else [{"type": "text", "text": str(content)}]

    return {"role": "user", "content": parts(a["content"]) + parts(b["content"])}


def sanitize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """История, которую примет любой провайдер: у каждого вызова инструмента есть результат, лишних
    результатов нет, первым идёт сообщение человека, два сообщения человека подряд склеены."""
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if message["role"] == "assistant" and message.get("tool_calls"):
            results: dict[str, dict[str, Any]] = {}
            j = i + 1
            while j < len(messages) and messages[j]["role"] == "tool":
                results[messages[j].get("tool_call_id", "")] = messages[j]
                j += 1
            out.append(message)
            for call in message["tool_calls"]:
                call_id = call.get("id", "")
                out.append(
                    results.get(call_id) or {"role": "tool", "tool_call_id": call_id, "content": "Отменено"}
                )
            i = j
            continue
        if message["role"] == "tool":
            i += 1
            continue
        if message["role"] == "user" and out and out[-1]["role"] == "user":
            out[-1] = _merge_user(out[-1], message)
        else:
            out.append(message)
        i += 1
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _size(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


class Ai(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.llm = LlmClient(ctx)
        self.web = WebClient(ctx)
        self.chats = ChatStore(ctx)
        self.tools = ToolRegistry(ctx)
        self.chatgpt = ChatGpt(self)  # провайдер «ChatGPT (подписка)» через Codex
        self.mcp_server = JarvisMcpServer(ctx)  # инструменты Jarvis для программ-провайдеров (/api/ai/mcp)
        # «Claude (подписка)», «Gemini CLI», «Qwen Code» через программы человека
        self.cli = CliProviders(self)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._configured: bool | None = None
        self._unsubs: list[Callable[[], None]] = []

    def set_transport(self, transport: Any) -> None:
        """Подменить сеть (тесты): запросы к модели и инструментов пойдут через transport."""
        self.llm.transport = transport
        self.web.transport = transport

    def account_backend(self) -> Any:
        """Провайдер без ключа API (ChatGPT или программа человека), если выбран он, иначе None."""
        if self.chatgpt.selected:
            return self.chatgpt
        return self.cli.selected()

    @property
    def configured(self) -> bool:
        backend = self.account_backend()
        if backend is not None:
            return bool(backend.configured)
        return self.llm.configured

    def refresh_configured(self) -> bool:
        """Перепроверить «ИИ настроен» (ключ, адрес, вход); изменилось — событие ai.configured."""
        now = self.configured
        if now != self._configured:
            self._configured = now
            self.ctx.bus.publish("ai.configured", {"configured": now})
        return now

    async def start(self) -> None:
        self._configured = self.configured
        self._unsubs += [
            self.ctx.settings.on_change(self._on_settings),
            self.ctx.bus.on("secrets.changed", lambda _e: self.refresh_configured()),
        ]
        await self.mcp_server.start()
        await self.chatgpt.start()
        await self.cli.start()

    def _on_settings(self, old: Any, new: Any) -> None:
        if old.ai.provider != new.ai.provider:
            backend = self.cli.get(new.ai.provider)
            if backend is not None and self.ctx.loop is not None and not self.ctx.loop.is_closed():
                self.ctx.loop.call_soon_threadsafe(backend.on_selected)  # проверить вход в программу
        if old.ai != new.ai:
            self.refresh_configured()

    async def stop(self) -> None:
        for off in self._unsubs:
            off()
        self._unsubs.clear()
        tasks = [t for group in self._tasks.values() for t in group if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=3.0)
        await self.chatgpt.stop()
        await self.cli.stop()
        await self.mcp_server.stop()
        await self.llm.aclose()

    # --- провайдеры и расходы (для API) ---------------------------------------------------------------
    def providers(self) -> list[dict[str, Any]]:
        custom_url = self.ctx.settings.value.ai.base_url.strip()
        items = [p.to_json(custom_url if p.id == "custom" else None) for p in PRESETS.values()]
        out = []
        for item in items:
            backend = self.cli.get(item["id"])
            if item["id"] == "chatgpt":
                item = self.chatgpt.preset_json(item)
            elif backend is not None:
                item = backend.preset_json(item)
            out.append(item)
        return out

    async def list_models(self) -> list[str]:
        """Модели выбранного провайдера (у ChatGPT — из каталога Codex, у программ — свой список)."""
        backend = self.account_backend()
        if backend is not None:
            return list(await backend.list_models())
        return await self.llm.list_models()

    async def test_connection(self) -> dict[str, Any]:
        """Проверка связи с выбранным провайдером: {ok, error?, latency_ms, model}."""
        backend = self.account_backend()
        if backend is not None:
            return dict(await backend.test())
        return await self.llm.test()

    def usage(self) -> dict[str, Any]:
        return {"month": self.llm.month_usage(), "budget": self.ctx.settings.value.ai.monthly_budget}

    # --- чаты (для API) ---------------------------------------------------------------------------------
    def create_chat(self, title: str = "", mode: str | None = None) -> dict[str, Any]:
        mode = mode if mode in MODES else self.ctx.settings.value.ai.mode
        return self.chats.create(title=title, mode=mode)

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        chat = self.chats.require(chat_id)
        return {"chat": chat, "messages": self.chats.ui_messages(chat_id)}

    def delete_chat(self, chat_id: str) -> bool:
        self.stop_chat(chat_id)
        self.chatgpt.forget_chat(chat_id)
        self.cli.forget_chat(chat_id)
        return self.chats.delete(chat_id)

    def stop_chat(self, chat_id: str) -> bool:
        """Кнопка «Стоп»: оборвать ответ (и ждущие очереди сообщения) в чате."""
        tasks = [t for t in self._tasks.get(chat_id, set()) if not t.done()]
        for task in tasks:
            task.cancel()
        return bool(tasks)

    def stop_all(self) -> int:
        """«Стоп» голосом: оборвать все ответы, которые сейчас идут. Вернёт, сколько оборвано."""
        return sum(self.stop_chat(chat_id) for chat_id in list(self._tasks))

    # --- главный вход -------------------------------------------------------------------------------------
    async def ask(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        source: Source = "text",
        mode: str | None = None,
        images: list[str] | None = None,
    ) -> AskResult:
        try:
            turn = self._begin(text, chat_id=chat_id, source=source, mode=mode, images=images)
        except UserFacingError as e:
            return AskResult(chat_id or "", "", error=str(e))
        return await self._spawn(turn)

    async def post_message(
        self,
        chat_id: str,
        text: str,
        *,
        images: list[str] | None = None,
        mode: str | None = None,
        source: Source = "ui",
    ) -> dict[str, Any]:
        """Сообщение из окна чата: ответ идёт событиями, вернуть сразу id сообщения."""
        self.chats.require(chat_id)
        turn = self._begin(text, chat_id=chat_id, source=source, mode=mode, images=images)
        self.ctx.spawn(self._spawn(turn), f"chat-{turn.chat_id}")
        return {"chat_id": turn.chat_id, "message_id": turn.user_id, "reply_id": turn.reply_id}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Одиночный запрос без инструментов (сборщик команд, конспекты, перевод, диктовка)."""
        backend = self.account_backend()
        if backend is not None:
            return await backend.complete(
                messages, json_schema=json_schema, temperature=temperature, max_tokens=max_tokens
            )
        response = await self.llm.chat(
            messages, json_schema=json_schema, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        return response.parsed if json_schema is not None else response.content

    # --- начало ответа -----------------------------------------------------------------------------------
    def _check_images(self, images: list[str] | None) -> list[str]:
        out = []
        for url in images or []:
            if not isinstance(url, str) or not _IMAGE_URL.match(url):
                raise UserFacingError("Картинка должна быть в формате data URL (png, jpeg, webp или gif)")
            if len(url) > MAX_IMAGE_CHARS:
                raise UserFacingError("Картинка слишком большая — не больше 6 МБ")
            out.append(url)
        if len(out) > MAX_IMAGES:
            raise UserFacingError(f"Не больше {MAX_IMAGES} картинок в одном сообщении")
        return out

    def _chat_for(self, chat_id: str | None, source: Source, mode: str | None) -> dict[str, Any]:
        chat = self.chats.get(chat_id)
        if chat is not None:
            return chat
        settings = self.ctx.settings.value.ai
        if source in VOICE_CHATS:
            return self.chats.create(title=VOICE_CHATS[source], kind="voice", mode=settings.mode)
        return self.chats.create(mode=mode if mode in MODES else settings.mode)

    def _begin(
        self,
        text: str,
        *,
        chat_id: str | None,
        source: Source,
        mode: str | None,
        images: list[str] | None,
    ) -> Turn:
        text = (text or "").strip()
        pictures = self._check_images(images)
        if not text and not pictures:
            raise UserFacingError("Пустое сообщение")
        chat = self._chat_for(chat_id, source, mode)
        agent_task = ""
        found = AGENT_PHRASE.match(text)
        if found and found["task"].strip():
            agent_task = found["task"].strip()
        if agent_task:
            turn_mode = "agent"
        elif mode in MODES:
            turn_mode = mode
        elif chat["kind"] == "voice":
            turn_mode = self.ctx.settings.value.ai.mode
        else:
            turn_mode = chat["mode"]
        first = self.ctx.db.query_one(
            "SELECT 1 FROM messages WHERE chat_id=? AND role='user' LIMIT 1", (chat["id"],)
        )
        row = self.chats.add_message(
            chat["id"], "user", text, meta={"images": pictures} if pictures else None
        )
        self.ctx.bus.publish("chat.message", {"chat_id": chat["id"], "message": ui_message(row)})
        changes: dict[str, Any] = {}
        if chat["kind"] == "chat" and first is None and chat["title"] in ("", "Новый чат"):
            changes["title"] = chat_title(text) if text else "Картинка"
        if chat["kind"] == "chat" and mode in MODES and mode != chat["mode"]:
            changes["mode"] = mode
        if changes:
            chat = self.chats.update(chat["id"], **changes)
        return Turn(
            chat_id=chat["id"],
            user_id=row["id"],
            reply_id=new_id(),
            text=text,
            images=pictures,
            mode=turn_mode,
            source=source,
            model=chat["model"],
            kind=chat["kind"],
            agent_task=agent_task,
        )

    async def _spawn(self, turn: Turn) -> AskResult:
        """Ответ — в отдельной задаче: её можно оборвать кнопкой «Стоп», не трогая того, кто спросил."""
        task = asyncio.ensure_future(self._run(turn))
        group = self._tasks.setdefault(turn.chat_id, set())
        group.add(task)
        task.add_done_callback(group.discard)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _run(self, turn: Turn) -> AskResult:
        lock = self._locks.setdefault(turn.chat_id, asyncio.Lock())
        try:
            await lock.acquire()
        except asyncio.CancelledError:
            _uncancel()
            return self._finish(turn, stopped=True)
        try:
            if turn.mode == "agent":
                return await self._run_agent(turn)
            return await self._run_chat(turn)
        finally:
            lock.release()

    # --- ответ модели с инструментами ---------------------------------------------------------------------
    async def _run_chat(self, turn: Turn) -> AskResult:
        backend = self.account_backend()
        if backend is not None:
            return await backend.run_chat(turn)
        problem = self.llm.problem(turn.model or None)
        if problem:
            return self._finish(turn, error=problem)
        self._status(turn, "thinking", "Думаю…")
        tools = self.tools.available(turn.mode, source=turn.source)
        specs = [t.spec() for t in tools.values()]
        env = ToolEnv(self.ctx, self.web, turn.source, turn.chat_id, user_text=turn.text)
        try:
            messages = self._messages(turn, has_agent_tool="run_agent" in tools)
            for round_no in range(MAX_ROUNDS + 1):
                last = round_no == MAX_ROUNDS
                if round_no:
                    self._status(turn, "thinking", "Формирую ответ…")
                if last:
                    messages.append(
                        {
                            "role": "user",
                            "content": "(Хватит вызывать инструменты — ответь по уже полученным данным.)",
                        }
                    )
                turn.new_round = True
                response = await self.llm.chat(
                    messages,
                    tools=specs or None,
                    tool_choice="none" if last and specs else None,
                    model=turn.model or None,
                    stream=True,
                    on_delta=lambda delta: self._delta(turn, delta),
                )
                self._flush(turn)
                turn.usage.add(response.usage)
                turn.rounds.append(response.content)
                if not response.tool_calls or last:
                    break
                messages.append(self._save_tool_round(turn, response))
                for call in response.tool_calls:
                    content = await self._call_tool(turn, env, tools, call)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        except LlmError as e:
            return self._finish(turn, error=str(e))
        except asyncio.CancelledError:
            _uncancel()
            return self._finish(turn, stopped=True)
        except Exception as e:
            log.exception("ответ ИИ упал")
            return self._finish(turn, error=f"Внутренняя ошибка Jarvis AI: {e}")
        return self._finish(turn)

    def _messages(self, turn: Turn, *, has_agent_tool: bool) -> list[dict[str, Any]]:
        settings = self.ctx.settings.value
        memory = ""
        if settings.ai.memory:
            try:
                memory = self.ctx.notes.memory_prompt()
            except Exception:
                log.exception("память не прочиталась")
        system = system_prompt(
            settings,
            source=turn.source,
            mode=turn.mode,
            memory=memory,
            has_agent_tool=has_agent_tool,
            active=self._active_context(),
        )
        vision = self.llm.config(turn.model or None).vision
        current = {"role": "user", "content": user_content(turn.text, turn.images, vision)}
        history = sanitize_history([*self._history(turn), current])
        return [{"role": "system", "content": system}, *history]

    def _active_context(self) -> str:
        """Что идёт прямо сейчас: работающий агент и дела на сегодня — чтобы «как там письмо?» имело смысл."""
        lines: list[str] = []
        try:
            state = self.ctx.agent.state()
        except Exception:
            state = {}
        if state.get("status") in ("planning", "running", "waiting") and state.get("task"):
            doing = state.get("text") or "в работе"
            lines.append(f"Сейчас работает агент: «{one_line(str(state['task']), 120)}» — {doing}.")
        try:
            end_of_day = datetime.now().replace(hour=23, minute=59, second=59).timestamp()
            tasks = [
                t
                for t in self.ctx.notes.tasks_list(include_done=False)
                if t.get("due") and float(t["due"]) <= end_of_day
            ]
        except Exception:
            tasks = []
        if tasks:
            lines.append(
                "Дела на сегодня: " + "; ".join(one_line(str(t["title"]), 60) for t in tasks[:3]) + "."
            )
        return "\n".join(lines)

    def _history(self, turn: Turn) -> list[dict[str, Any]]:
        groups = group_turns(self.chats.rows(turn.chat_id))
        index = next((i for i, g in enumerate(groups) if g[0]["id"] == turn.user_id), len(groups))
        groups = groups[:index]
        if turn.kind == "voice":
            since = time.time() - VOICE_WINDOW_S
            groups = [g for g in groups if g[0]["created"] >= since][-VOICE_TURNS:]
        selected: list[list[dict[str, Any]]] = []
        budget = HISTORY_CHARS
        for position, group in enumerate(reversed(groups)):
            messages = _group_messages(group, keep_tools=position < 2)
            size = _size(messages)
            if selected and size > budget:
                break
            selected.insert(0, messages)
            budget -= size
            if budget <= 0:
                break
        return [m for group in selected for m in group]

    def _save_tool_round(self, turn: Turn, response: LlmResponse) -> dict[str, Any]:
        calls = [call.to_message() for call in response.tool_calls]
        self.chats.add_message(
            turn.chat_id,
            "assistant",
            response.content,
            tool_calls=calls,
            meta={"turn": turn.user_id, "hidden": True},
        )
        return {"role": "assistant", "content": response.content or None, "tool_calls": calls}

    async def _call_tool(self, turn: Turn, env: ToolEnv, tools: dict[str, Tool], call: ToolCall) -> str:
        tool = tools.get(call.name)
        if tool is None:
            out = ToolOutput.fail(f"инструмента «{call.name}» нет")
            self._save_tool_result(turn, call, out.content)
            return out.content
        try:
            args: dict[str, Any] | None = call.parse_arguments()
        except ValueError:
            args = None
        title = tool.title_for(args or {})
        self._status(turn, tool.status, tool.status_text)
        self._tool_event(turn, call, title, "running")
        if args is None:
            out = ToolOutput.fail("аргументы не в формате JSON — повтори вызов", title=title)
        elif (refused := await self._permission(turn, env, tool, title, args)) is not None:
            out = refused
        else:
            try:
                out = await asyncio.wait_for(tool.handler(env, args), tool.timeout)
            except (UserFacingError, PlatformError) as e:
                out = ToolOutput.fail(str(e), title=title)
            except TimeoutError:
                out = ToolOutput.fail("инструмент не ответил вовремя", title=title)
            except Exception as e:
                log.exception("инструмент %s упал", call.name)
                out = ToolOutput.fail(f"внутренняя ошибка: {e}", title=title)
        final_title = out.title or title
        self._tool_event(turn, call, final_title, "ok" if out.ok else "error", out.detail)
        if tool.action:
            action: dict[str, Any] = {"title": final_title, "ok": out.ok}
            if out.detail:
                action["detail"] = out.detail
            turn.actions.append(action)
        known = {s["url"] for s in turn.sources}
        for source in out.sources:
            if source.get("url") and source["url"] not in known and len(turn.sources) < MAX_SOURCES:
                turn.sources.append({"title": source.get("title") or source["url"], "url": source["url"]})
                known.add(source["url"])
        turn.memory.extend(out.memory)
        content = out.content
        if len(content) > TOOL_RESULT_CHARS:
            content = content[:TOOL_RESULT_CHARS] + "… [обрезано]"
        self._save_tool_result(turn, call, content)
        return content

    async def _permission(
        self, turn: Turn, env: ToolEnv, tool: Tool, title: str, args: dict[str, Any]
    ) -> ToolOutput | None:
        """Разрешения инструментов (Настройки → Jarvis AI → Инструменты и разрешения): «Запрещено» — отказ,
        «Спрашивать» — карточка подтверждения; рискованные инструменты MCP (отправка, удаление, оплата)
        спрашиваются всегда. None — можно выполнять."""
        risk = policy.tool_risk(tool.name, tool.description)
        choice = policy.decision(self.ctx.settings.value.ai, tool.name, risk)
        if choice == "deny":
            return ToolOutput.fail(
                "этот инструмент запрещён в настройках (Jarvis AI → Инструменты и разрешения) — скажи "
                "пользователю и не обходи запрет другими инструментами",
                title=title,
            )
        asks = choice == "ask" or (risk == "risky" and tool.name.startswith("mcp__"))
        if asks and not await confirm(env, f"{title}?", args_detail(args)):
            return ToolOutput(
                "Пользователь не разрешил это действие. Не повторяй его и не обходи другим путём.",
                ok=False,
                title=title,
                detail="Отклонено",
            )
        return None

    def _save_tool_result(self, turn: Turn, call: ToolCall, content: str) -> None:
        self.chats.add_message(
            turn.chat_id,
            "tool",
            content[:STORED_TOOL_CHARS],
            tool_call_id=call.id,
            name=call.name,
            meta={"turn": turn.user_id, "hidden": True},
        )

    # --- агент ---------------------------------------------------------------------------------------------
    async def _run_agent(self, turn: Turn) -> AskResult:
        from jarvis.ai.agent import Agent

        agent = self.ctx.agent
        task = turn.agent_task or turn.text
        if not isinstance(agent, Agent):
            try:
                await agent.run(task, turn.source)
            except UserFacingError as e:
                return self._finish(turn, error=str(e))
            turn.shown = "Агент взялся за задачу."
            return self._finish(turn)
        try:
            run_id = await agent.run(
                task,
                turn.source,
                chat_id=turn.chat_id,
                turn_id=turn.user_id,
                reply_id=turn.reply_id,
                speak=False,
            )
        except UserFacingError as e:
            return self._finish(turn, error=str(e))
        turn.finished = True  # итог в чат запишет агент
        try:
            result = await agent.wait(run_id)
        except asyncio.CancelledError:
            _uncancel()
            await agent.stop_run()
            result = await agent.wait(run_id)
        spoken = speech_text(result.summary)
        return AskResult(
            turn.chat_id,
            result.summary,
            spoken=spoken if spoken != result.summary else "",
            actions=result.actions,
            error=result.error,
        )

    # --- события ---------------------------------------------------------------------------------------
    def _status(self, turn: Turn, status: str, text: str) -> None:
        if turn.status == (status, text):
            return
        turn.status = (status, text)
        self.ctx.bus.publish(
            "chat.status",
            {"chat_id": turn.chat_id, "message_id": turn.reply_id, "status": status, "text": text},
        )

    def _tool_event(self, turn: Turn, call: ToolCall, title: str, status: str, detail: str = "") -> None:
        data: dict[str, Any] = {
            "chat_id": turn.chat_id,
            "message_id": turn.reply_id,
            "call_id": call.id,
            "name": call.name,
            "title": title,
            "status": status,
        }
        if detail:
            data["detail"] = detail
        self.ctx.bus.publish("chat.tool", data)

    def _delta(self, turn: Turn, text: str) -> None:
        if turn.new_round:
            text = text.lstrip()
            if not text:
                return
            turn.new_round = False
            if turn.shown.strip():
                text = "\n\n" + text  # текст нового раунда (после инструментов) — с новой строки
        turn.shown += text
        turn.pending += text
        now = time.monotonic()
        if len(turn.pending) >= 80 or now - turn.flushed_at >= 0.05:
            self._flush(turn)

    def _flush(self, turn: Turn) -> None:
        if turn.pending:
            self.ctx.bus.publish(
                "chat.delta", {"chat_id": turn.chat_id, "message_id": turn.reply_id, "delta": turn.pending}
            )
            turn.pending = ""
        turn.flushed_at = time.monotonic()

    def _fallback_text(self, turn: Turn) -> str:
        address = self.ctx.settings.value.profile.address.strip()
        if not turn.actions:
            return "Модель ничего не ответила — попробуйте переформулировать."
        failed = [a for a in turn.actions if not a["ok"]]
        if not failed:
            return f"Готово, {address}." if address else "Готово."
        return "Не получилось: " + "; ".join(f"{a['title']} — {a.get('detail') or 'ошибка'}" for a in failed)

    def _finish(self, turn: Turn, *, error: str = "", stopped: bool = False) -> AskResult:
        """Сохранить итоговый ответ, разослать chat.message и chat.done (или chat.error); вернуть итог."""
        self._flush(turn)
        text = turn.shown.strip()
        if not text and not error and not stopped:
            text = self._fallback_text(turn)
        final = next((r.strip() for r in reversed(turn.rounds) if r.strip()), "")
        meta: dict[str, Any] = {
            "turn": turn.user_id,
            "sources": turn.sources,
            "actions": turn.actions,
            "memory": turn.memory,
            "usage": turn.usage.to_json(),
        }
        if error:
            meta["error"] = error
        if stopped:
            meta["stopped"] = True
        if final and final != text:
            meta["llm_content"] = final
        if not turn.finished:
            turn.finished = True
            row = self.chats.add_message(turn.chat_id, "assistant", text, meta=meta, message_id=turn.reply_id)
            self._status(turn, "error" if error else "done", error or (STOPPED if stopped else ""))
            self.ctx.bus.publish("chat.message", {"chat_id": turn.chat_id, "message": ui_message(row)})
            if error:
                self.ctx.bus.publish(
                    "chat.error", {"chat_id": turn.chat_id, "message_id": turn.reply_id, "error": error}
                )
            else:
                done: dict[str, Any] = {
                    "chat_id": turn.chat_id,
                    "message_id": turn.reply_id,
                    "text": text,
                    "sources": turn.sources,
                    "actions": turn.actions,
                    "usage": turn.usage.to_json(),
                }
                if stopped:
                    done["stopped"] = True
                self.ctx.bus.publish("chat.done", done)
        spoken = speech_text(final or text) if (final or text) else ""
        return AskResult(
            turn.chat_id,
            text,
            spoken=spoken if spoken != text else "",
            actions=turn.actions,
            sources=turn.sources,
            error=error or (STOPPED if stopped else ""),
        )


def _uncancel() -> None:
    """Отмена обработана (ответ сохранён) — снять пометку отмены с задачи."""
    task = asyncio.current_task()
    if task is not None:
        with contextlib.suppress(Exception):
            task.uncancel()
