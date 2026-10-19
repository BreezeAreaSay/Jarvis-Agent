"""Провайдер «ChatGPT (подписка)»: Jarvis AI через Codex app-server и вход в аккаунт ChatGPT.

Один процесс Codex на ядро; запускается лениво (первый вопрос, страница настроек, старт ядра с этим
провайдером) в рабочем потоке. Каждому чату Jarvis — свой тред Codex (id в kv `chatgpt:thread:<chat_id>`;
треды сохраняются в <data>/codex-home/sessions и переживают перезапуск: thread/resume). Не вышло продолжить
тред или изменился набор инструментов — новый тред, а прошлая переписка из нашей базы уходит в него
пересказом.

Инструменты Jarvis (ToolRegistry: команды, поиск, погода, память, заметки, календарь, MCP…) отдаются
треду как dynamicTools: модель вызывает их, Codex присылает item/tool/call, ядро выполняет инструмент тем же
кодом, что и у провайдеров по ключу (карточки chat.tool, подтверждения, источники, «Запомнил…»), и
возвращает результат.
Поиск в интернете — наш web_search/fetch_page (если разрешён в настройках): встроенного поиска Codex модели
GPT-6 в codex 0.163.0 не получают.

Агент (jarvis.ai.agent) с этим провайдером работает ходом Codex в отдельном треде, инструменты — действия
агента (снимок окна, клик, ввод, клавиши…); подтверждения и события agent.* — прежние, из Agent._act.

Ошибки — по-русски (jarvis.ai.chatgpt.errors): регион/VPN, лимит подписки, нет входа, нет связи.
События `ai.chatgpt` {state: installing|installed|login|device|logged_in|logged_out|error, …} — для настроек.
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.ai import apphints
from jarvis.ai.chatgpt import component, errors, prompt
from jarvis.ai.chatgpt.component import SdkInfo
from jarvis.ai.chatgpt.runtime import (
    CodexEvent,
    CodexRuntime,
    Runtime,
    RuntimeConfig,
    RuntimeFactory,
    RuntimeGone,
    RuntimeHooks,
    TurnRun,
    tool_failure,
)
from jarvis.ai.chatgpt.turns import GONE, TurnOutcome, TurnSink, TurnTranslator, markdown_links
from jarvis.ai.llm import BAD_FORMAT, DISABLED, LlmError, LlmFormatError, ToolCall, Usage, extract_json
from jarvis.ai.providers import PRESETS
from jarvis.ai.tools import Tool, ToolEnv
from jarvis.ai.web import site_name
from jarvis.db import new_id
from jarvis.services import AskResult, UserFacingError

if TYPE_CHECKING:
    from jarvis.ai.agent import Agent, AgentRun
    from jarvis.ai.service import Ai, Turn

log = logging.getLogger(__name__)

PROVIDER = "chatgpt"
EVENT = "ai.chatgpt"
KV_THREAD = "chatgpt:thread:"
START_TIMEOUT_S = 45.0
CALL_TIMEOUT_S = 30.0
LOGIN_TIMEOUT_S = 600.0
RETRY_REGION_S = 20.0  # 403 повторяется дольше — значит регион, прерываем
RETRY_NETWORK_S = 30.0  # Codex без сети повторяет минутами («Reconnecting…») — прерываем
INTERRUPT_WAIT_S = 10.0
IDLE_WAIT_S = 10.0
# голосовой чат: тред старше трёх часов не продолжаем (как VOICE_WINDOW_S в jarvis.ai.service)
VOICE_WINDOW_S = 3 * 3600
VOICE_MAX_TURNS = 30
RESUME_AFTER_S = component.THREAD_UNLOAD_S - 120  # тред мог выгрузиться — сначала thread/resume
MAX_SOURCES = 8
TOOL_ORDER_S = 0.02
SUBAGENT_SWEEPS_S = (0.0, 0.5, 2.0)
# Встроенный поиск Codex (web_search="live") вместо нашего web_search. Выключен: в codex 0.163.0 модели
# GPT-5.6+ и GPT-6 (режим кода, в том числе модель по умолчанию) его не получают — ни через config треда, ни
# глобально; у GPT-5.5 он есть. Наш web_search/fetch_page есть у всех моделей, с карточками и «Источниками».
NATIVE_WEB_SEARCH = False
SCREENSHOT_HINT = (
    " Результат — картинка; если ты вызываешь инструменты из exec, это строка с data URL — "
    "передай её в image(), чтобы увидеть снимок."
)
CHAT_MODE_DENIED = (
    "Ошибка: сейчас режим «Чат» — действия на компьютере выключены. Скажи пользователю, что для этого нужно "
    "включить режим «Всё вместе»."
)
PLAN_LABELS = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}


def _uncancel() -> None:
    task = asyncio.current_task()
    if task is not None:
        with contextlib.suppress(Exception):
            task.uncancel()


def _background(fn: Callable[[], Any], name: str) -> None:
    """Блокирующий вызов в фоне, не дожидаясь (interrupt, close, delete)."""

    def run() -> None:
        try:
            fn()
        except Exception as e:
            log.info("%s не прошёл: %s", name, e)

    threading.Thread(target=run, name=f"chatgpt-{name}", daemon=True).start()


async def _blocking(
    fn: Callable[[], Any], timeout: float | None, on_late: Callable[[Any], None] | None = None
) -> Any:
    """fn() в отдельном потоке; ждём не дольше timeout. Опоздавший результат отдаётся on_late."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def deliver(ok: bool, value: Any) -> None:
        if future.done():
            if ok and on_late is not None:
                on_late(value)
            return
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)

    def work() -> None:
        try:
            value, ok = fn(), True
        except BaseException as e:
            value, ok = e, False
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, ok, value)

    threading.Thread(target=work, name="chatgpt-call", daemon=True).start()
    return await asyncio.wait_for(future, timeout)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """JSON Schema для outputSchema (строгий режим OpenAI): у объектов additionalProperties=false и все
    поля в required (необязательные становятся «или null»). None — схема со сложными конструкциями: тогда
    просим JSON словами и разбираем ответ сами."""
    body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
    allowed = {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "description",
        "additionalProperties",
        "anyOf",
    }

    def walk(node: Any) -> Any:
        if not isinstance(node, dict) or set(node) - allowed:
            raise ValueError("schema")
        out = dict(node)
        if "anyOf" in out:
            out["anyOf"] = [walk(n) for n in out["anyOf"]]
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties") or {}
            required = set(out.get("required") or [])
            new_props = {}
            for key, sub in props.items():
                sub = walk(sub)
                if key not in required:
                    sub = {"anyOf": [sub, {"type": "null"}]}
                new_props[key] = sub
            out["properties"] = new_props
            out["required"] = list(props)
            out["additionalProperties"] = False
        if out.get("type") == "array" and "items" in out:
            out["items"] = walk(out["items"])
        return out

    try:
        return walk(body)
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class ChatThread:
    chat_id: str
    thread_id: str
    sig: str
    sent: dict[str, str] = field(default_factory=dict)  # разделы контекста, уже ушедшие в тред
    turns: int = 0
    last_used: float = field(default_factory=time.time)
    history: str = ""  # «Ранее в этом чате» — уходит с первым ходом нового треда
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.idle.set()


@dataclass
class ActiveTurn:
    """Ход, который сейчас идёт в треде: обработчик item/tool/call и задачи инструментов."""

    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    tasks: set["asyncio.Task[Any]"] = field(default_factory=set)
    run: TurnRun | None = None


class ChatSink(TurnSink):
    """Ход Codex → события чата: chat.delta, chat.status, карточки поиска, источники."""

    def __init__(self, ai: "Ai", turn: "Turn") -> None:
        self.ai = ai
        self.turn = turn

    def message_started(self, item_id: str) -> None:
        self.turn.new_round = True

    def delta(self, item_id: str, text: str) -> None:
        self.ai._delta(self.turn, text)

    def message_done(self, item_id: str, text: str, phase: str | None) -> None:
        self.ai._flush(self.turn)
        if text.strip():
            self.turn.rounds.append(text)

    def search(self, item_id: str, query: str, done: bool, urls: list[str]) -> None:
        turn = self.turn
        self.ai._flush(turn)
        call = ToolCall(item_id or new_id(), "web_search", "{}")
        title = f"Поиск: {query}" if query else "Поиск в интернете"
        if not done:
            self.ai._status(turn, "searching", "Ищу актуальную информацию…")
        self.ai._tool_event(turn, call, title, "ok" if done else "running")
        known = {s["url"] for s in turn.sources}
        for url in urls:
            if url not in known and len(turn.sources) < MAX_SOURCES:
                turn.sources.append({"title": site_name(url) or url, "url": url})
                known.add(url)
        if done:
            self.ai._status(turn, "thinking", "Формирую ответ…")

    def retrying(self, error: dict[str, Any]) -> None:
        self.ai._status(self.turn, "thinking", "Нет ответа от ChatGPT, повторяю…")


class ChatGpt:
    """«ChatGPT (подписка)» для Ai: состояние компонента, вход, ходы чата, одиночные запросы, агент."""

    def __init__(self, ai: "Ai") -> None:
        self.ai = ai
        self.ctx = ai.ctx
        self.runtime_factory: RuntimeFactory = CodexRuntime  # тесты подставляют фальшивый Codex
        self.find_sdk: Callable[[], SdkInfo] = component.find_sdk
        self.install_sdk: Callable[[], Awaitable[None]] = component.install
        self._sdk: SdkInfo | None = None
        self._sdk_checked = 0.0
        self._runtime: Runtime | None = None
        self._runtime_key: tuple[Any, ...] | None = None
        self._start_lock: asyncio.Lock | None = None
        self._start_error = ""
        self._installing = False
        self._install_error = ""
        self._account: dict[str, Any] | None = None
        self._account_known = False
        self._login: dict[str, Any] | None = None
        self._login_task: asyncio.Task[Any] | None = None
        self._threads: dict[str, ChatThread] = {}
        self._active: dict[str, ActiveTurn] = {}
        self._models: list[dict[str, Any]] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- состояние --------------------------------------------------------------------------------------
    @property
    def selected(self) -> bool:
        return self.ctx.settings.value.ai.provider == PROVIDER

    def sdk(self, *, refresh: bool = False) -> SdkInfo:
        """Есть ли компонент; без компонента проверка повторяется не чаще раза в 5 с (configured частый)."""
        stale = self._sdk is None or (not self._sdk.installed and time.monotonic() - self._sdk_checked > 5.0)
        if refresh or stale:
            self._sdk_checked = time.monotonic()
            try:
                self._sdk = self.find_sdk()
            except Exception as e:
                log.exception("компонент ChatGPT не проверился")
                self._sdk = SdkInfo(False, error=str(e))
        return self._sdk

    @property
    def installed(self) -> bool:
        if self._sdk is not None and self._sdk.installed:
            return True
        return self.sdk().installed

    def logged_in(self) -> bool:
        """Вход сделан: по account/read, а пока Codex не спрашивали — есть ли auth.json."""
        if self._account_known:
            return self._account is not None
        return component.has_auth_file()

    @property
    def configured(self) -> bool:
        try:
            return self.problem() is None
        except Exception:
            log.exception("состояние ChatGPT не читается")
            return False

    def problem(self) -> str | None:
        """Почему сейчас нельзя спросить ChatGPT (текст для человека) или None."""
        if not self.ctx.settings.value.ai.enabled:
            return DISABLED
        if self._installing:
            return "Компонент ChatGPT ещё скачивается — подождите немного"
        if not self.installed:
            return errors.NOT_INSTALLED
        if not self.logged_in():
            return errors.NOT_LOGGED_IN
        return None

    def account_label(self) -> str:
        account = self._account or {}
        if account.get("type") == "apiKey":
            return "ключ API OpenAI"
        email = str(account.get("email") or "").strip()
        plan = str(account.get("planType") or "").strip()
        plan_label = f"ChatGPT {PLAN_LABELS.get(plan, plan.replace('_', ' ').title())}" if plan else ""
        if email and plan_label:
            return f"{email} ({plan_label})"
        return email or plan_label

    def _set_account(self, account: dict[str, Any] | None) -> None:
        changed = not self._account_known or (self._account is None) != (account is None)
        self._account, self._account_known = account, True
        if changed:
            self.ai.refresh_configured()

    def _publish(self, state: str, **data: Any) -> None:
        self.ctx.bus.publish(EVENT, {"state": state, **data})

    # --- жизненный цикл ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubs.append(self.ctx.settings.on_change(self._on_settings))
        if self.selected and self.ctx.settings.value.ai.enabled and self.installed:
            self.ctx.spawn(self._warm_up(), "chatgpt-warmup")

    async def stop(self) -> None:
        for off in self._unsubs:
            off()
        self._unsubs.clear()
        if self._login_task is not None:
            self._login_task.cancel()
        runtime, self._runtime = self._runtime, None
        self._threads.clear()
        if runtime is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(runtime.close), 5.0)

    def _on_settings(self, old: Any, new: Any) -> None:
        if old.ai.provider == new.ai.provider and old.ai.enabled == new.ai.enabled:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if new.ai.provider == PROVIDER and new.ai.enabled:
            loop.call_soon_threadsafe(lambda: self.ctx.spawn(self._warm_up(), "chatgpt-warmup"))
        elif old.ai.provider == PROVIDER and not self._active:
            runtime, self._runtime = self._runtime, None
            self._threads.clear()
            if runtime is not None:
                _background(runtime.close, "close")

    async def _warm_up(self) -> None:
        """Codex заранее: первый вопрос не ждёт запуска; заодно — вход и список моделей."""
        if not self.installed:
            return
        try:
            runtime = await self._runtime_for_use()
            self._set_account(await asyncio.wait_for(asyncio.to_thread(runtime.account), CALL_TIMEOUT_S))
            await self._load_models(runtime)
        except Exception as e:
            log.info("ChatGPT не прогрелся: %s", e)

    def _config_key(self) -> tuple[Any, ...]:
        env = component.codex_env(self.ctx.settings.value.ai)
        return (component.build_overrides(), tuple(sorted(env.items())))

    async def _runtime_for_use(self) -> Runtime:
        """Codex запущен (или запустится сейчас, в рабочем потоке); ошибка — UserFacingError."""
        if not self.installed:
            raise UserFacingError(errors.NOT_INSTALLED)
        key = self._config_key()
        runtime = self._runtime
        if runtime is not None and runtime.alive and (self._runtime_key == key or self._active):
            return runtime
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            runtime = self._runtime
            if runtime is not None and runtime.alive and (self._runtime_key == key or self._active):
                return runtime
            if runtime is not None:
                log.info(
                    "перезапускаю Codex (%s)", "изменились настройки" if runtime.alive else "процесс умер"
                )
                self._runtime = None
                self._threads.clear()
                _background(runtime.close, "close")
            self._loop = asyncio.get_running_loop()
            ai = self.ctx.settings.value.ai
            config = RuntimeConfig(
                cwd=str(component.work_dir()),
                env=component.codex_env(ai),
                overrides=component.build_overrides(),
            )
            fresh = self.runtime_factory(config, self._hooks())
            started = time.perf_counter()
            try:
                await _blocking(fresh.start, START_TIMEOUT_S, on_late=lambda _: fresh.close())
            except TimeoutError:
                _background(fresh.close, "close")
                self._start_error = errors.START.format(detail=f"нет ответа за {START_TIMEOUT_S:.0f} с")
                raise UserFacingError(self._start_error) from None
            except Exception as e:
                log.warning("Codex не запустился: %s", e)
                _background(fresh.close, "close")
                reason, text = errors.explain_exception(e, starting=True)
                if reason == "not_installed":
                    self._sdk = None
                if reason in ("start", "config") and not str(component.codex_home()).isascii():
                    text += errors.NON_ASCII_HINT
                self._start_error = text
                raise UserFacingError(text) from None
            log.info("Codex запущен за %.2f с (pid %s)", time.perf_counter() - started, fresh.pid)
            self._runtime, self._runtime_key, self._start_error = fresh, key, ""
            self._threads.clear()
            return fresh

    def _hooks(self) -> RuntimeHooks:
        return RuntimeHooks(on_tool_call=self._tool_call_threadsafe, on_notification=self._notify_threadsafe)

    def _notify_threadsafe(self, event: CodexEvent) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._on_notification, event)

    def _on_notification(self, event: CodexEvent) -> None:
        method, params = event.method, event.params
        if method == "account/updated":
            self.ctx.spawn(self._refresh_account(), "chatgpt-account")
        elif method == "configWarning":
            log.warning("Codex: %s %s", params.get("summary"), params.get("details") or "")
        elif method in ("warning", "deprecationNotice"):
            log.info("Codex: %s", params.get("message") or params.get("summary"))
        elif method == "jarvis/closed":
            log.warning("процесс Codex завершился: %s", errors.short(params.get("error"), 200))

    async def _refresh_account(self) -> None:
        runtime = self._runtime
        if runtime is None or not runtime.alive:
            return
        with contextlib.suppress(Exception):
            self._set_account(await asyncio.wait_for(asyncio.to_thread(runtime.account), CALL_TIMEOUT_S))

    async def _load_models(self, runtime: Runtime) -> list[dict[str, Any]]:
        models = await asyncio.wait_for(asyncio.to_thread(runtime.models), CALL_TIMEOUT_S)
        self._models = [m for m in models if isinstance(m, dict) and not m.get("hidden")]
        return self._models

    # --- модели --------------------------------------------------------------------------------------
    def model_ids(self) -> list[str]:
        if self._models:
            return [str(m.get("id") or m.get("model")) for m in self._models if m.get("id") or m.get("model")]
        return list(PRESETS[PROVIDER].models)

    def default_model(self) -> str:
        for model in self._models or []:
            if model.get("isDefault"):
                return str(model.get("id") or model.get("model") or "")
        return PRESETS[PROVIDER].default_model

    def resolve_model(self, requested: str | None = None) -> str:
        """Модель хода: из чата или настроек, если её знает Codex; иначе — модель Codex по умолчанию."""
        name = (requested or "").strip() or self.ctx.settings.value.ai.model.strip()
        return name if name in self.model_ids() else self.default_model()

    def _default_effort(self, model: str) -> str:
        for item in self._models or []:
            if item.get("id") == model and isinstance(item.get("defaultReasoningEffort"), str):
                return str(item["defaultReasoningEffort"])
        return "medium"

    def _supported(self, model: str, wanted: str) -> str:
        """Усилие, если модель его знает (по каталогу Codex); иначе — её умолчание."""
        for item in self._models or []:
            if item.get("id") != model:
                continue
            raw = item.get("supportedReasoningEfforts")
            if isinstance(raw, list) and raw:
                names = {str(e.get("reasoningEffort") if isinstance(e, dict) else e) for e in raw}
                if wanted not in names:
                    return self._default_effort(model)
        return wanted

    def _effort(self, model: str, source: str) -> str:
        """Сколько думать в чате: «Быстро» и голос — low, «Тщательно» — high, иначе — умолчание модели."""
        speed = self.ctx.settings.value.ai.speed
        if speed == "fast" or source in ("voice", "remote", "telegram"):
            return self._supported(model, "low")
        if speed == "deep":
            return self._supported(model, "high")
        return self._default_effort(model)

    def _agent_effort(self, model: str) -> str:
        speed = self.ctx.settings.value.ai.speed
        return self._supported(model, {"fast": "low", "deep": "high"}.get(speed, "medium"))

    def preset_json(self, base: dict[str, Any]) -> dict[str, Any]:
        """Пресет для GET /api/ai/providers: модели и умолчание — из каталога Codex (если есть)."""
        out = dict(base)
        if self._models:
            out["models"] = self.model_ids()
            out["default_model"] = self.default_model()
        return out

    async def list_models(self) -> list[str]:
        if not self.installed:
            return self.model_ids()
        try:
            runtime = await self._runtime_for_use()
            await self._load_models(runtime)
        except Exception as e:
            log.info("список моделей Codex не получен: %s", e)
        return self.model_ids()

    async def test(self) -> dict[str, Any]:
        """Проверка связи: крошечный ход. {ok, error?, latency_ms, model}."""
        model = self.resolve_model()
        started = time.perf_counter()
        try:
            await self.complete([{"role": "user", "content": "Ответь одним словом: готов."}])
        except UserFacingError as e:
            latency = int((time.perf_counter() - started) * 1000)
            return {"ok": False, "error": str(e), "latency_ms": latency, "model": model}
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "model": model}

    # --- компонент и вход (API настроек) -------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        info = self.sdk()
        out: dict[str, Any] = {
            "installed": info.installed,
            "installing": self._installing,
            "logged_in": False,
            "account": "",
            "error": "",
            "version": info.version,
        }
        if self._installing:
            return out
        if not info.installed:
            out["error"] = self._install_error or info.error
            return out
        try:
            runtime = await self._runtime_for_use()
            self._set_account(await asyncio.wait_for(asyncio.to_thread(runtime.account), CALL_TIMEOUT_S))
        except UserFacingError as e:
            out["error"] = str(e)
        except TimeoutError:
            out["error"] = errors.START.format(detail="не отвечает")
        except Exception as e:
            out["error"] = errors.explain_exception(e)[1]
        out["logged_in"] = self.logged_in()
        out["account"] = self.account_label() if out["logged_in"] else ""
        login = self._login
        if login is not None and not out["logged_in"]:
            out["login_pending"] = True
            for key in ("auth_url", "verification_url", "user_code"):
                if login.get(key):
                    out[key] = login[key]
        return out

    async def install(self) -> dict[str, Any]:
        """Скачать компонент в фоне; ход — события ai.chatgpt."""
        if self.sdk(refresh=True).installed:
            return {"ok": True, "installed": True}
        if not self._installing:
            self._installing = True
            self._install_error = ""
            self._publish("installing")
            self.ctx.spawn(self._install(), "chatgpt-install")
        return {"ok": True, "installing": True}

    async def _install(self) -> None:
        try:
            await self.install_sdk()
            info = self.sdk(refresh=True)
            if not info.installed:
                raise UserFacingError(
                    info.error or "компонент скачался, но не находится — перезапустите Jarvis"
                )
        except Exception as e:
            self._installing = False
            self._install_error = str(e) if isinstance(e, UserFacingError) else f"Не удалось скачать: {e}"
            log.warning("компонент ChatGPT не поставился: %s", e)
            self._publish("error", error=self._install_error)
            self.ai.refresh_configured()
            return
        self._installing = False
        log.info("компонент ChatGPT поставлен (openai-codex %s)", info.version)
        self._publish("installed", version=info.version)
        self.ai.refresh_configured()
        if self.selected:
            await self._warm_up()  # прежний вход из <data>/codex-home подхватится сам

    async def login(self, device: bool = False) -> dict[str, Any]:
        """Вход в ChatGPT: браузер (или код для другого устройства). Ждём завершения в фоне."""
        if not self.installed:
            raise UserFacingError(errors.NOT_INSTALLED)
        runtime = await self._runtime_for_use()
        try:
            account = await asyncio.wait_for(asyncio.to_thread(runtime.account), CALL_TIMEOUT_S)
        except Exception as e:
            raise UserFacingError(errors.explain_exception(e)[1]) from None
        self._set_account(account)
        if account is not None:
            self._publish("logged_in", account=self.account_label())
            return {"ok": True, "logged_in": True, "account": self.account_label()}
        await self._cancel_login(runtime)
        try:
            data = await asyncio.wait_for(asyncio.to_thread(runtime.login_start, device), CALL_TIMEOUT_S)
        except Exception as e:
            text = errors.explain_exception(e)[1]
            self._publish("error", error=f"Вход не начался: {text}")
            raise UserFacingError(f"Вход не начался: {text}") from None
        login_id = str(data.get("loginId") or "")
        login: dict[str, Any] = {"id": login_id, "device": device, "started": time.time()}
        if device:
            login["verification_url"] = str(data.get("verificationUrl") or "")
            login["user_code"] = str(data.get("userCode") or "")
            url = login["verification_url"]
        else:
            login["auth_url"] = str(data.get("authUrl") or "")
            url = login["auth_url"]
        self._login = login
        opened = False
        if url:
            try:
                await self.ctx.run_platform(self.ctx.platform.open_url, url)
                opened = True
            except Exception as e:
                log.warning("браузер для входа не открылся: %s", e)
        public = {k: login[k] for k in ("auth_url", "verification_url", "user_code") if login.get(k)}
        self._publish("device" if device else "login", opened=opened, **public)
        self._login_task = self.ctx.spawn(self._wait_login(runtime, login), "chatgpt-login")
        return {"ok": True, "logged_in": False, "opened": opened, **public}

    async def _cancel_login(self, runtime: Runtime) -> None:
        login, self._login = self._login, None
        if self._login_task is not None:
            self._login_task.cancel()
            self._login_task = None
        if login and login.get("id"):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(runtime.login_cancel, login["id"]), 5.0)

    async def cancel_login(self) -> dict[str, Any]:
        runtime = self._runtime
        if runtime is not None:
            await self._cancel_login(runtime)
        else:
            self._login = None
        return {"ok": True}

    async def _wait_login(self, runtime: Runtime, login: dict[str, Any]) -> None:
        try:
            done = await _blocking(lambda: runtime.login_wait(login["id"]), LOGIN_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        except TimeoutError:
            if self._login is login:
                self._login = None
            with contextlib.suppress(Exception):
                await asyncio.to_thread(runtime.login_cancel, login["id"])
            self._publish("error", error="Вход в ChatGPT не завершён за 10 минут — попробуйте ещё раз")
            return
        except Exception as e:
            if self._login is login:
                self._login = None
            self._publish("error", error=f"Вход не выполнен: {errors.explain_exception(e)[1]}")
            return
        if self._login is login:
            self._login = None
        if not done.get("success"):
            reason = errors.short(done.get("error") or "вход отменён", 200)
            self._publish("error", error=f"Вход не выполнен: {reason}")
            return
        try:
            account = await asyncio.wait_for(asyncio.to_thread(runtime.account), CALL_TIMEOUT_S)
        except Exception:
            account = {"type": "chatgpt"}
        self._set_account(account or {"type": "chatgpt"})
        log.info("вход в ChatGPT выполнен")
        self._publish("logged_in", account=self.account_label())

    async def logout(self) -> dict[str, Any]:
        if self.installed:
            runtime = await self._runtime_for_use()
            await self._cancel_login(runtime)
            try:
                await asyncio.wait_for(asyncio.to_thread(runtime.logout), CALL_TIMEOUT_S)
            except Exception as e:
                raise UserFacingError(f"Выйти не получилось: {errors.explain_exception(e)[1]}") from None
        else:
            with contextlib.suppress(OSError):
                component.auth_file().unlink()
        self._set_account(None)
        self._publish("logged_out")
        return {"ok": True}

    # --- инструменты для Codex (item/tool/call) -------------------------------------------------------
    def _tool_call_threadsafe(
        self, params: dict[str, Any], respond: Callable[[dict[str, Any]], None]
    ) -> None:
        """Из потока чтения Codex: выполнить инструмент в цикле ядра, ответить, когда будет готово."""
        loop = self._loop
        if loop is None or loop.is_closed():
            respond(tool_failure("Ошибка: Jarvis закрывается"))
            return
        future = asyncio.run_coroutine_threadsafe(self._tool_call(params), loop)

        def done(f: Any) -> None:
            try:
                result = f.result()
            except BaseException as e:
                result = tool_failure(f"Ошибка: {e or type(e).__name__}")
            respond(result)

        future.add_done_callback(done)

    async def _tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        active = self._active.get(str(params.get("threadId") or ""))
        if active is None:
            return tool_failure("Ошибка: ход уже закончился")
        task = asyncio.current_task()
        if task is not None:
            active.tasks.add(task)
        try:
            return await active.handler(params)
        except asyncio.CancelledError:
            _uncancel()
            return tool_failure("Остановлено пользователем")
        except Exception as e:
            log.exception("инструмент для Codex упал")
            return tool_failure(f"Ошибка: внутренняя ошибка Jarvis: {e}")
        finally:
            if task is not None:
                active.tasks.discard(task)

    @staticmethod
    def _call_of(params: dict[str, Any]) -> ToolCall:
        args = params.get("arguments")
        if isinstance(args, str):
            raw = args
        else:
            raw = json.dumps(args if args is not None else {}, ensure_ascii=False)
        return ToolCall(str(params.get("callId") or ("call_" + new_id())), str(params.get("tool") or ""), raw)

    def _chat_tools(self, source: str) -> tuple[dict[str, Tool], bool]:
        """Инструменты треда чата (режим «Всё вместе») и включён ли встроенный поиск Codex."""
        tools = self.ai.tools.available("all", source=source)
        native = NATIVE_WEB_SEARCH and self.ctx.settings.value.ai.web_search
        if native:
            tools.pop("web_search", None)
        return tools, native

    @staticmethod
    def _dynamic_specs(tools: dict[str, Tool]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in sorted(tools.values(), key=lambda t: t.name)
        ]

    @staticmethod
    def _signature(specs: list[dict[str, Any]], native_search: bool, kind: str) -> str:
        import hashlib

        raw = json.dumps([specs, native_search, kind], ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    async def _chat_tool(
        self, turn: "Turn", env: ToolEnv, allowed: dict[str, Tool], everything: dict[str, Tool], params: dict
    ) -> dict[str, Any]:
        ai = self.ai
        # текст до вызова идёт событиями хода через другой поток — пусть он дойдёт раньше карточки
        await asyncio.sleep(TOOL_ORDER_S)
        ai._flush(turn)
        call = self._call_of(params)
        ai.chats.add_message(
            turn.chat_id,
            "assistant",
            "",
            tool_calls=[call.to_message()],
            meta={"turn": turn.user_id, "hidden": True},
        )
        if call.name in everything and call.name not in allowed:
            content = CHAT_MODE_DENIED
            ai._save_tool_result(turn, call, content)
        else:
            content = await ai._call_tool(turn, env, allowed, call)
        ai._status(turn, "thinking", "Формирую ответ…")
        return {
            "contentItems": [{"type": "inputText", "text": content}],
            "success": not content.startswith("Ошибка"),
        }

    # --- треды чатов ---------------------------------------------------------------------------------
    def _thread_params(
        self, specs: list[dict[str, Any]], native_search: bool, developer: str, model: str
    ) -> dict[str, Any]:
        return {
            "model": model,  # = модель хода, иначе Codex добавит <model_switch> (~20 тыс. знаков)
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "cwd": str(component.work_dir()),
            "ephemeral": False,
            "developerInstructions": developer,
            "dynamicTools": specs,
            "config": component.thread_config(web_search=native_search),
        }

    def _resume_params(self, native_search: bool) -> dict[str, Any]:
        return {
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "cwd": str(component.work_dir()),
            "excludeTurns": True,
            "config": component.thread_config(web_search=native_search),
        }

    def _stale(self, kind: str, last_used: float, turns: int) -> bool:
        if kind != "voice":
            return False
        return time.time() - last_used > VOICE_WINDOW_S or turns >= VOICE_MAX_TURNS

    async def _chat_thread(
        self,
        runtime: Runtime,
        turn: "Turn",
        specs: list[dict[str, Any]],
        native: bool,
        sig: str,
        model: str,
    ) -> ChatThread:
        """Тред Codex для чата: загруженный, сохранённый (thread/resume) или новый (с пересказом истории)."""
        state = self._threads.get(turn.chat_id)
        busy = ""
        if (
            state is not None
            and state.sig == sig
            and not self._stale(turn.kind, state.last_used, state.turns)
        ):
            if not state.idle.is_set():
                try:
                    await asyncio.wait_for(state.idle.wait(), IDLE_WAIT_S)
                except TimeoutError:
                    busy, state = state.thread_id, None  # прошлый ход не закрылся — новый тред
            if state is not None:
                return state
        self._threads.pop(turn.chat_id, None)
        key = KV_THREAD + turn.chat_id
        saved = self.ctx.db.kv_get(key)
        old_id = ""
        if isinstance(saved, dict) and saved.get("id"):
            old_id = str(saved["id"])
            fresh_enough = not self._stale(
                turn.kind, float(saved.get("updated") or 0), int(saved.get("turns") or 0)
            )
            if saved.get("sig") == sig and fresh_enough and old_id != busy:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(runtime.thread_resume, old_id, self._resume_params(native)),
                        CALL_TIMEOUT_S,
                    )
                except Exception as e:
                    log.info("тред %s не продолжился (%s) — новый тред", old_id, e)
                else:
                    state = ChatThread(turn.chat_id, old_id, sig, turns=int(saved.get("turns") or 0))
                    self._threads[turn.chat_id] = state
                    return state
        thread = await asyncio.wait_for(
            asyncio.to_thread(
                runtime.thread_start,
                self._thread_params(specs, native, prompt.developer_instructions(), model),
            ),
            CALL_TIMEOUT_S,
        )
        state = ChatThread(turn.chat_id, str(thread["id"]), sig, history=self._history(turn))
        self._threads[turn.chat_id] = state
        self.ctx.db.kv_set(
            key,
            {
                "id": state.thread_id,
                "sig": sig,
                "path": thread.get("path"),
                "updated": time.time(),
                "turns": 0,
            },
        )
        if old_id and old_id != state.thread_id:
            self._delete_thread(old_id, saved.get("path") if isinstance(saved, dict) else None)
        return state

    def _history(self, turn: "Turn") -> str:
        try:
            messages = self.ai._history(turn)
        except Exception:
            log.exception("история чата не собралась")
            return ""
        name = self.ctx.settings.value.profile.assistant_name.strip() or "Джарвис"
        return prompt.history_text(messages, name)

    def _touch(self, state: ChatThread) -> None:
        state.turns += 1
        state.last_used = time.time()
        key = KV_THREAD + state.chat_id
        saved = self.ctx.db.kv_get(key)
        if isinstance(saved, dict) and saved.get("id") == state.thread_id:
            saved.update(updated=state.last_used, turns=state.turns)
            self.ctx.db.kv_set(key, saved)

    def _delete_thread(self, thread_id: str, path: Any = None) -> None:
        """Удалить тред Codex (переписка с диска codex-home): через Codex, а без него — файл разговора."""
        runtime = self._runtime
        if runtime is not None and runtime.alive:
            _background(lambda: runtime.thread_delete(thread_id), "thread-delete")
            return
        if isinstance(path, str) and path:
            target = Path(path)
            with contextlib.suppress(OSError, ValueError):
                target.resolve().relative_to(component.codex_home().resolve())
                target.unlink()

    def forget_chat(self, chat_id: str) -> None:
        """Чат удалён: его тред Codex — тоже."""
        self._threads.pop(chat_id, None)
        key = KV_THREAD + chat_id
        saved = self.ctx.db.kv_get(key)
        if isinstance(saved, dict) and saved.get("id"):
            self.ctx.db.delete("kv", key, key="key")
            self._delete_thread(str(saved["id"]), saved.get("path"))

    # --- ход ----------------------------------------------------------------------------------------
    @staticmethod
    def _items(text: str, images: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [{"type": "text", "text": text or "Что на изображении?"}]
        items += [{"type": "image", "url": url} for url in images]
        return items

    async def _start_turn(
        self,
        runtime: Runtime,
        thread_id: str,
        items: list[dict[str, Any]],
        params: dict[str, Any],
        resume: dict[str, Any] | None = None,
    ) -> TurnRun:
        """turn/start; тред выгружен из памяти Codex — thread/resume и ещё раз."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(runtime.turn_start, thread_id, items, params), CALL_TIMEOUT_S
            )
        except RuntimeGone:
            raise
        except Exception as e:
            if resume is None or "thread" not in str(e).casefold():
                raise
            log.info("тред %s не найден в Codex (%s) — thread/resume", thread_id, e)
        await asyncio.wait_for(asyncio.to_thread(runtime.thread_resume, thread_id, resume), CALL_TIMEOUT_S)
        return await asyncio.wait_for(
            asyncio.to_thread(runtime.turn_start, thread_id, items, params), CALL_TIMEOUT_S
        )

    @staticmethod
    async def _start_shielded(start: Awaitable[TurnRun]) -> TurnRun:
        """Старт хода не обрывается посреди запроса: при «Стопе» ход, успевший начаться, сразу прерывается."""
        task = asyncio.ensure_future(start)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            def stop_late(t: "asyncio.Future[TurnRun]") -> None:
                if not t.cancelled() and t.exception() is None:
                    _background(t.result().interrupt, "interrupt")

            task.add_done_callback(stop_late)
            raise

    async def _consume(
        self, run: TurnRun, sink: TurnSink | None, on_done: Callable[[], None] | None = None
    ) -> TurnOutcome:
        """События хода → TurnOutcome. Повторы Codex ограничены по времени (регион — 20 с, сеть — 30 с)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[CodexEvent] = asyncio.Queue()

        def put(event: CodexEvent) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, event)

        def pump() -> None:
            while True:
                try:
                    event = run.next_event()
                except Exception as e:
                    put(CodexEvent(GONE, {"error": str(e) or type(e).__name__}))
                    break
                put(event)
                if event.method == "turn/completed":
                    break
            run.close()
            if on_done is not None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(on_done)

        threading.Thread(target=pump, name="chatgpt-turn", daemon=True).start()
        translator = TurnTranslator(sink)
        out = translator.outcome
        retry_since: float | None = None
        deadline: float | None = None
        while True:
            timeout = None
            if deadline is not None:
                timeout = max(0.05, deadline - time.monotonic())
            elif out.retry_error is not None and not out.abort_reason:
                if retry_since is None:
                    retry_since = time.monotonic()
                region = errors.classify_turn_error(out.retry_error)[0] == "region"
                limit = RETRY_REGION_S if region else RETRY_NETWORK_S
                timeout = max(0.05, retry_since + limit - time.monotonic())
            try:
                event = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                if deadline is not None:
                    return out  # прервали, а Codex так и не закрыл ход
                region = errors.classify_turn_error(out.retry_error)[0] == "region"
                out.abort_reason = "region" if region else "network"
                _background(run.interrupt, "interrupt")
                deadline = time.monotonic() + INTERRUPT_WAIT_S
                continue
            if translator.feed(event):
                return out
            if out.abort_reason == "subagent" and deadline is None:
                log.warning("Codex запускает под-агента — прерываю ход")
                _background(run.interrupt, "interrupt")
                self.ctx.spawn(self._stop_subagents(), "chatgpt-subagents")
                deadline = time.monotonic() + INTERRUPT_WAIT_S

    async def _stop_subagents(self) -> None:
        """Прервать ходы тредов, которые Jarvis не запускал (под-агенты Codex): прерывание родителя их не
        останавливает (проверено на 0.163.0). Повторяем — под-агент мог стартовать чуть позже."""
        for delay in SUBAGENT_SWEEPS_S:
            await asyncio.sleep(delay)
            runtime = self._runtime
            if runtime is None or not runtime.alive:
                return
            for thread_id, turn_id in runtime.running_turns().items():
                if thread_id not in self._active:
                    log.warning("прерываю под-агента Codex (тред %s)", thread_id)
                    _background(
                        lambda r=runtime, t=thread_id, u=turn_id: r.interrupt(t, u), "interrupt-subagent"
                    )

    def _usage(self, out: TurnOutcome, model: str) -> Usage:
        usage = Usage(prompt_tokens=out.input_tokens, completion_tokens=out.output_tokens, cost=0.0)
        if out.input_tokens or out.output_tokens:
            try:
                self.ctx.db.insert(
                    "usage",
                    {
                        "ts": time.time(),
                        "provider": PROVIDER,
                        "model": model,
                        "prompt_tokens": out.input_tokens,
                        "completion_tokens": out.output_tokens,
                        "cost": 0.0,
                    },
                )
            except Exception:
                log.exception("расход токенов не записан")
        return usage

    def _failure(self, out: TurnOutcome, model: str) -> str:
        """Текст ошибки неудачного хода."""
        if out.abort_reason == "subagent":
            return errors.SUBAGENT
        if out.abort_reason == "region":
            return errors.REGION
        if out.abort_reason == "network":
            detail = errors.short((out.retry_error or {}).get("message") or errors.NO_DETAILS, 160)
            return errors.NETWORK.format(detail=detail)
        if out.status == "gone":
            return errors.DEAD
        reason, text = errors.classify_turn_error(out.error or out.retry_error, model=model)
        if reason == "auth":
            self.ctx.spawn(self._refresh_account(), "chatgpt-account")
        return text

    # --- чат ----------------------------------------------------------------------------------------
    async def run_chat(self, turn: "Turn") -> AskResult:
        """Ответ в чате ходом Codex: те же события chat.*, что у провайдеров по ключу."""
        ai = self.ai
        problem = self.problem()
        if problem:
            return ai._finish(turn, error=problem)
        ai._status(turn, "thinking", "Думаю…")
        state: ChatThread | None = None
        active: ActiveTurn | None = None
        run: TurnRun | None = None
        model = self.resolve_model(turn.model or None)
        try:
            runtime = await self._runtime_for_use()
            everything, native = self._chat_tools(turn.source)
            specs = self._dynamic_specs(everything)
            sig = self._signature(specs, native, "chat")
            state = await self._chat_thread(runtime, turn, specs, native, sig, model)
            allowed = ai.tools.available(turn.mode, source=turn.source)
            if native:
                allowed.pop("web_search", None)
            env = ToolEnv(self.ctx, ai.web, turn.source, turn.chat_id, user_text=turn.text)

            async def handler(params: dict[str, Any]) -> dict[str, Any]:
                return await self._chat_tool(turn, env, allowed, everything, params)

            active = ActiveTurn(handler)
            self._active[state.thread_id] = active
            memory = ""
            if self.ctx.settings.value.ai.memory:
                try:
                    memory = self.ctx.notes.memory_prompt()
                except Exception:
                    log.exception("память не прочиталась")
            sections = prompt.context_sections(
                self.ctx.settings.value,
                source=turn.source,
                mode=turn.mode,
                memory=memory,
                has_agent_tool="run_agent" in allowed,
                apps=await apphints.chat_hints(self.ctx, turn.text, allowed),
            )
            sent = dict(state.sent)
            context: dict[str, Any] = {
                "jarvis": {"kind": "application", "value": prompt.context_text(sections, sent)}
            }
            if state.history:
                context["history"] = {"kind": "untrusted", "value": state.history}
            params: dict[str, Any] = {
                "model": model,
                "effort": self._effort(model, turn.source),
                "additionalContext": context,
            }
            if time.time() - state.last_used > RESUME_AFTER_S and state.turns:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            runtime.thread_resume, state.thread_id, self._resume_params(native)
                        ),
                        CALL_TIMEOUT_S,
                    )
            state.idle.clear()
            run = await self._start_shielded(
                self._start_turn(
                    runtime,
                    state.thread_id,
                    self._items(turn.text, turn.images),
                    params,
                    self._resume_params(native),
                )
            )
            active.run = run
            state.sent, state.history = sent, ""
            out = await self._consume(run, ChatSink(ai, turn), on_done=state.idle.set)
        except asyncio.CancelledError:
            _uncancel()
            self._abandon(state, active, run)
            return ai._finish(turn, stopped=True)
        except UserFacingError as e:
            self._abandon(state, active, run)
            return ai._finish(turn, error=str(e))
        except RuntimeGone:
            self._abandon(state, active, run)
            return ai._finish(turn, error=errors.DEAD)
        except Exception as e:
            log.exception("ход ChatGPT упал")
            self._abandon(state, active, run)
            return ai._finish(turn, error=errors.explain_exception(e, model=model)[1])
        finally:
            if state is not None and self._active.get(state.thread_id) is active:
                self._active.pop(state.thread_id, None)
        self._touch(state)
        turn.usage.add(self._usage(out, model))
        if out.searched:
            known = {s["url"] for s in turn.sources}
            for title, url in markdown_links(out.text):
                if url not in known and len(turn.sources) < MAX_SOURCES:
                    turn.sources.append({"title": title or site_name(url), "url": url})
                    known.add(url)
        if out.status == "completed" and not out.abort_reason:
            return ai._finish(turn)
        if out.status == "interrupted" and not out.abort_reason and out.error is None:
            return ai._finish(turn, stopped=True)
        if out.status == "gone":
            self._threads.pop(turn.chat_id, None)
        return ai._finish(turn, error=self._failure(out, model))

    def _abandon(self, state: ChatThread | None, active: ActiveTurn | None, run: TurnRun | None) -> None:
        """Ход брошен (Стоп, ошибка): прервать в Codex, отменить инструменты."""
        if active is not None:
            for task in list(active.tasks):
                task.cancel()
        if run is not None:
            _background(run.interrupt, "interrupt")
        elif state is not None:
            state.idle.set()

    # --- одиночный запрос ---------------------------------------------------------------------------
    @staticmethod
    def _complete_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Сообщения OpenAI → (инструкции, вход хода)."""
        system: list[str] = []
        dialog: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    system.append(content.strip())
            elif message.get("role") in ("user", "assistant"):
                dialog.append(message)
        images: list[str] = []

        def text_of(content: Any) -> str:
            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url")
                        if isinstance(url, str) and url.startswith("data:image/"):
                            images.append(url)
                    elif isinstance(part.get("text"), str):
                        parts.append(part["text"])
                return "\n".join(parts)
            return str(content or "")

        if len(dialog) == 1:
            text = text_of(dialog[0].get("content"))
        else:
            lines = []
            for message in dialog[:-1]:
                who = "Пользователь" if message["role"] == "user" else "Ассистент"
                lines.append(f"{who}: {text_of(message.get('content'))}")
            last = text_of(dialog[-1].get("content")) if dialog else ""
            text = ("Предыдущие сообщения:\n" + "\n".join(lines) + "\n\n" + last) if lines else last
        developer = "\n\n".join(system) or "Отвечай по-русски, кратко и по делу."
        developer += "\n\nОтвечай сразу текстом: инструментов в этом запросе нет."
        items: list[dict[str, Any]] = [{"type": "text", "text": text}]
        items += [{"type": "image", "url": url} for url in images]
        return developer, items

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage: Usage | None = None,
    ) -> Any:
        """Одиночный ход без инструментов во временном треде. С json_schema — dict (outputSchema Codex)."""
        problem = self.problem()
        if problem:
            raise LlmError(problem)
        runtime = await self._runtime_for_use()
        developer, items = self._complete_input(messages)
        model = self.resolve_model()
        schema = strict_schema(json_schema) if json_schema is not None else None
        if json_schema is not None and schema is None:
            body = json_schema.get("schema") if isinstance(json_schema.get("schema"), dict) else json_schema
            items[0]["text"] += (
                "\n\nОтветь только одним JSON-объектом без пояснений и без ```. Схема: "
                + json.dumps(body, ensure_ascii=False)
            )
        out = await self._once(runtime, developer, items, model, schema)
        if (
            out.status != "completed"
            and schema is not None
            and "schema" in json.dumps(out.error or {}).casefold()
        ):
            log.info("outputSchema не принята (%s) — прошу JSON словами", out.error)
            items[0]["text"] += (
                "\n\nОтветь только одним JSON-объектом без пояснений и без ```. Схема: "
                + json.dumps(json_schema, ensure_ascii=False)
            )
            out = await self._once(runtime, developer, items, model, None)
        spent = self._usage(out, model)
        if usage is not None:
            usage.add(spent)
        if out.status != "completed":
            raise LlmError(self._failure(out, model))
        text = out.final
        if json_schema is None:
            return text
        parsed = extract_json(text)
        if not isinstance(parsed, dict):
            raise LlmFormatError(BAD_FORMAT)
        return parsed

    async def _once(
        self,
        runtime: Runtime,
        developer: str,
        items: list[dict[str, Any]],
        model: str,
        schema: dict[str, Any] | None,
    ) -> TurnOutcome:
        thread = await asyncio.wait_for(
            asyncio.to_thread(
                runtime.thread_start,
                {
                    "model": model,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "cwd": str(component.work_dir()),
                    "ephemeral": True,
                    "developerInstructions": developer,
                    "config": component.thread_config(web_search=False),
                },
            ),
            CALL_TIMEOUT_S,
        )
        thread_id = str(thread["id"])
        params: dict[str, Any] = {"model": model, "effort": "low"}
        if schema is not None:
            params["outputSchema"] = schema

        async def no_tools(_: dict[str, Any]) -> dict[str, Any]:
            return tool_failure("Ошибка: в этом запросе инструментов нет")

        self._active[thread_id] = ActiveTurn(no_tools)
        run: TurnRun | None = None
        try:
            run = await self._start_shielded(self._start_turn(runtime, thread_id, items, params))
            return await self._consume(run, None)
        except asyncio.CancelledError:
            if run is not None:
                _background(run.interrupt, "interrupt")
            raise
        except RuntimeGone:
            raise LlmError(errors.DEAD) from None
        except UserFacingError:
            raise
        except Exception as e:
            raise LlmError(errors.explain_exception(e, model=model)[1]) from None
        finally:
            self._active.pop(thread_id, None)
            _background(lambda: runtime.thread_unsubscribe(thread_id), "unsubscribe")

    # --- агент --------------------------------------------------------------------------------------
    async def run_agent(
        self, agent: "Agent", run: "AgentRun", *, system: str, first: str, specs: list[dict[str, Any]]
    ) -> str:
        """Работа агента ходом Codex: инструменты — действия агента; вернёт итог (finish) для чата."""
        from jarvis.ai.agent import AgentLimit

        problem = self.problem()
        if problem:
            raise UserFacingError(problem)
        runtime = await self._runtime_for_use()
        model = self.resolve_model()
        limit = self.ctx.settings.value.ai.max_agent_steps
        tools = []
        for spec in specs:
            fn = spec.get("function") or {}
            description = str(fn.get("description", ""))
            if fn.get("name") == "screenshot":
                description += SCREENSHOT_HINT
            tools.append(
                {
                    "type": "function",
                    "name": fn.get("name"),
                    "description": description,
                    "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        thread = await asyncio.wait_for(
            asyncio.to_thread(
                runtime.thread_start,
                {
                    "model": model,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "cwd": str(component.work_dir()),
                    "ephemeral": True,
                    "developerInstructions": f"{prompt.AGENT_RULES}\n\n{system}",
                    "dynamicTools": tools,
                    "config": component.thread_config(web_search=False),
                },
            ),
            CALL_TIMEOUT_S,
        )
        thread_id = str(thread["id"])
        finish: dict[str, Any] = {"summary": None, "limit": False}
        run_ref: dict[str, TurnRun | None] = {"run": None}
        one_at_a_time = asyncio.Lock()  # exec (режим кода) может звать действия параллельно — окно одно

        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            async with one_at_a_time:
                return await act(params)

        def result(content: str, extras: list[dict[str, Any]]) -> dict[str, Any]:
            items: list[dict[str, Any]] = [{"type": "inputText", "text": content}]
            for extra in extras:
                parts = extra.get("content") if isinstance(extra.get("content"), list) else []
                for part in parts:
                    if part.get("type") == "text" and part.get("text"):
                        items.append({"type": "inputText", "text": str(part["text"])})
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url")
                        if isinstance(url, str):
                            items.append({"type": "inputImage", "imageUrl": url})
            return {"contentItems": items, "success": not content.startswith("Ошибка")}

        async def act(params: dict[str, Any]) -> dict[str, Any]:
            call = self._call_of(params)
            if call.name == "finish":
                try:
                    finish_args = call.parse_arguments()
                except ValueError:
                    finish_args = {}
                check = await agent.check_finish(run, finish_args)
                if check is not None:  # реплика пользователя или проверка по снимку — работа продолжается
                    return result(*check)
                finish["summary"] = agent.finish_summary(run, finish_args)
                return {"contentItems": [{"type": "inputText", "text": "ok"}], "success": True}
            if finish["summary"] is not None:
                return tool_failure("Пропущено: работа уже завершена")
            if run.count >= limit:
                finish["limit"] = True
                current = run_ref["run"]
                if current is not None:
                    _background(current.interrupt, "interrupt")
                return tool_failure(f"Лимит шагов ({limit}) исчерпан")
            return result(*await agent._act(run, call))

        active = ActiveTurn(handler)
        self._active[thread_id] = active
        turn_run: TurnRun | None = None
        try:
            turn_run = await self._start_shielded(
                self._start_turn(
                    runtime,
                    thread_id,
                    [{"type": "text", "text": first}],
                    {"model": model, "effort": self._agent_effort(model)},
                )
            )
            run_ref["run"] = active.run = turn_run
            out = await self._consume(turn_run, None)
        except asyncio.CancelledError:
            self._abandon(None, active, turn_run)
            raise
        except RuntimeGone:
            raise UserFacingError(errors.DEAD) from None
        except UserFacingError:
            raise
        except Exception as e:
            raise UserFacingError(errors.explain_exception(e, model=model)[1]) from None
        finally:
            self._active.pop(thread_id, None)
            _background(lambda: runtime.thread_unsubscribe(thread_id), "unsubscribe")
        run.usage.add(self._usage(out, model))
        if finish["limit"]:
            raise AgentLimit(f"Агент не уложился в {limit} шагов")
        if finish["summary"] is not None:
            return str(finish["summary"])
        if out.status == "completed":
            return out.final or "Агент закончил без итога — проверьте результат на экране."
        if out.status == "interrupted" and not out.abort_reason and out.error is None:
            raise asyncio.CancelledError
        raise UserFacingError(self._failure(out, model))
