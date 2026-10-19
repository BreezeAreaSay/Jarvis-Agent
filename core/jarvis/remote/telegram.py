"""Telegram-бот «PC Control»: пульт из любой точки мира через бота, созданного владельцем в @BotFather.

Long polling getUpdates — без вебхуков и белого IP. Бот слушается только владельца
(settings.remote.telegram_owner_id); владелец привязывается кодом из приложения: /start 123456 (или ссылкой
t.me/<бот>?start=123456). Меню — инлайн-кнопки, навигация правит одно и то же сообщение:
«Главное › Медиа» → ⏯ Пауза … Внизу сообщения — итог последнего нажатия: «Выполнено: Пауза».
Свободный текст и голосовые уходят в Jarvis: ctx.dispatcher.handle(text, "telegram").

Безопасность: при запуске накопившиеся нажатия кнопок не выполняются (пока компьютер был выключен, кто-то мог
нажать «Выключить»), старые сообщения пропускаются, подтверждение выключения живёт минуту.
"""

import asyncio
import contextlib
import functools
import html
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from jarvis import netproxy
from jarvis.platform.base import PlatformError
from jarvis.remote import audio
from jarvis.remote.actions import Outcome
from jarvis.remote.net import pc_name
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.remote.service import Remote

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 25
MAX_BACKOFF = 60.0
STALE_MESSAGE_S = 120.0  # сообщения старше — из очереди, пока компьютер был выключен
CONFIRM_TTL_S = 60.0
STRANGER_REPLY_S = 600.0  # чужим отвечаем не чаще раза в 10 минут
MAX_MESSAGE = 4000
MAX_VOICE_BYTES = 20 * 1024 * 1024

DENIED = "Это личный пульт. Доступ закрыт."
NOT_PAIRED = (
    "Пульт ещё не привязан. Откройте Jarvis → «Пульт в телефоне» → Telegram, нажмите «Привязать» и отправьте "
    "сюда код в виде: /start 123456"
)
BAD_CODE = "Код не подошёл или устарел. Получите новый в приложении Jarvis: «Пульт в телефоне» → Telegram."
NO_VOICE = "Голосовые пока не поддерживаются — пришлите текстом"
HELP = (
    "Это пульт Jarvis для вашего компьютера.\n\n"
    "• /menu — главное меню: медиа, система, команды, скриншот\n"
    "• /screen — скриншот экрана\n"
    "• Команду или вопрос можно просто написать: «сверни все окна», «какая погода завтра» — "
    "Jarvis выполнит или ответит.\n"
    "• Голосовые сообщения тоже понимаю."
)
BOT_COMMANDS = [
    {"command": "menu", "description": "Главное меню пульта"},
    {"command": "screen", "description": "Скриншот экрана"},
    {"command": "help", "description": "Что умеет пульт"},
]


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(description)
        self.description = description
        self.code = code
        self.retry_after = retry_after


class TelegramApi:
    """Тонкая обёртка Bot API. Токен живёт только в адресе запроса и в журнал не пишется."""

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self.client = client

    @property
    def bot_id(self) -> str:
        return self._token.split(":", 1)[0]

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(f"{API_BASE}/bot{self._token}/{method}", json=params or {})
        return self._result(response)

    async def upload(self, method: str, fields: dict[str, str], files: dict[str, Any]) -> Any:
        response = await self.client.post(
            f"{API_BASE}/bot{self._token}/{method}", data=fields, files=files, timeout=60.0
        )
        return self._result(response)

    async def download(self, file_path: str) -> bytes:
        response = await self.client.get(f"{API_BASE}/file/bot{self._token}/{file_path}", timeout=60.0)
        if response.status_code != 200:
            raise TelegramError(f"файл не скачался (HTTP {response.status_code})", response.status_code)
        return response.content

    @staticmethod
    def _result(response: httpx.Response) -> Any:
        try:
            data = response.json()
        except ValueError as e:
            raise TelegramError(f"HTTP {response.status_code}", response.status_code) from e
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            parameters = data.get("parameters") or {}
            raise TelegramError(
                str(data.get("description") or f"HTTP {response.status_code}"),
                int(data.get("error_code") or response.status_code),
                float(parameters.get("retry_after") or 0),
            )
        return data.get("result")


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _header(*parts: str) -> str:
    return "<b>" + " › ".join(html.escape(p) for p in parts) + "</b>"


def _split_command(text: str) -> tuple[str, str]:
    """«/start@my_bot 123456» → ("start", "123456"); не команда — ("", "")."""
    if not text.startswith("/"):
        return "", ""
    head, _, rest = text.partition(" ")
    return head[1:].split("@", 1)[0].casefold(), rest.strip()


def chunks(text: str, size: int = MAX_MESSAGE) -> list[str]:
    """Разрезать длинный ответ на сообщения Telegram — по строкам, если получается."""
    out: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def format_reply(result: dict[str, Any]) -> str:
    """Итог Jarvis (см. RemoteActions.dispatch) → ответ в чат."""
    kind = result.get("kind")
    message = str(result.get("message") or "")
    if kind == "command":
        title = str(result.get("title") or "Команда")
        if result.get("ok"):
            extra = f"\n{message}" if message and message != "Выполнено" else ""
            return f"✅ {title}{extra}"
        return f"❌ {title}" + (f"\n{message}" if message else "")
    if kind == "ai":
        if result.get("ok"):
            return str(result.get("text") or "…")
        return f"❌ {message or 'ИИ не ответил'}"
    if kind == "stop":
        return "⏹ Остановлено"
    if kind == "unknown":
        return "🤷 Не понял команду. Попробуйте сказать иначе."
    return ""


def status_line(outcome: Outcome) -> str:
    if outcome.ok:
        return f"Выполнено: {outcome.title}" + (f" · {outcome.message}" if outcome.message else "")
    return f"Ошибка: {outcome.message or outcome.title}"


class TelegramBot:
    def __init__(self, remote: "Remote") -> None:
        self.remote = remote
        self.ctx = remote.ctx
        self.actions = remote.actions
        self.transport: httpx.AsyncBaseTransport | None = None  # тесты подставляют поддельный Telegram
        self.decoder: Callable[[bytes], Any] | None = None  # тесты подставляют свой декодер голосовых
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self.status = "off"  # off | connecting | online | error
        self.error = ""
        self.username = ""
        self.api: TelegramApi | None = None
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[Any] | None = None
        self._jobs: set[asyncio.Task[Any]] = set()
        self._config: tuple[str, str] | None = None
        self._system: tuple[tuple[str, str], ...] = ()  # системный прокси, с которым создан клиент
        self._bot_id = ""  # чей username показываем: сменился токен — имя бота узнаём заново
        self._offset = 0
        self._strangers: dict[int, float] = {}

    # --- запуск и остановка -----------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def apply(self, enabled: bool, token: str, proxy: str) -> None:
        """Привести бота к настройкам: выключен, нет токена или работает (сменились токен/прокси — заново)."""
        if not enabled:
            await self.stop()
            self._set_status("off", "")
            return
        if not token:
            await self.stop()
            self._set_status("error", "Укажите токен бота от @BotFather")
            return
        if self.running and self._config == (token, proxy):
            return
        await self.stop()
        try:
            self.connect(token, proxy)
        except Exception as e:
            log.warning("telegram: клиент не создан: %s", e)
            self._set_status("error", "Неверный адрес прокси в настройках ИИ")
            return
        self._task = self.ctx.spawn(self._run(), "telegram-bot")

    def connect(self, token: str, proxy: str = "") -> None:
        """Подготовить клиента Bot API, не запуская опрос."""
        kwargs: dict[str, Any] = {}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        elif proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=POLL_TIMEOUT + 10.0, write=30.0, pool=10.0), **kwargs
        )
        self.api = TelegramApi(token, self._client)
        self._config = (token, proxy)
        self._system = self._system_proxy()
        if self.api.bot_id != self._bot_id:
            self._bot_id = self.api.bot_id
            self.username = ""
        self._offset = int(self.ctx.db.kv_get(self._offset_key(), 0) or 0)

    def _system_proxy(self) -> tuple[tuple[str, str], ...]:
        """Системный прокси, через который пойдёт клиент без своего прокси (() — свой прокси или тесты)."""
        if self._config is None or self._config[1] or self.transport is not None:
            return ()
        return tuple(sorted(netproxy.system_proxies().items()))

    def _follow_system_proxy(self) -> None:
        """Связи нет, а VPN тем временем включили или выключили (сменился системный прокси) — новый клиент.

        Прежний не закрываем: им ещё могут отвечать задания (_jobs).
        """
        if self._config is not None and self._system_proxy() != self._system:
            log.info("telegram: сменился системный прокси — переподключаюсь")
            try:
                self.connect(*self._config)
            except Exception as e:
                log.warning("telegram: клиент не пересоздан: %s", e)

    async def stop(self) -> None:
        task, self._task = self._task, None
        jobs, self._jobs = list(self._jobs), set()
        for pending in [task, *jobs]:
            if pending is not None:
                pending.cancel()
        for pending in [task, *jobs]:
            if pending is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
        client, self._client = self._client, None
        self.api = None
        self._config = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    def _set_status(self, status: str, error: str) -> None:
        if (status, error) == (self.status, self.error):
            return
        self.status, self.error = status, error
        self.remote.publish()

    def _offset_key(self) -> str:
        bot_id = self.api.bot_id if self.api is not None else "0"
        return f"remote:telegram_offset:{bot_id}"

    # --- опрос ------------------------------------------------------------------------------------
    async def _run(self) -> None:
        delay = 1.0
        while True:
            api = self.api
            if api is None:
                return
            try:
                self._set_status("connecting", "")
                me = await api.call("getMe")
                self.username = str((me or {}).get("username") or "")
                with contextlib.suppress(TelegramError, httpx.HTTPError):
                    await api.call("setMyCommands", {"commands": BOT_COMMANDS})
                self._set_status("online", "")
                delay = 1.0
                await self._poll(api)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code in (401, 404):
                    log.warning("telegram: токен не принят (%s)", e.code)
                    self._set_status("error", "Неверный токен бота — проверьте его в @BotFather")
                    return
                if e.code == 409 and "webhook" in e.description.casefold():
                    with contextlib.suppress(TelegramError, httpx.HTTPError):
                        await api.call("deleteWebhook", {"drop_pending_updates": False})
                    self._set_status("error", "У бота был вебхук — отключил, переподключаюсь")
                elif e.code == 409:
                    self._set_status(
                        "error", "Этот бот уже запущен в другом месте (другой компьютер или программа)"
                    )
                else:
                    log.warning("telegram: ошибка API %s: %s", e.code, e.description)
                    self._set_status("error", f"Telegram: {e.description}")
                if e.retry_after:
                    delay = max(delay, e.retry_after)
            except httpx.HTTPError as e:
                log.info("telegram: нет связи (%s)", type(e).__name__)
                proxy = self._config[1] if self._config else ""
                used = dict(self._system)  # системный прокси, с которым создан клиент
                system = used.get("https") or used.get("all") or ""
                if proxy:
                    hint = " — проверьте прокси"
                elif system:
                    hint = f" — запросы идут через системный прокси {system}: проверьте, что VPN включён"
                else:
                    hint = " — возможно, нужен прокси (Настройки → ИИ → Прокси)"
                self._set_status("error", "Нет связи с Telegram" + hint)
                self._follow_system_proxy()
            except Exception:
                log.exception("telegram: опрос упал")
                self._set_status("error", "Сбой бота — перезапускаю")
            await self.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)

    async def _poll(self, api: TelegramApi) -> None:
        backlog = True  # сначала разбираем то, что накопилось, пока ядро не работало
        while True:
            updates = await api.call(
                "getUpdates",
                {
                    "offset": self._offset,
                    "timeout": 0 if backlog else POLL_TIMEOUT,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            for update in updates or []:
                self._advance(update)
                if "message" in update and not backlog:
                    # фраза для ИИ может думать полминуты — кнопки пульта в это время работают
                    self._job(self.handle_update(update))
                    continue
                try:
                    await self.handle_update(update, backlog=backlog)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("telegram: обновление не обработано")
            if backlog and not updates:
                backlog = False

    def _advance(self, update: dict[str, Any]) -> None:
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            return
        if update_id + 1 > self._offset:
            self._offset = update_id + 1
            self.ctx.db.kv_set(self._offset_key(), self._offset)

    def _job(self, coro: Awaitable[Any]) -> None:
        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram: сообщение не обработано")

        task = asyncio.ensure_future(guarded())
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    # --- обработка --------------------------------------------------------------------------------
    async def handle_update(self, update: dict[str, Any], *, backlog: bool = False) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"], backlog=backlog)
        elif "message" in update:
            await self._on_message(update["message"], backlog=backlog)

    def _owner(self) -> int:
        return int(self.ctx.settings.value.remote.telegram_owner_id or 0)

    def _may_reply_stranger(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._strangers.get(user_id)
        if last is not None and now - last < STRANGER_REPLY_S:
            return False
        self._strangers[user_id] = now
        if len(self._strangers) > 500:
            self._strangers.pop(next(iter(self._strangers)))
        return True

    async def _on_message(self, message: dict[str, Any], *, backlog: bool = False) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = int(user.get("id") or 0)
        if chat.get("type") != "private" or chat_id is None or not user_id or user.get("is_bot"):
            return
        if backlog and time.time() - float(message.get("date") or 0) > STALE_MESSAGE_S:
            return
        text = str(message.get("text") or "").strip()
        command, arg = _split_command(text)
        owner = self._owner()

        if command == "start" and arg and user_id != owner:
            if self.remote.consume_pairing(arg):
                self._bind_owner(user_id, user)
                await self._send(
                    chat_id, "✅ Готово! Пульт привязан к вашему аккаунту — теперь бот слушается только вас."
                )
                await self._send_menu(chat_id)
            else:
                await self._send(chat_id, BAD_CODE)
            return
        if not owner:
            if self._may_reply_stranger(user_id):
                await self._send(chat_id, NOT_PAIRED)
            return
        if user_id != owner:
            log.info("telegram: чужой пользователь %s — отказ", user_id)
            if self._may_reply_stranger(user_id):
                await self._send(chat_id, DENIED)
            return

        if command in ("start", "menu"):
            await self._send_menu(chat_id)
        elif command == "help":
            await self._send(chat_id, HELP)
        elif command in ("screen", "screenshot"):
            try:
                await self._send_screenshot(chat_id)
            except (UserFacingError, PlatformError, TelegramError) as e:
                await self._send(chat_id, f"Скриншот не получился: {e}")
        elif message.get("voice") or message.get("audio"):
            await self._on_voice(chat_id, message)
        elif command:
            await self._send(chat_id, HELP)
        elif text:
            await self._dispatch(chat_id, text)
        else:
            await self._send(chat_id, "Пришлите текст или голосовое — передам Jarvis.")

    def _bind_owner(self, user_id: int, user: dict[str, Any]) -> None:
        name = " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip()
        log.info("telegram: пульт привязан к %s (%s)", user_id, name or user.get("username") or "—")
        self.ctx.settings.patch({"remote": {"telegram_owner_id": user_id}})

    async def _on_callback(self, query: dict[str, Any], *, backlog: bool = False) -> None:
        query_id = str(query.get("id") or "")
        user_id = int((query.get("from") or {}).get("id") or 0)
        data = str(query.get("data") or "")
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        owner = self._owner()
        if not owner or user_id != owner:
            await self._answer(query_id, DENIED, alert=True)
            return
        if backlog:
            await self._answer(query_id, "Кнопка устарела — откройте меню заново: /menu")
            return
        if chat_id is None or message_id is None:
            await self._answer(query_id)
            return
        parts = data.split(":")
        if parts[0] == "nav" and len(parts) >= 2:
            await self._answer(query_id)
            await self._show(chat_id, message_id, parts[1])
        elif parts[0] == "do" and len(parts) >= 3:
            await self._answer(query_id)
            await self._do(chat_id, message_id, parts[1], parts[2], parts[3:])
        else:
            await self._answer(query_id, "Неизвестная кнопка")

    async def _do(self, chat_id: int, message_id: int, menu: str, action: str, extra: list[str]) -> None:
        try:
            if menu == "media":
                outcome = await self.actions.media(action)
            elif action == "screenshot":
                await self._send_screenshot(chat_id)
                outcome = Outcome(True, "Скриншот")
            elif menu == "system" and action == "minimize_all":
                outcome = await self.actions.minimize_all()
                outcome.title = "Свернуть всё"
            elif menu == "system" and action in ("lock", "sleep"):
                outcome = await self.actions.power(action)
                outcome.title = "Заблокировать" if action == "lock" else "Сон"
            elif menu == "system" and action == "shutdown":
                await self._show(chat_id, message_id, "shutdown")
                return
            elif menu == "system" and action == "shutdown_yes":
                issued = int(extra[0]) if extra and extra[0].isdigit() else 0
                if time.time() - issued > CONFIRM_TTL_S:
                    outcome = Outcome(
                        False, "Выключить", "подтверждение устарело — нажмите «Выключить» ещё раз"
                    )
                else:
                    outcome = await self.actions.power("shutdown")
                    outcome.title = "Выключение"
            elif menu == "commands":
                outcome = await self.actions.run_favorite(action, "telegram")
                if len(outcome.message) > 200:  # длинный ответ ИИ — отдельным сообщением
                    for part in chunks(outcome.message):
                        await self._send(chat_id, part)
                    outcome.message = ""
            else:
                outcome = Outcome(False, "Кнопка", "неизвестная кнопка")
        except (UserFacingError, PlatformError) as e:
            outcome = Outcome(False, "Ошибка", str(e) or "не получилось")
        except TelegramError as e:
            outcome = Outcome(False, "Ошибка", f"Telegram: {e.description}")
        except Exception:
            log.exception("telegram: кнопка %s:%s упала", menu, action)
            outcome = Outcome(False, "Ошибка", "не получилось")
        await self._show(chat_id, message_id, menu, status_line(outcome))

    # --- меню -------------------------------------------------------------------------------------
    async def render(self, menu: str, status: str = "") -> tuple[str, dict[str, Any]]:
        """Текст (HTML) и кнопки меню: main, media, system, shutdown, commands, ai."""
        back = [_button("← Назад", "nav:main")]
        lines: list[str]
        rows: list[list[dict[str, str]]]
        if menu == "media":
            lines = [_header("Главное", "Медиа")]
            try:
                state = await self.actions.state()
                media = state["media"]
                if media.get("title"):
                    track = html.escape(media["title"])
                    if media.get("artist"):
                        track += f" — {html.escape(media['artist'])}"
                    if media.get("app"):
                        track += f" ({html.escape(media['app'])})"
                    paused = " ⏸" if media.get("status") == "paused" else ""
                    lines.append(f"🎵 {track}{paused}")
                else:
                    lines.append("Сейчас ничего не играет")
                lines.append(
                    f"🔊 Громкость: {state['volume']}%" + (" · звук выключен" if state["muted"] else "")
                )
            except Exception as e:
                lines.append(f"Плеер недоступен: {html.escape(str(e))}")
            rows = [
                [
                    _button("⏮ Назад", "do:media:prev"),
                    _button("⏯ Пауза", "do:media:play_pause"),
                    _button("⏭ Дальше", "do:media:next"),
                ],
                [
                    _button("🔉 Тише", "do:media:volume_down"),
                    _button("🔇 Звук", "do:media:mute"),
                    _button("🔊 Громче", "do:media:volume_up"),
                ],
                back,
            ]
        elif menu == "system":
            lines = [_header("Главное", "Система")]
            try:
                lines.append(html.escape(await self.actions.system_line()))
            except Exception as e:
                lines.append(f"Нагрузка недоступна: {html.escape(str(e))}")
            rows = [
                [
                    _button("🗕 Свернуть всё", "do:system:minimize_all"),
                    _button("🔒 Заблокировать", "do:system:lock"),
                ],
                [_button("🌙 Сон", "do:system:sleep"), _button("⏻ Выключить", "do:system:shutdown")],
                [_button("📸 Скриншот", "do:system:screenshot")],
                back,
            ]
        elif menu == "shutdown":
            lines = [
                _header("Главное", "Система", "Выключение"),
                "Выключить компьютер? Несохранённые данные пропадут.",
            ]
            rows = [
                [
                    _button("✅ Да, выключить", f"do:system:shutdown_yes:{int(time.time())}"),
                    _button("✖ Отмена", "nav:system"),
                ]
            ]
        elif menu == "commands":
            favorites = self.actions.favorites()
            lines = [_header("Главное", "Команды")]
            lines.append(
                "Избранные команды и фразы для Jarvis." if favorites else "Избранных команд пока нет."
            )
            buttons = [_button(f["title"][:40], f"do:commands:{f['key']}") for f in favorites[:20]]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append(back)
        elif menu == "ai":
            lines = [
                _header("Главное", "Jarvis AI"),
                "Напишите вопрос или команду обычным сообщением — Jarvis ответит здесь. Можно голосовым.",
            ]
            rows = [back]
        else:
            lines = [_header("Главное"), f"🖥 {html.escape(pc_name())} на связи"]
            rows = [
                [_button("🎵 Медиа", "nav:media"), _button("🖥 Система", "nav:system")],
                [_button("⭐ Команды", "nav:commands"), _button("📸 Скриншот", "do:main:screenshot")],
                [_button("🤖 Jarvis AI", "nav:ai")],
            ]
        if status:
            lines += ["", html.escape(status[:500])]
        return "\n".join(lines), {"inline_keyboard": rows}

    async def _show(self, chat_id: int, message_id: int, menu: str, status: str = "") -> None:
        text, keyboard = await self.render(menu, status)
        await self._edit(chat_id, message_id, text, keyboard)

    async def _send_menu(self, chat_id: int) -> None:
        text, keyboard = await self.render("main")
        await self._send(chat_id, text, keyboard, as_html=True)

    # --- ответы -----------------------------------------------------------------------------------
    def _require_api(self) -> TelegramApi:
        if self.api is None:
            raise TelegramError("бот не подключён")
        return self.api

    async def _send(
        self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None, *, as_html: bool = False
    ) -> Any:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_MESSAGE] or "…",
            "link_preview_options": {"is_disabled": True},
        }
        if as_html:
            params["parse_mode"] = "HTML"
        if keyboard is not None:
            params["reply_markup"] = keyboard
        return await self._require_api().call("sendMessage", params)

    async def _edit(self, chat_id: int, message_id: int, text: str, keyboard: dict[str, Any]) -> None:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
            "link_preview_options": {"is_disabled": True},
        }
        try:
            await self._require_api().call("editMessageText", params)
        except TelegramError as e:
            description = e.description.casefold()
            if "not modified" in description:
                return
            if "not found" in description or "can't be edited" in description:
                await self._send(chat_id, text, keyboard, as_html=True)
                return
            raise

    async def _answer(self, query_id: str, text: str = "", *, alert: bool = False) -> None:
        if not query_id:
            return
        params: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            params["text"] = text
            params["show_alert"] = alert
        with contextlib.suppress(TelegramError, httpx.HTTPError):
            await self._require_api().call("answerCallbackQuery", params)

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: int, action: str = "typing") -> AsyncIterator[None]:
        """«Печатает…» в чате, пока Jarvis думает (Telegram гасит статус через 5 с — повторяем)."""
        api = self._require_api()

        async def ping() -> None:
            with contextlib.suppress(TelegramError, httpx.HTTPError):
                await api.call("sendChatAction", {"chat_id": chat_id, "action": action})

        async def keep() -> None:
            while True:
                await asyncio.sleep(4.5)
                await ping()

        await ping()
        task = asyncio.ensure_future(keep())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _send_screenshot(self, chat_id: int) -> None:
        api = self._require_api()
        data, mime = await self.actions.screenshot("jpeg")
        extension = "jpg" if mime == "image/jpeg" else "png"
        caption = f"Скриншот · {time.strftime('%H:%M')}"
        await api.upload(
            "sendPhoto",
            {"chat_id": str(chat_id), "caption": caption},
            {"photo": (f"screenshot.{extension}", data, mime)},
        )

    async def _dispatch(self, chat_id: int, text: str) -> None:
        async with self._typing(chat_id):
            result = await self.actions.dispatch(text, "telegram")
        reply = format_reply(result)
        for part in chunks(reply):
            await self._send(chat_id, part)

    async def _on_voice(self, chat_id: int, message: dict[str, Any]) -> None:
        voice = message.get("voice") or message.get("audio") or {}
        if int(voice.get("duration") or 0) > audio.MAX_SECONDS:
            await self._send(chat_id, "Голосовое слишком длинное — пришлите до 2 минут")
            return
        if int(voice.get("file_size") or 0) > MAX_VOICE_BYTES:
            await self._send(chat_id, "Файл слишком большой")
            return
        decoder = self.decoder
        if decoder is None:
            ffmpeg = audio.find_ffmpeg()
            if ffmpeg is None:
                await self._send(chat_id, NO_VOICE)
                return
            decoder = functools.partial(audio.decode_with_ffmpeg, ffmpeg=ffmpeg)
        api = self._require_api()
        text = ""
        async with self._typing(chat_id):
            try:
                info = await api.call("getFile", {"file_id": voice.get("file_id")})
                data = await api.download(str((info or {}).get("file_path") or ""))
                samples = await self.ctx.run_io(decoder, data)
                text = str(await self.ctx.voice.transcribe(samples, audio.RATE) or "").strip()
            except audio.AudioDecodeError as e:
                log.warning("telegram: голосовое не декодировалось: %s", e)
                await self._send(chat_id, "Не удалось прочитать голосовое — пришлите текстом")
                return
            except (TelegramError, httpx.HTTPError) as e:
                log.warning("telegram: голосовое не скачалось: %s", type(e).__name__)
                await self._send(chat_id, "Не удалось скачать голосовое — попробуйте ещё раз")
                return
            except UserFacingError as e:
                await self._send(chat_id, str(e))
                return
        if not text:
            await self._send(chat_id, "Не расслышал — повторите, пожалуйста")
            return
        await self._send(chat_id, f"🎙 «{text}»")
        await self._dispatch(chat_id, text)
