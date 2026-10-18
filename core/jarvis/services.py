"""Интерфейсы сервисов ядра — договор между модулями (подробно — docs/CONTRACTS.md).

Каждый сервис живёт в своём пакете и получает AppContext в конструкторе; соседей зовёт через ctx.
Все сервисы умеют start()/stop() (async). Ошибки для человека — UserFacingError с текстом по-русски.
"""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

Source = Literal["voice", "text", "ui", "remote", "telegram", "ai", "plugin", "agent"]


class UserFacingError(RuntimeError):
    """Ошибка с понятным человеку текстом (уходит в интерфейс и голос как есть)."""


# --- команды ---------------------------------------------------------------------------------------


@dataclass
class ExecResult:
    """Итог выполнения команды (или цепочки команд из одной фразы)."""

    ok: bool
    title: str  # что сделано: «Включить музыку»
    message: str = ""  # подробность или текст ошибки: «Выполнено», «Spotify не найден»
    icon: str = ""  # ключ иконки для тоста: spotify, chrome, volume, system…
    say: str = ""  # что сказать голосом ("" — короткое подтверждение по настройкам)
    command_ids: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchedPart:
    command_id: str
    title: str
    text: str  # часть фразы, которая совпала
    score: float  # 0..100
    slots: dict[str, Any] = field(default_factory=dict)  # {"number": 50, "app": "Spotify", "query": "…"}


@dataclass
class CommandMatch:
    text: str  # нормализованная фраза целиком
    parts: list[MatchedPart]  # несколько — «сверни все окна и включи музыку»

    @property
    def score(self) -> float:
        return min((p.score for p in self.parts), default=0.0)


@dataclass
class DynamicCommand:
    """Команда, которую добавляет плагин или сервис на лету (умный дом, столы, второй ПК)."""

    id: str  # уникальный: "desk.open:games", "ha.toggle:light.kitchen"
    title: str
    phrases: list[str]  # шаблоны фраз, как у обычных команд (поддерживают {number}, {text} …)
    handler: Callable[[dict[str, Any]], Awaitable[ExecResult]]  # slots → результат
    category: str = "plugins"
    icon: str = ""


DynamicProvider = Callable[[], list[DynamicCommand]]


class CommandService(Protocol):
    def match(self, text: str) -> CommandMatch | None:
        """Фраза → команды. None — ничего не подошло (тогда фразу получает ИИ)."""
        ...

    async def execute(self, match: CommandMatch, source: Source) -> ExecResult: ...

    async def run_text(self, text: str, source: Source) -> ExecResult | None:
        """match + execute; None — фраза не команда."""
        ...

    async def run_command(
        self, command_id: str, slots: dict[str, Any] | None = None, source: Source = "ui"
    ) -> ExecResult: ...

    def register_provider(self, name: str, provider: DynamicProvider) -> None:
        """Источник динамических команд; список перечитывается при каждом refresh_dynamic()."""
        ...

    def refresh_dynamic(self) -> None: ...

    def describe(self, limit: int = 300) -> list[dict[str, Any]]:
        """Краткий список команд для ИИ: id, title, фразы-примеры."""
        ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# --- голос -----------------------------------------------------------------------------------------

VoiceState = Literal["off", "loading", "idle", "listening", "processing", "speaking", "error"]


class VoiceService(Protocol):
    state: VoiceState

    async def set_enabled(self, enabled: bool) -> None: ...

    async def listen_now(self) -> None:
        """Начать слушать команду без слова-активатора (кнопка микрофона, хоткей)."""
        ...

    async def stop_listening(self) -> None: ...

    def follow_up(self, seconds: float | None = None) -> None:
        """После ответа слушать продолжение без активатора (настройка voice.follow_up_s)."""
        ...

    async def ask_yes_no(self, timeout: float = 8.0) -> bool | None:
        """Выслушать «да» / «нет» (подтверждение команды). None — не расслышал или нет ответа."""
        ...

    async def capture_text(self, timeout: float = 15.0, *, punct: bool = True) -> str:
        """Выслушать одну фразу и вернуть текст, не выполняя её (диктовка, ответы плагинам)."""
        ...

    async def transcribe(self, samples: Any, rate: int, *, language: str = "ru") -> str:
        """Распознать готовый звук (голосовое из Telegram, запись созвона, субтитры). samples — float32 моно.

        language: "ru" — GigaAM; другой язык или "auto" — многоязычная модель (Whisper), если скачана,
        иначе UserFacingError с подсказкой, какую модель скачать.
        """
        ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class TtsService(Protocol):
    @property
    def speaking(self) -> bool: ...

    async def say(self, text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        """Сказать вслух. interrupt — оборвать текущую речь; wait — дождаться конца."""
        ...

    def silence(self) -> None:
        """Замолчать сейчас и очистить очередь фраз."""
        ...

    async def synthesize(self, text: str) -> tuple[Any, int]:
        """Синтез без воспроизведения: (float32 моно, частота). Для «Чтения вслух» с прогрессом."""
        ...

    async def play(self, samples: Any, rate: int, *, wait: bool = True) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# --- ИИ --------------------------------------------------------------------------------------------


@dataclass
class AskResult:
    chat_id: str
    text: str  # итоговый ответ ассистента
    spoken: str = ""  # короткая версия для голоса ("" — ответ целиком годится для голоса)
    actions: list[dict[str, Any]] = field(default_factory=list)  # выполненные инструменты (карточки)
    sources: list[dict[str, Any]] = field(default_factory=list)  # [{title, url}]
    error: str = ""


class AiService(Protocol):
    @property
    def configured(self) -> bool:
        """Есть провайдер и (если нужен) ключ."""
        ...

    async def ask(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        source: Source = "text",
        mode: str | None = None,
        images: list[str] | None = None,
    ) -> AskResult:
        """Сообщение в чат с инструментами; события chat.* идут в шину по ходу ответа."""
        ...

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Одиночный запрос к модели без инструментов. С json_schema возвращает dict, иначе str."""
        ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class AgentService(Protocol):
    async def run(self, task: str, source: Source = "text") -> str:
        """Запустить агента («работает сам»); возвращает id запуска. Ход — события agent.*"""
        ...

    async def stop_run(self) -> None: ...

    def resolve_approval(self, approval_id: str, allow: bool) -> bool: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class NotesService(Protocol):
    """Заметки, задачи, календарь, напоминания и память о пользователе (таблицы jarvis.db)."""

    def memory_list(self) -> list[dict[str, Any]]: ...
    def memory_add(self, text: str, source: str = "ai") -> dict[str, Any]: ...
    def memory_delete(self, memory_id: str) -> bool: ...
    def memory_prompt(self) -> str:
        """Факты о пользователе одной строкой для системного промпта ИИ."""
        ...

    def note_add(self, title: str, body: str = "") -> dict[str, Any]: ...
    def task_add(self, title: str, due: float | None = None) -> dict[str, Any]: ...
    def event_add(self, title: str, start: float, end: float | None = None, **kw: Any) -> dict[str, Any]: ...
    def reminder_add(self, text: str, at: float, repeat: str | None = None) -> dict[str, Any]: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class McpService(Protocol):
    def tools(self) -> list[dict[str, Any]]:
        """Инструменты подключённых серверов для function-calling (имя — mcp__<сервер>__<инструмент>)."""
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# --- плагины, пульт, виджеты, столы ------------------------------------------------------------------


class PluginManager(Protocol):
    def list(self) -> list[dict[str, Any]]: ...
    def has(self, plugin_id: str) -> bool: ...
    async def update(
        self, plugin_id: str, *, enabled: bool | None = None, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Включить/выключить и поменять настройки (секреты схемы: null — удалить, "" — оставить как есть)."""
        ...

    async def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]: ...
    async def action(self, plugin_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class RemoteService(Protocol):
    def info(self) -> dict[str, Any]: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class WidgetService(Protocol):
    async def data(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        """Данные виджета: weather, rates, crypto, stocks, news, system, media, recent, apps, history…"""
        ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class DeskService(Protocol):
    def list(self) -> list[dict[str, Any]]: ...
    def open(self, desk_id_or_name: str) -> dict[str, Any] | None:
        """Показать стол в интерфейсе (событие desk.open). None — такого стола нет."""
        ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class Dispatcher(Protocol):
    async def handle(self, text: str, source: Source, *, speak: bool | None = None) -> dict[str, Any]:
        """Главный вход: фраза человека → команда, ИИ или «не понял». Возвращает итог для клиента."""
        ...
