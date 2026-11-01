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

from jarvis import text as textops
from jarvis.ai import apphints, policy, screen, vision
from jarvis.ai.chatgpt import ChatGpt
from jarvis.ai.chats import MODES, ChatStore, ui_message
from jarvis.ai.cli import CliProviders
from jarvis.ai.format import chat_title, one_line, speech_text
from jarvis.ai.llm import LlmClient, LlmError, LlmResponse, ToolCall, Usage, user_content
from jarvis.ai.prompts import system_prompt
from jarvis.ai.providers import PRESETS
from jarvis.ai.tools import Tool, ToolEnv, ToolOutput, ToolRegistry, args_detail, confirm
from jarvis.ai.web import WebClient
from jarvis.background.repeat import describe
from jarvis.db import new_id
from jarvis.mcp.server import JarvisMcpServer
from jarvis.platform.base import PlatformError
from jarvis.redact import set_outgoing
from jarvis.service_base import BaseService
from jarvis.services import AskResult, Source, UserFacingError
from jarvis.voice.stress import strip_accents

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
# «в фоне: собери…», «фоном найди…», «поставь задачу: …» — сразу фоновая задача (jarvis.background)
BACKGROUND_PHRASE = re.compile(
    r"^\s*(?:в\s+фоне|фоном|фоново|в\s+фоновом\s+режиме|фонов(?:ая|ую)\s+задач[аую]|"
    r"поставь\s+(?:фоновую\s+)?задачу)\s*[:,—–-]?\s*(?P<task>.+)$",
    re.I | re.S,
)
# «приватно: …» — ответ только моделью на этом ПК: без облака, поиска в интернете и серверов MCP
PRIVATE_PHRASE = re.compile(
    r"^\s*(?:джарвис[,\s]+)?(?:приватно|локально|по\s+секрету|без\s+облака|"
    r"только\s+на\s+этом\s+(?:пк|компьютере))\s*[:,—–-]?\s*(?P<text>.+)$",
    re.I | re.S,
)
# в приватном ответе нет инструментов, которые ходят в интернет (картинки рисует облако), и тех, что пишут то,
# что потом само уходит в каждый облачный запрос: память и дела на сегодня (Ai._active_context)
NETWORK_TOOLS = frozenset(
    {
        "web_search",
        "fetch_page",
        "get_weather",
        "get_news",
        "get_rates",
        "run_agent",
        "remember",
        "generate_image",
        "add_task",
    }
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
    private: bool = False  # «приватно: …» — только модель на этом ПК (LlmClient.private_config)
    note: str = ""  # пометка для модели к словам человека (снимок экрана приложен…) — в чат не пишется
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

    @property
    def prompt(self) -> str:
        """Слова человека для модели — с пометкой (note), если она есть."""
        return f"{self.text}\n\n{self.note}".strip() if self.note else self.text


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


def _group_messages(
    group: list[dict[str, Any]], *, keep_tools: bool, pictures: bool = False
) -> list[dict[str, Any]]:
    """Обмен → сообщения для модели. pictures — картинки сообщения человека остаются картинками
    (уточняющий вопрос к только что приложенному снимку: «а какого она года?»)."""
    out: list[dict[str, Any]] = []
    for row in group:
        role, meta = row["role"], row["meta"]
        if role == "user":
            content: Any = row["content"]
            if meta.get("images") and pictures:
                content = user_content(content, list(meta["images"]), True)
            elif meta.get("images"):
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
        add_stock = getattr(getattr(ctx, "tts", None), "add_stock", None)
        if callable(add_stock):  # «Сейчас сделаю» — частый ответ: звук готов заранее
            add_stock(lambda: [self._ack_text()])
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

    def account_backend(self, *, routed: bool = True) -> Any:
        """Провайдер без ключа API (ChatGPT или программа человека), если выбран он, иначе None. routed — для
        ответов: пока он на паузе после сбоя (лимит подписки, нет входа, регион) и есть запасной провайдер по
        ключу, — None: отвечает запасной (llm.config)."""
        backend = self.chatgpt if self.chatgpt.selected else self.cli.selected()
        paused = backend is not None and routed and self.llm.outage(self.ctx.settings.value.ai.provider)
        if paused and self.llm.fallback_config() is not None:
            return None
        return backend

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
        set_outgoing(self.ctx.settings.value.ai.mask_secrets)
        self._configured = self.configured
        self._unsubs += [
            self.ctx.settings.on_change(self._on_settings),
            self.ctx.bus.on("secrets.changed", self._on_secrets),
        ]
        await self.mcp_server.start()
        await self.chatgpt.start()
        await self.cli.start()

    def _on_settings(self, old: Any, new: Any) -> None:
        if old.ai.provider != new.ai.provider:
            backend = self.cli.get(new.ai.provider)
            if backend is not None and self.ctx.loop is not None and not self.ctx.loop.is_closed():
                self.ctx.loop.call_soon_threadsafe(backend.on_selected)  # проверить вход в программу
        if old.ai.mask_secrets != new.ai.mask_secrets:
            set_outgoing(new.ai.mask_secrets)
        if old.ai != new.ai:
            self.llm.clear_outages()  # другой провайдер, модель или запасной — основной снова зовётся первым
            self.refresh_configured()

    def _on_secrets(self, _event: Any) -> None:
        self.llm.clear_outages()  # новый ключ — основной снова зовётся первым
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
        backend = self.account_backend(routed=False)
        if backend is not None:
            return list(await backend.list_models())
        return await self.llm.list_models()

    async def test_connection(self) -> dict[str, Any]:
        """Проверка связи с выбранным провайдером: {ok, error?, latency_ms, model}."""
        backend = self.account_backend(routed=False)
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
        agent_task: str = "",
    ) -> AskResult:
        private = PRIVATE_PHRASE.match(text or "") if not agent_task else None
        if private is not None and private["text"].strip(" .,:"):
            return await self._private(private["text"].strip(), chat_id, source, images)
        background = BACKGROUND_PHRASE.match(text or "") if not images and not agent_task else None
        if (
            background is not None
            and background["task"].strip(" .,:")
            and getattr(self.ctx, "background", None)
        ):
            return self._background(text, background["task"].strip(), chat_id, source, mode)
        images, note = await self._screen_question(text, images, chat_id, source, mode, agent_task)
        browser = await self._browser_front(agent_task)
        try:
            turn = self._begin(
                text,
                chat_id=chat_id,
                source=source,
                mode=mode,
                images=images,
                agent_task=agent_task,
                browser=browser,
            )
        except UserFacingError as e:
            return AskResult(chat_id or "", "", error=str(e))
        turn.note = note
        return await self._spawn(turn)

    async def _private(
        self, text: str, chat_id: str | None, source: Source, images: list[str] | None
    ) -> AskResult:
        """«Приватно: …» — ответ в режиме «Чат» моделью на этом ПК: без облака, поиска и серверов MCP. Модели
        на этом ПК нет — понятная ошибка, а не тихий уход в облако."""
        problem = self.llm.private_problem()
        if problem:
            return AskResult(chat_id or "", "", error=problem)
        try:
            turn = self._begin(text, chat_id=chat_id, source=source, mode="chat", images=images, private=True)
        except UserFacingError as e:
            return AskResult(chat_id or "", "", error=str(e))
        return await self._spawn(turn)

    def _background(
        self, text: str, brief: str, chat_id: str | None, source: Source, mode: str | None
    ) -> AskResult:
        """«В фоне: …» — фоновая задача без ответа модели: в чат — просьба и короткое «Принял»."""
        chat = self._chat_for(chat_id, source, mode)
        row = self.chats.add_message(chat["id"], "user", text)
        self.ctx.bus.publish("chat.message", {"chat_id": chat["id"], "message": ui_message(row)})
        kind = {"voice": "voice", "telegram": "remote", "remote": "remote"}.get(str(source), "text")
        meta: dict[str, Any] = {"turn": row["id"]}
        try:
            task = self.ctx.background.create(brief, source=kind)
        except UserFacingError as e:
            reply, spoken, meta["error"] = str(e), "", str(e)
        else:
            meta["background"] = task.id
            address = self.ctx.settings.value.profile.address.strip()
            if task.recurring:
                every = describe(task.repeat)
                reply = (
                    f"Принял — буду делать {every}: «{task.title}». Первый раз — сейчас. Итоги и замеры — "
                    "в заметках, расписание — на странице «Фоновые задачи»."
                )
                spoken = f"Принял{', ' + address if address else ''}. Буду делать {every}."
            else:
                reply = (
                    f"Принял — работаю в фоне: «{task.title}». Ход — на странице «Фоновые задачи», итог "
                    "запишу в заметки."
                )
                spoken = f"Принял{', ' + address if address else ''}. Работаю в фоне."
        answer = self.chats.add_message(chat["id"], "assistant", reply, meta=meta)
        self.ctx.bus.publish("chat.message", {"chat_id": chat["id"], "message": ui_message(answer)})
        self.ctx.bus.publish(
            "chat.done",
            {"chat_id": chat["id"], "message_id": answer["id"], "text": reply, "sources": [], "actions": []},
        )
        return AskResult(chat["id"], reply, spoken=spoken, error=meta.get("error", ""))

    async def _screen_question(
        self,
        text: str,
        images: list[str] | None,
        chat_id: str | None,
        source: Source,
        mode: str | None,
        agent_task: str = "",
    ) -> tuple[list[str] | None, str]:
        """«Что на экране?», «что это за модель?» — снимок экрана к вопросу: модель с картинками ответит за
        один ход, а не агент за много шагов. Модель картинок не видит, чат в режиме «Агент» или снять не
        вышло — как было (агенту)."""
        if images or agent_task or AGENT_PHRASE.match(text or "") or not screen.is_question(text):
            return images, ""
        if self._mode_of(chat_id, source, mode) == "agent":
            return images, ""
        if not self.ctx.settings.value.ai.enabled or not self._sees_images():
            return images, ""
        try:
            pictures, note = await screen.capture(self.ctx)
        except Exception:
            log.exception("снимок экрана к вопросу не сделан")
            return images, ""
        if pictures:
            return pictures, note
        return images, note  # снимка нет; пометка (скрытое от ИИ окно) — модели, чтобы так и сказала

    def _mode_of(self, chat_id: str | None, source: Source, mode: str | None) -> str:
        """Режим, в котором пойдёт ход (как в _begin, но без создания чата)."""
        if mode in MODES:
            return str(mode)
        chat = self.chats.get(chat_id)
        if chat is not None and chat["kind"] != "voice":
            return str(chat["mode"])
        return self.ctx.settings.value.ai.mode

    def _sees_images(self) -> bool:
        """Понимает ли картинки модель, которая ответит в чате (ключ API, Codex или программа человека)."""
        ai = self.ctx.settings.value.ai
        backend = self.account_backend()
        if backend is None:
            try:
                return bool(self.llm.config(None).vision)
            except Exception:
                return False
        info = getattr(backend, "info", None)
        if info is not None and not getattr(info, "images", True):
            return False  # программа без картинок (Qwen Code…): снимок до модели не дойдёт
        model = ai.model
        resolve = getattr(backend, "resolve_model", None)
        if callable(resolve):
            with contextlib.suppress(Exception):
                model = str(resolve() or model)
        return vision.sees_images(ai.provider, model, ai.vision)

    async def _browser_front(self, agent_task: str = "") -> bool:
        """Впереди браузер: просьба о деле на открытой странице уйдёт агенту (apphints.app_task)."""
        if agent_task or not self.ctx.settings.value.ai.auto_agent:
            return False
        try:
            return await apphints.browser_front(self.ctx)
        except Exception:
            log.debug("не узнать, впереди ли браузер", exc_info=True)
            return False

    async def post_message(
        self,
        chat_id: str,
        text: str,
        *,
        images: list[str] | None = None,
        mode: str | None = None,
        source: Source = "ui",
    ) -> dict[str, Any]:
        """Сообщение из окна чата: ответ идёт событиями, вернуть сразу id сообщения. Пока в этом чате работает
        агент, сообщение — ему (ответ на его вопрос или поправка), а не новый ход."""
        self.chats.require(chat_id)
        agent = getattr(self.ctx, "agent", None)
        run = getattr(agent, "current", None)
        working = run is not None and not run.finished and run.chat_id == chat_id and not images
        if working and textops.is_stop(text):
            # «стоп» в чате агента — стоп всему, как голосом, а не поправка к задаче
            row = self.chats.add_message(chat_id, "user", text.strip())
            self.ctx.bus.publish("chat.message", {"chat_id": chat_id, "message": ui_message(row)})
            await self.ctx.dispatcher.stop()
            return {"chat_id": chat_id, "message_id": row["id"], "reply_id": run.reply_id}  # type: ignore[union-attr]
        private = PRIVATE_PHRASE.match(text or "")
        if private is not None and private["text"].strip(" .,:"):
            # «приватно: …» из окна чата — как голосом и в Telegram: только модель на этом ПК, не агенту
            problem = self.llm.private_problem()
            if problem:
                raise UserFacingError(problem)
            turn = self._begin(
                private["text"].strip(),
                chat_id=chat_id,
                source=source,
                mode="chat",
                images=images,
                private=True,
            )
            self.ctx.spawn(self._spawn(turn), f"chat-{turn.chat_id}")
            return {"chat_id": turn.chat_id, "message_id": turn.user_id, "reply_id": turn.reply_id}
        if working and agent.interject(text, source):  # type: ignore[union-attr]
            row = self.chats.add_message(chat_id, "user", text.strip())
            self.ctx.bus.publish("chat.message", {"chat_id": chat_id, "message": ui_message(row)})
            return {"chat_id": chat_id, "message_id": row["id"], "reply_id": run.reply_id}
        images, note = await self._screen_question(text, images, chat_id, source, mode)
        browser = await self._browser_front()
        turn = self._begin(text, chat_id=chat_id, source=source, mode=mode, images=images, browser=browser)
        turn.note = note
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
        agent_task: str = "",
        browser: bool = False,
        private: bool = False,
    ) -> Turn:
        text = (text or "").strip()
        pictures = self._check_images(images)
        if not text and not pictures:
            raise UserFacingError("Пустое сообщение")
        chat = self._chat_for(chat_id, source, mode)
        agent_task = agent_task.strip()
        found = AGENT_PHRASE.match(text) if not private else None  # агент работает облачной моделью
        if not agent_task and found and found["task"].strip():
            agent_task = found["task"].strip()
        if agent_task:
            turn_mode = "agent"
        elif mode in MODES:
            turn_mode = mode
        elif chat["kind"] == "voice":
            turn_mode = self.ctx.settings.value.ai.mode
        else:
            turn_mode = chat["mode"]
        if (
            turn_mode == "all"
            and not pictures
            and self.ctx.settings.value.ai.auto_agent
            and apphints.app_task(text, browser=browser)
        ):
            # «напиши Ане в телеграме…», дело в браузере — для агента: сразу ему, без лишнего ответа модели
            agent_task, turn_mode = text, "agent"
        first = self.ctx.db.query_one(
            "SELECT 1 FROM messages WHERE chat_id=? AND role='user' LIMIT 1", (chat["id"],)
        )
        meta: dict[str, Any] = {"images": pictures} if pictures else {}
        if private:
            meta["private"] = True  # «приватно: …» — в историю для облачной модели не идёт (Ai._history)
        row = self.chats.add_message(chat["id"], "user", text, meta=meta or None)
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
            private=private,
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
        backend = None if turn.private else self.account_backend()
        if backend is not None:
            return await backend.run_chat(turn)
        problem = self.llm.private_problem() if turn.private else self.llm.problem(turn.model or None)
        if problem:
            return self._finish(turn, error=problem)
        self._status(turn, "thinking", "Думаю…")
        tools = self.tools.available(turn.mode, source=turn.source)
        if turn.private:  # только то, что не ходит в интернет
            tools = {k: t for k, t in tools.items() if k not in NETWORK_TOOLS and not k.startswith("mcp__")}
        specs = [t.spec() for t in tools.values()]
        env = ToolEnv(self.ctx, self.web, turn.source, turn.chat_id, user_text=turn.text)
        try:
            apps = await apphints.chat_hints(self.ctx, turn.text, tools)
            messages = self._messages(turn, has_agent_tool="run_agent" in tools, apps=apps)
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
                    private=turn.private,
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

    def _messages(self, turn: Turn, *, has_agent_tool: bool, apps: str = "") -> list[dict[str, Any]]:
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
            apps=apps,
        )
        vision = self.llm.config(turn.model or None).vision
        current = {"role": "user", "content": user_content(turn.prompt, turn.images, vision)}
        history = sanitize_history([*self._history(turn, pictures=vision and not turn.images), current])
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

    def _history(self, turn: Turn, *, pictures: bool = False) -> list[dict[str, Any]]:
        """История чата для модели. pictures — картинки прошлого обмена (снимок экрана к «что это?») остаются
        картинками: в размер истории не считаются, старше — только пометка."""
        groups = group_turns(self.chats.rows(turn.chat_id))
        index = next((i for i, g in enumerate(groups) if g[0]["id"] == turn.user_id), len(groups))
        groups = groups[:index]
        if not turn.private:  # «приватно: …» было только для модели на этом ПК — облаку его не показываем
            groups = [g for g in groups if not g[0]["meta"].get("private")]
        if turn.kind == "voice":
            since = time.time() - VOICE_WINDOW_S
            groups = [g for g in groups if g[0]["created"] >= since][-VOICE_TURNS:]
        selected: list[list[dict[str, Any]]] = []
        budget = HISTORY_CHARS
        for position, group in enumerate(reversed(groups)):
            messages = _group_messages(group, keep_tools=position < 2)
            size = _size(messages)
            if pictures and position == 0:
                messages = _group_messages(group, keep_tools=True, pictures=True)
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
            meta={"turn": turn.user_id, "hidden": True, **({"private": True} if turn.private else {})},
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
                turn.sources.append(
                    {
                        "title": source.get("title") or source["url"],
                        "url": source["url"],
                        # когда получены сведения: свежесть ответа видно рядом со ссылкой
                        "fetched_at": source.get("fetched_at")
                        or datetime.now().isoformat(timespec="seconds"),
                    }
                )
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
                request=turn.text,
            )
        except UserFacingError as e:
            return self._finish(turn, error=str(e))
        turn.finished = True  # итог в чат запишет агент
        if turn.source == "voice":
            await self._agent_ack()
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

    def _ack_text(self) -> str:
        address = self.ctx.settings.value.profile.address.strip()
        return f"Сейчас сделаю, {address}." if address else "Сейчас сделаю."

    async def _agent_ack(self) -> None:
        """Агент работает секунды — с голоса сразу короткий ответ, чтобы не было тишины; итог — в конце."""
        if self.ctx.settings.value.voice.silent_mode:
            return
        try:
            await self.ctx.tts.say(self._ack_text())
        except Exception:
            log.debug("ответ «сейчас сделаю» не прозвучал", exc_info=True)

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
        # знаки ударения («за́мок») модель ставит для голоса: в речь идут из turn.rounds, на экран — без них
        text = strip_accents(text)
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
        if turn.private:
            meta["private"] = True
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
