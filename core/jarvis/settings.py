"""Настройки Jarvis: модель (pydantic), хранение в <data>/settings.json и секреты (ключи API).

Секреты лежат отдельно, в <data>/secrets.dat: на Windows — зашифрованы DPAPI (расшифровать может только этот
пользователь Windows), на других системах — просто base64. Интерфейс видит только, задан ли секрет.

Меняя модель, поправь desktop/src/api/types.ts (тип Settings) в том же коммите.
"""

import base64
import contextlib
import copy
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jarvis import netproxy
from jarvis.bus import EventBus

log = logging.getLogger(__name__)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class ProfileSettings(_Model):
    name: str = ""
    address: str = "сэр"  # как Jarvis обращается к пользователю
    city: str = "Москва"  # город по умолчанию для погоды
    assistant_name: str = "Джарвис"


class InterfaceSettings(_Model):
    accent: str = "#3B82F6"  # цвет темы
    transparency: int = Field(30, ge=0, le=100)  # прозрачность панелей, %
    blur: int = Field(90, ge=0, le=100)  # размытие под панелями, %
    animations: bool = True
    language: Literal["ru", "en"] = "ru"
    minimize_to_tray: bool = True  # сворачивать в трей при закрытии
    always_on_top: bool = False
    window_material: Literal["none", "acrylic", "mica"] = "none"  # настоящая прозрачность окна (Windows 11)
    start_minimized: bool = False
    ui_scale: int = Field(100, ge=80, le=140)


CommandLanguage = Literal[
    "ru", "auto", "en", "uk", "de", "fr", "es", "it", "pt", "pl", "tr", "ja", "ko", "zh"
]


class VoiceSettings(_Model):
    enabled: bool = True  # микрофон слушает
    wake_words: list[str] = Field(default_factory=lambda: ["джарвис"])
    prefix_mode: bool = True  # команда только после слова-активатора
    silent_mode: bool = False  # тихий режим: без ответов голосом
    input_device: str = ""  # часть имени микрофона; "" — по умолчанию
    sensitivity: float = Field(0.5, ge=0.0, le=1.0)  # чувствительность детектора речи
    # итог фразы: GigaAM или Vosk на ПК, или ElevenLabs Scribe (облако по ключу ElevenLabs; «Джарвис» — на ПК)
    asr_model: Literal["gigaam-v3", "gigaam-v3-punct", "vosk-small", "elevenlabs"] = "gigaam-v3"
    # язык команд: ru — GigaAM; другой или auto — итоговый текст фразы распознаёт Whisper (скачается сам)
    language: CommandLanguage = "ru"
    end_silence_ms: int = Field(450, ge=300, le=2000)  # столько тишины после речи — фраза закончилась
    follow_up_s: int = Field(6, ge=0, le=30)  # слушать продолжение без активатора после ответа
    ask_more: bool = True  # после ответа голосом — «Что-нибудь ещё?» и слушать продолжение без активатора
    ptt_key: str = ""  # клавиша «держи и говори», например "right ctrl"; "" — выключено
    earcons: bool = True  # короткие звуки «слушаю», «готово», «ошибка»
    max_utterance_s: int = Field(15, ge=3, le=60)
    confirm_by_voice: bool = True  # «да/нет» голосом для подтверждений команд
    # необратимое (отправить, оплатить, удалить, ввести пароль…) разрешать и голосовым «да» — по умолчанию
    # только кнопкой: голос могут подслушать или распознать неверно (jarvis/confirm.py)
    confirm_high_by_voice: bool = False
    speak_confirmations: Literal["short", "off"] = "short"  # озвучивать итог команд («Готово, сэр»)
    # приветствие, когда человек вернулся к компьютеру (jarvis.voice.presence): off — нет, news — только если
    # есть что сказать (напоминания), always — всегда; молчит над играми, в звонках и в тихом режиме
    presence_greeting: Literal["off", "news", "always"] = "off"
    presence_away_min: int = Field(20, ge=5, le=240)  # столько минут без ввода — «человек отошёл»


class TtsSettings(_Model):
    engine: Literal["piper", "windows", "edge", "openai", "fish", "elevenlabs"] = "piper"
    piper_voice: Literal["ruslan", "denis", "dmitri", "irina"] = "ruslan"
    windows_voice: str = ""
    edge_voice: str = "ru-RU-DmitryNeural"
    openai_model: str = "gpt-4o-mini-tts"
    openai_voice: str = "onyx"
    openai_instructions: str = (
        "Говори как J.A.R.V.I.S. из «Железного человека»: спокойно, уверенно, учтиво, с лёгкой иронией."
    )
    fish_voice_id: str = ""  # reference_id голоса Fish Audio
    fish_model: str = "s1"
    elevenlabs_voice_id: str = ""  # "" — George (стандартный британский голос, есть на бесплатном тарифе)
    elevenlabs_model: str = "eleven_flash_v2_5"  # быстрая, говорит по-русски, символ — полкредита
    elevenlabs_stability: float = Field(0.5, ge=0.0, le=1.0)
    elevenlabs_similarity: float = Field(0.75, ge=0.0, le=1.0)
    elevenlabs_style: float = Field(0.0, ge=0.0, le=1.0)  # выразительность: больше — живее, но медленнее
    speed: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(1.0, ge=0.8, le=1.2)
    effect: float = Field(0.35, ge=0.0, le=1.0)  # «электроника» в голосе
    stress_dict: bool = True  # голос Jarvis (piper): ударения и «ё» по словарю Jarvis (jarvis.voice.stress)
    volume: int = Field(100, ge=0, le=100)
    output_device: str = ""


AiProvider = Literal[
    "openai",
    "chatgpt",
    "claude_code",
    "gemini_cli",
    "qwen_code",
    "openrouter",
    "deepseek",
    "anthropic",
    "gemini",
    "xai",
    "mistral",
    "groq",
    "qwen",
    "moonshot",
    "zhipu",
    "minimax",
    "doubao",
    "siliconflow",
    "yandex",
    "ollama",
    "lmstudio",
    "custom",
]
# chatgpt — подписка ChatGPT через Codex (вход в аккаунт, без ключа API; компонент скачивается по кнопке);
# claude_code, gemini_cli, qwen_code — через официальные программы человека (вход — в самой программе)


class AiSettings(_Model):
    enabled: bool = True
    provider: AiProvider = "openai"
    base_url: str = ""  # пусто — адрес провайдера по умолчанию
    model: str = "gpt-5.4-mini"
    temperature: float = Field(0.6, ge=0.0, le=2.0)
    proxy: str = ""  # http://… или socks5://… для запросов к ИИ; пусто — системный прокси (VPN) или напрямую
    persona: Literal["jarvis", "plain"] = "jarvis"
    custom_instructions: str = ""
    web_search: bool = True
    memory: bool = True  # запоминать факты о пользователе
    mode: Literal["chat", "all", "agent"] = "all"  # режим по умолчанию: чат / всё вместе / агент
    # скорость ответа: fast — модель думает меньше (усилие low), deep — дольше и тщательнее, balanced — как у
    # модели по умолчанию
    speed: Literal["fast", "balanced", "deep"] = "fast"
    # «Всё вместе»: дело на компьютере не из лёгких («напиши Ане…», «поставь будильник…», «что на экране?») —
    # сразу агенту, без лишнего ответа модели; чату — одна простая команда, сведения, память, заметки
    auto_agent: bool = True
    # модель понимает картинки (снимки окон для агента, картинки в чате): auto — по провайдеру и модели
    vision: Literal["auto", "on", "off"] = "auto"
    agent_confirm: Literal["risky", "always"] = "risky"
    max_agent_steps: int = Field(25, ge=3, le=100)
    agent_notes: bool = True  # агент запоминает, что не сработало в программе (ai/lessons.py)
    # пароли, ключи API, номера карт и закрытые ключи в тексте — маской, прежде чем он уйдёт облачной модели
    # (ChatGPT, Claude, ключ API не на этом ПК); модели на этом ПК (Ollama, LM Studio) видят как есть
    mask_secrets: bool = True
    # окна, которые ИИ не видит и где агент не работает (кроме менеджеров паролей — они скрыты всегда):
    # программа («keepass», «telegram.exe») или слово из заголовка окна (jarvis/ai/private_windows.py)
    private_apps: list[str] = Field(default_factory=list)
    agent_journal: bool = True  # шаги агента и снимки экрана — в журнал на этом компьютере (ai/journal.py)
    # браузер Jarvis: свой профиль Chrome или Edge, агент работает со страницами по CDP (ai/browser)
    browser: bool = False
    browser_app: Literal["auto", "chrome", "edge"] = "auto"
    monthly_budget: float = Field(0.0, ge=0.0)  # лимит расходов в месяц, $; 0 — без лимита
    folder_id: str = ""  # ID каталога Yandex Cloud (для YandexGPT)
    # запасной ИИ (по ключу API или на этом ПК): основной не отвечает (квота, лимит подписки, регион, сбой,
    # нет связи) — отвечает он, основной какое-то время не зовётся (ai/llm.py, OUTAGE_COOLDOWN); "" — нет
    fallback_provider: AiProvider | Literal[""] = ""
    fallback_model: str = ""  # пусто — модель провайдера по умолчанию
    fallback_base_url: str = ""  # пусто — адрес провайдера по умолчанию
    # разрешения инструментов ИИ по имени: allow / ask / deny; auto или нет записи — по риску (ai/policy.py)
    tool_policy: dict[str, Literal["auto", "allow", "ask", "deny"]] = Field(default_factory=dict)

    @field_validator("proxy", mode="before")
    @classmethod
    def _proxy(cls, value: Any) -> Any:
        # «127.0.0.1:1080» → «http://127.0.0.1:1080», «socks4://…» → «socks5://…»: без схемы адрес не понимают
        # ни httpx, ни Claude Code
        return netproxy.normalize(value) if isinstance(value, str) else value

    @field_validator("vision", mode="before")
    @classmethod
    def _vision(cls, value: Any) -> Any:
        # до 2.1 был флажок (по умолчанию выключен): включённый — «on», остальное — «auto»
        if value is True:
            return "on"
        if value is False or value is None:
            return "auto"
        return value


NotifyChannel = Literal["toast", "voice", "telegram"]


class BackgroundSettings(_Model):
    """Фоновые задачи (jarvis/background): хранилище заметок, очередь, самостоятельность, уведомления."""

    vault: str = ""  # хранилище Obsidian (папка); пусто — найти самому, нет Obsidian — Документы\Jarvis
    folder: str = "Jarvis"  # своя папка агента в хранилище: пишет он только сюда
    read_vault: bool = True  # читать и искать всё хранилище (иначе — только свою папку)
    inbox: bool = True  # строки в «Входящие.md» своей папки — новые задачи (jarvis.background.inbox)
    parallel: int = Field(2, ge=1, le=4)  # сколько задач идёт одновременно
    # самостоятельность: careful — спрашивать чаще, normal — неясное и необратимое, bold — деньги и вход
    autonomy: Literal["careful", "normal", "bold"] = "normal"
    # первый план задачи — человеку на утверждение: big — план из 4+ шагов или повторяющаяся задача
    approve_plan: Literal["never", "big", "always"] = "big"
    max_steps: int = Field(150, ge=10, le=1000)  # действий на задачу
    max_hours: float = Field(4.0, ge=0.25, le=48.0)  # часов работы на задачу (без ожидания ответа)
    # модель фоновых задач по ключу API (подешевле — для долгих задач); пусто — та же, что в чате
    model: str = Field("", max_length=200)
    notify_question: list[NotifyChannel] = Field(default_factory=lambda: ["toast"])  # есть вопрос
    notify_done: list[NotifyChannel] = Field(default_factory=lambda: ["toast"])  # задача закончена


class AvatarSettings(_Model):
    desktop: bool = True  # живой аватар на рабочем столе
    size: int = Field(150, ge=60, le=400)
    on_top: bool = True
    animated: bool = True  # анимация аватара
    position: list[int] | None = None  # [x, y] или None — правый нижний угол
    hide_fullscreen: bool = True  # прятать над играми и видео во весь экран


class SystemSettings(_Model):
    autostart: bool = True
    hotkey_show: str = "Ctrl+Alt+J"
    hotkey_listen: str = "Ctrl+Alt+Space"  # ещё раз, пока слушает или отвечает, — «стоп»
    hotkey_mic: str = "Ctrl+Alt+M"  # микрофон Jarvis: выключить / включить
    check_updates: bool = True
    auto_update: bool = True  # найденное обновление скачивать самому; ставится при перезапуске
    overlay: bool = True  # всплывающая строка голоса и уведомления поверх окон


class RemoteSettings(_Model):
    lan_enabled: bool = False  # пульт в браузере телефона по Wi-Fi
    lan_port: int = Field(8766, ge=1024, le=65535)
    telegram_enabled: bool = False
    telegram_owner_id: int = 0  # кому бот подчиняется (привязывается кодом)
    favorites: list[str] = Field(default_factory=list)  # id команд на главном экране пульта


class PrivacySettings(_Model):
    """Сроки хранения (дни; 0 — хранить, пока не удалите сами). Чистит jarvis.retention раз в сутки."""

    history_days: int = Field(90, ge=0, le=3650)  # история команд
    chat_days: int = Field(0, ge=0, le=3650)  # чаты Jarvis AI без новых сообщений (закреплённые не трогаем)
    voice_chat_days: int = Field(30, ge=0, le=3650)  # сообщения голосового чата, Telegram и пульта
    tool_results_days: int = Field(30, ge=0, le=3650)  # скрытые результаты инструментов и картинки в чатах
    audio_days: int = Field(1, ge=0, le=365)  # записи созвонов «Конспекта» (если их не велено хранить)
    logs_days: int = Field(14, ge=0, le=365)  # журналы


class PluginState(_Model):
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class Settings(_Model):
    version: int = 1
    onboarding_done: bool = False
    profile: ProfileSettings = Field(default_factory=ProfileSettings)
    interface: InterfaceSettings = Field(default_factory=InterfaceSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    tts: TtsSettings = Field(default_factory=TtsSettings)
    ai: AiSettings = Field(default_factory=AiSettings)
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    system: SystemSettings = Field(default_factory=SystemSettings)
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    plugins: dict[str, PluginState] = Field(default_factory=dict)
    packs: list[str] = Field(default_factory=list)  # установленные паки команд
    disabled_commands: list[str] = Field(default_factory=list)  # выключенные встроенные команды


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Слить patch в копию base: словари — рекурсивно, остальное (включая списки) — заменой."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


ChangeListener = Callable[[Settings, Settings], None]


class SettingsStore:
    """Настройки в памяти + атомарная запись в файл. patch() публикует settings.changed."""

    def __init__(self, path: Path, bus: EventBus | None = None) -> None:
        self.path = path
        self._bus = bus
        self._lock = threading.RLock()
        self._listeners: list[ChangeListener] = []
        self._value = self._load()

    @property
    def value(self) -> Settings:
        return self._value

    def get(self) -> Settings:
        return self._value

    def _load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return Settings.model_validate(raw)
        except (OSError, ValueError, ValidationError) as e:
            log.error("настройки не читаются (%s) — беру значения по умолчанию, файл сохраню как .bad", e)
            with contextlib.suppress(OSError):
                self.path.replace(self.path.with_suffix(".json.bad"))
            return Settings()

    def save(self) -> None:
        with self._lock:
            data = self._value.model_dump(mode="json")
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def patch(self, data: dict[str, Any]) -> Settings:
        """Изменить часть настроек. Неверные значения — ValidationError (ничего не меняется)."""
        with self._lock:
            old = self._value
            merged = deep_merge(old.model_dump(mode="json"), data)
            new = Settings.model_validate(merged)
            self._value = new
            self.save()
        self._notify(old, new)
        return new

    def replace(self, new: Settings) -> Settings:
        with self._lock:
            old = self._value
            self._value = new
            self.save()
        self._notify(old, new)
        return new

    def on_change(self, listener: ChangeListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self, old: Settings, new: Settings) -> None:
        for listener in list(self._listeners):
            try:
                listener(old, new)
            except Exception:
                log.exception("обработчик изменения настроек упал")
        if self._bus is not None:
            self._bus.publish("settings.changed", {"settings": new.model_dump(mode="json")})


# --- секреты --------------------------------------------------------------------------------------

SECRET_NAMES = (
    "ai_api_key",  # ключ выбранного провайдера ИИ
    "ai_fallback_api_key",  # ключ запасного провайдера ИИ (settings.ai.fallback_provider)
    "openai_api_key",  # ключ OpenAI для голоса (если ИИ — другой провайдер)
    "fish_api_key",
    "elevenlabs_api_key",  # голоса ElevenLabs
    "telegram_token",
    "ha_token",  # Home Assistant
    "yandex_iot_token",  # Умный дом Яндекса
    "remote_token",  # пароль пульта и второго компьютера
)


def _protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore[import-not-found]

            return b"DPAPI:" + win32crypt.CryptProtectData(data, "Jarvis", None, None, None, 0)
        except Exception:
            log.warning("DPAPI недоступен — секреты хранятся без шифрования")
    return b"B64:" + base64.b64encode(data)


def _unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"DPAPI:"):
        import win32crypt  # type: ignore[import-not-found]

        return win32crypt.CryptUnprotectData(blob[6:], None, None, None, 0)[1]
    if blob.startswith(b"B64:"):
        return base64.b64decode(blob[4:])
    raise ValueError("неизвестный формат файла секретов")


class Secrets:
    def __init__(self, path: Path, bus: EventBus | None = None) -> None:
        self.path = path
        self._bus = bus
        self._lock = threading.Lock()
        self._values: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(_unprotect(self.path.read_bytes()).decode("utf-8"))
            return {str(k): str(v) for k, v in raw.items()}
        except Exception as e:
            log.error("секреты не читаются: %s", e)
            return {}

    def get(self, name: str) -> str:
        return self._values.get(name, "")

    def set(self, name: str, value: str) -> None:
        with self._lock:
            value = value.strip()
            if value:
                self._values[name] = value
            else:
                self._values.pop(name, None)
            payload = json.dumps(self._values, ensure_ascii=False).encode("utf-8")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(_protect(payload))
            os.replace(tmp, self.path)
        if self._bus is not None:
            self._bus.publish("secrets.changed", {"name": name, "set": bool(value)})

    def status(self) -> dict[str, bool]:
        return {name: bool(self._values.get(name)) for name in SECRET_NAMES}
