"""Основа плагинов: класс Plugin, описание настроек для интерфейса и общие помощники.

Плагин — модуль jarvis/plugins/<id>.py с подклассом Plugin. Менеджер (jarvis.plugins.manager.Plugins) создаёт
все плагины при сборке ядра, запускает включённые (start) и останавливает выключенные (stop), хранит
включённость и настройки в settings.plugins[id], отдаёт интерфейсу описание (Plugin в docs/CONTRACTS.md §5)
и собирает голосовые команды работающих плагинов (commands()).

Правила для плагина:
- конструктор дешёвый и не падает: никаких тяжёлых импортов и обращений к ОС — всё в start() и действиях;
- фоновые циклы — self.spawn(...): ошибка попадёт в статус плагина, а не уронит ядро;
- блокирующие циклы захвата звука и камеры — в своих потоках (daemon), а не в пуле ctx.run_io;
- события — self.publish("<имя>", data) → plugin.<id>.<имя>;
- изменился статус для плитки — self.changed(); изменился набор голосовых команд — self.commands_changed().
"""

import asyncio
import contextlib
import copy
import inspect
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import httpx

from jarvis.platform.base import PlatformError
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.plugins.manager import Plugins

log = logging.getLogger(__name__)

FieldKind = Literal["text", "number", "bool", "select", "secret", "hotkey", "device", "list"]
CommandHandler = Callable[[dict[str, Any]], Awaitable[ExecResult | str | None]]

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_PHRASE_JUNK = re.compile(r"[^\w\s{}%-]", re.UNICODE)
_MAX_TEXT = 2000
_MAX_LIST = 20


@dataclass(frozen=True)
class Option:
    """Вариант для поля select: значение и подпись."""

    value: str
    label: str

    def to_json(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class ConfigField:
    """Поле настроек плагина — по нему интерфейс рисует форму, а менеджер проверяет значения.

    kind: text | number | bool | select | secret | hotkey | device | list.
    secret — имя секрета в ctx.secrets: значение в настройки не попадает (в схеме — только set: bool).
    device — какой список устройств показать: "input" (микрофоны) или "output" (колонки) из
    /api/voice/devices.
    fields — поля одного элемента списка (kind="list"); в элементе поле kind="secret" хранит сам плагин.
    """

    key: str
    kind: FieldKind
    label: str
    default: Any = None
    options: tuple[Option, ...] = ()
    secret: str = ""
    hint: str = ""
    placeholder: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    integer: bool = False
    device: str = ""
    fields: tuple["ConfigField", ...] = ()

    def default_value(self) -> Any:
        if self.default is not None:
            return copy.deepcopy(self.default)
        return {"number": 0, "bool": False, "list": []}.get(self.kind, "")

    def to_json(self, secret_set: Callable[[str], bool] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "kind": self.kind, "label": self.label}
        if self.kind != "secret":
            data["default"] = self.default_value()
        if self.options:
            data["options"] = [o.to_json() for o in self.options]
        if self.secret:
            data["secret"] = self.secret
            data["set"] = bool(secret_set and secret_set(self.secret))
        for name in ("hint", "placeholder", "device"):
            value = getattr(self, name)
            if value:
                data[name] = value
        for name in ("min", "max", "step"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.fields:
            data["fields"] = [f.to_json(None) for f in self.fields]
        return data

    def coerce(self, value: Any) -> Any:
        """Привести значение к типу поля. Неверное значение — ValueError с текстом для человека."""
        if value is None:
            return self.default_value()
        kind = self.kind
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, int | float):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "да", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "нет", "off", ""):
                return False
            raise ValueError(f"«{self.label}»: нужно «да» или «нет»")
        if kind == "number":
            try:
                number = float(str(value).replace(",", ".")) if not isinstance(value, int | float) else value
            except ValueError:
                raise ValueError(f"«{self.label}»: нужно число") from None
            if isinstance(number, bool) or not math.isfinite(number):  # bool, NaN и бесконечность — не числа
                raise ValueError(f"«{self.label}»: нужно число")
            if self.min is not None:
                number = max(self.min, number)
            if self.max is not None:
                number = min(self.max, number)
            return round(number) if self.integer else float(number)
        if kind == "select":
            text = str(value)
            if self.options and text not in {o.value for o in self.options}:
                raise ValueError(f"«{self.label}»: недопустимое значение «{text}»")
            return text
        if kind == "list":
            if not isinstance(value, list):
                raise ValueError(f"«{self.label}»: нужен список")
            items: list[dict[str, Any]] = []
            for raw in value[:_MAX_LIST]:
                if not isinstance(raw, dict):
                    raise ValueError(f"«{self.label}»: неверный элемент списка")
                item: dict[str, Any] = {}
                if raw.get("id"):
                    item["id"] = str(raw["id"])[:64]
                for sub in self.fields:
                    if sub.kind == "secret":
                        # секрет элемента забирает плагин (prepare_config): None — удалить, "" — не менять
                        if sub.key in raw:
                            secret = raw[sub.key]
                            item[sub.key] = None if secret is None else str(secret).strip()[:_MAX_TEXT]
                    elif sub.key in raw:
                        item[sub.key] = sub.coerce(raw[sub.key])
                    else:
                        item[sub.key] = sub.default_value()
                items.append(item)
            return items
        # text, hotkey, device, secret (внутри элемента списка)
        if not isinstance(value, str | int | float):
            raise ValueError(f"«{self.label}»: нужен текст")
        return str(value).strip()[:_MAX_TEXT]


@dataclass(frozen=True)
class PluginAction:
    """Кнопка на плитке плагина: id действия для POST /api/plugins/{id}/action и подпись."""

    id: str
    title: str

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title}


def phrase(text: str) -> str:
    """Шаблон голосовой фразы: нижний регистр, «ё» → «е», без знаков (слоты {number}, {text} сохраняются)."""
    t = text.casefold().replace("ё", "е")
    t = _PHRASE_JUNK.sub(" ", t)
    return " ".join(t.split())


def spoken_name(name: str) -> str:
    """Название устройства, комнаты или компьютера для вставки во фразу (без скобок слотов и знаков)."""
    return phrase(name.replace("{", " ").replace("}", " "))


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def error_text(exc: BaseException) -> str:
    """Текст ошибки для статуса и тоста: свои ошибки — как есть, чужие — с типом."""
    if isinstance(exc, UserFacingError | PlatformError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "Превышено время ожидания"
    if isinstance(exc, httpx.ConnectError):
        return "Нет связи с сервером"
    if isinstance(exc, httpx.TimeoutException):
        return "Сервер не ответил вовремя"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class Plugin:
    """Базовый класс плагина. Подкласс задаёт описание (id, name …), поля настроек, действия и логику."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "plugin"
    color: ClassVar[tuple[str, str]] = ("#64748B", "#475569")
    fields: ClassVar[tuple[ConfigField, ...]] = ()
    actions: ClassVar[tuple[PluginAction, ...]] = ()
    examples: ClassVar[tuple[str, ...]] = ()  # примеры голосовых фраз для плитки

    def __init__(self, ctx: "AppContext", manager: "Plugins") -> None:
        self.ctx = ctx
        self.manager = manager
        self.error = ""  # ошибка работы (фоновая задача упала, устройство недоступно)
        self.http_transport: httpx.AsyncBaseTransport | None = None  # тесты подменяют сеть
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- жизненный цикл (переопределяют плагины) ------------------------------------------------------
    async def start(self) -> None:
        """Плагин включён: запустить фоновые задачи. Должен вернуться быстро."""
        return None

    async def stop(self) -> None:
        """Плагин выключен или ядро останавливается: остановить своё (задачи spawn() менеджер отменит сам)."""
        return None

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        """Настройки изменились, пока плагин работает."""
        return None

    def prepare_config(self, config: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
        """Перед сохранением настроек: плагин может забрать секреты из элементов списка и т. п."""
        return config

    def public_config(self) -> dict[str, Any]:
        """Настройки для интерфейса (по умолчанию — как хранятся)."""
        return self.config

    def status(self) -> dict[str, Any]:
        """Состояние для плитки в интерфейсе (JSON)."""
        return {}

    def commands(self) -> list[DynamicCommand]:
        """Голосовые команды плагина (только пока он работает)."""
        return []

    async def handle(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Действие из интерфейса или API: вызывает метод act_<action>(args)."""
        method = getattr(self, f"act_{action}", None) if _ACTION_RE.match(action or "") else None
        if method is None:
            raise UserFacingError(f"У плагина «{self.name}» нет действия «{action}»")
        result = await method(args)
        return result if isinstance(result, dict) else {}

    # --- описание и настройки --------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.manager.is_enabled(self.id)

    @property
    def running(self) -> bool:
        return self.manager.is_running(self.id)

    @property
    def config(self) -> dict[str, Any]:
        """Настройки плагина: сохранённые значения поверх значений по умолчанию (без секретов)."""
        stored = self.manager.stored_config(self.id)
        out: dict[str, Any] = {}
        for f in self.fields:
            if f.kind == "secret":
                continue
            if f.key in stored:
                try:
                    out[f.key] = f.coerce(stored[f.key])
                    continue
                except ValueError:
                    log.warning(
                        "плагин %s: неверное значение %s в настройках — беру по умолчанию", self.id, f.key
                    )
            out[f.key] = f.default_value()
        return out

    def schema(self) -> list[dict[str, Any]]:
        secrets = self.ctx.secrets
        return [f.to_json(lambda name: bool(secrets.get(name))) for f in self.fields]

    def secret(self, name: str) -> str:
        return self.ctx.secrets.get(name)

    # --- помощники ------------------------------------------------------------------------------------
    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Событие плагина plugin.<id>.<event> (из любого потока)."""
        self.ctx.bus.publish(f"plugin.{self.id}.{event}", data or {})

    def toast(self, title: str, message: str = "", *, status: str = "info", icon: str = "") -> None:
        self.ctx.bus.publish(
            "toast",
            {
                "title": title,
                "message": message,
                "status": status,
                "icon": icon or self.icon,
                "ts": time.time(),
            },
        )

    def changed(self) -> None:
        """Статус для плитки изменился — менеджер разошлёт plugins.changed (с задержкой, пачкой)."""
        self.manager.notify_changed()

    def commands_changed(self) -> None:
        """Набор голосовых команд изменился (новые устройства, компьютеры, имя питомца)."""
        self.manager.notify_commands_changed()

    def set_error(self, message: str) -> None:
        if message != self.error:
            self.error = message
            self.changed()

    def spawn(self, coro: Awaitable[Any], name: str) -> "asyncio.Task[Any]":
        """Фоновая задача плагина: ошибка пишется в журнал и в статус плагина; stop() менеджера её отменит."""

        async def guarded() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not isinstance(e, UserFacingError | PlatformError):
                    log.exception("плагин %s: задача %s упала", self.id, name)
                self.set_error(error_text(e))

        def finished(task: "asyncio.Task[Any]") -> None:
            self._tasks.discard(task)
            # задачу отменили до первого шага — закрыть корутину, чтобы не было «never awaited»
            if inspect.iscoroutine(coro) and inspect.getcoroutinestate(coro) == inspect.CORO_CREATED:
                coro.close()

        task = self.ctx.spawn(guarded(), f"plugin.{self.id}.{name}")
        self._tasks.add(task)
        task.add_done_callback(finished)
        return task

    async def cancel_tasks(self, timeout: float = 3.0) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()

    def http(self, timeout: float = 10.0, **kwargs: Any) -> httpx.AsyncClient:
        """HTTP-клиент плагина (в тестах — с MockTransport из self.http_transport или менеджера)."""
        transport = self.http_transport or self.manager.http_transport
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(timeout=timeout, **kwargs)

    def command(
        self,
        key: str,
        title: str,
        phrases: Iterable[str],
        handler: CommandHandler,
        *,
        icon: str = "",
    ) -> DynamicCommand:
        """Голосовая команда с id plugin.<id>:<key>; ошибка обработчика становится ExecResult(ok=False)."""
        command_icon = icon or self.icon

        async def run(slots: dict[str, Any]) -> ExecResult:
            if not self.running:
                return ExecResult(False, title, f"Плагин «{self.name}» выключен", icon=command_icon)
            try:
                result = await handler(dict(slots or {}))
            except (UserFacingError, PlatformError) as e:
                return ExecResult(False, title, str(e), icon=command_icon)
            except Exception as e:
                log.exception("плагин %s: команда %s упала", self.id, key)
                return ExecResult(
                    False, title, f"Ошибка плагина «{self.name}»: {error_text(e)}", icon="error"
                )
            if isinstance(result, ExecResult):
                return result
            return ExecResult(True, title, result or "Выполнено", icon=command_icon)

        return DynamicCommand(
            id=f"plugin.{self.id}:{key}",
            title=title,
            phrases=unique(phrase(p) for p in phrases),
            handler=run,
            category="plugins",
            icon=command_icon,
        )

    async def wait_tts_idle(self, timeout: float = 6.0, settle: float = 0.35) -> None:
        """Дождаться, пока Jarvis договорит («Готово, сэр» после голосовой команды), чтобы не слушать и не
        перебивать самого себя. settle — подождать, пока диспетчер успеет начать фразу."""
        if settle > 0:
            await asyncio.sleep(settle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                speaking = bool(self.ctx.tts.speaking)
            except Exception:
                return
            if not speaking:
                return
            await asyncio.sleep(0.1)
