"""Провайдеры Jarvis AI: адреса OpenAI-совместимых API, модели по умолчанию и примерные цены токенов.

Все провайдеры по ключу говорят на протоколе OpenAI /chat/completions; различия (как просить JSON-ответ,
понимает ли провайдер stream_options, как называется лимит длины ответа, какие ключи JSON Schema он не
принимает, как выглядят id вызовов инструментов) описаны в ProviderPreset и учитываются клиентом
jarvis.ai.llm. Цены — оценка для счётчика расходов и месячного лимита, точные суммы — в кабинете
провайдера.

Исключения — провайдеры без ключа API (group="account"): «ChatGPT (подписка)» — модели GPT по подписке ChatGPT
через Codex app-server (jarvis.ai.chatgpt), и провайдеры через официальные программы человека (cli=True):
Claude Code, Gemini CLI, Qwen Code (jarvis.ai.cli). login — вход в аккаунт вместо ключа, component — нужен
скачиваемый компонент (Codex).

Адреса и модели сверены в октябре 2026: Gemini — документация Gemini API и исходники Gemini CLI; xAI, Kimi,
MiniMax, Z.ai, DeepSeek, Model Studio — встроенные провайдеры Qwen Code 0.25; Doubao и Mistral — документация
OpenClaw 2026.10; YandexGPT — yandex-ai-studio-sdk (заголовки Api-Key и OpenAI-Project, модель gpt://каталог/…).
"""

from dataclasses import dataclass
from typing import Any, Literal

JsonMode = Literal["schema", "object", "prompt"]
Group = Literal["account", "key", "local", "custom"]


@dataclass(frozen=True)
class Region:
    """Регион провайдера: свой адрес API и свой кабинет для ключа (Китай / международный)."""

    id: str
    title: str
    base_url: str
    key_url: str = ""

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "base_url": self.base_url, "key_url": self.key_url}


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    title: str
    base_url: str  # "" — адрес задаёт человек (свой сервер) или провайдер без HTTP API (вход в аккаунт)
    needs_key: bool
    default_model: str
    models: tuple[str, ...]
    local: bool = False  # работает на этом ПК: без ключа, без прокси, бесплатно
    stream_usage: bool = True  # понимает stream_options.include_usage
    json_mode: JsonMode = "schema"  # как просить ответ строго в JSON
    max_tokens_param: str = "max_tokens"
    send_temperature: bool = True
    key_url: str = ""  # где получить ключ
    login: bool = False  # вход в аккаунт вместо ключа (ChatGPT, программы Claude/Gemini/Qwen)
    component: bool = False  # нужен скачиваемый компонент (Codex для ChatGPT)
    cli: bool = False  # работает через официальную программу человека (jarvis.ai.cli)
    group: Group = "key"  # группа в выборе провайдера
    country: str = ""  # cn | ru — значок страны у провайдера
    regions: tuple[Region, ...] = ()  # первый — по умолчанию (его base_url = base_url пресета)
    folder: bool = False  # нужен ID каталога Yandex Cloud (settings.ai.folder_id)
    auth_scheme: str = "Bearer"  # заголовок Authorization: «Bearer <ключ>» или «Api-Key <ключ>»
    drop_schema_keys: tuple[str, ...] = ()  # ключи JSON Schema, которые провайдер не принимает в инструментах
    short_tool_ids: bool = False  # id вызовов инструментов — ровно 9 латинских букв и цифр (Mistral)
    thought_signatures: bool = False  # Gemini 3: подпись рассуждений у вызовов инструментов (extra_content)
    retired: tuple[str, ...] = ()  # модели, которые провайдер убрал: вместо них — модель по умолчанию

    def region(self, base_url: str) -> Region | None:
        """Регион по адресу (пустой адрес — регион по умолчанию)."""
        url = (base_url or "").strip().rstrip("/")
        for region in self.regions:
            if region.base_url.rstrip("/") == url or (not url and region is self.regions[0]):
                return region
        return None

    def to_json(self, base_url: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "base_url": self.base_url if base_url is None else base_url,
            "needs_key": self.needs_key,
            "default_model": self.default_model,
            "models": list(self.models),
            "local": self.local,
            "key_url": self.key_url,
            "login": self.login,
            "component": self.component,
            "cli": self.cli,
            "group": self.group,
            "country": self.country,
            "regions": [r.to_json() for r in self.regions],
            "folder": self.folder,
        }


_SCHEMA_LENGTHS = ("minLength", "maxLength", "minItems", "maxItems", "minContains", "maxContains")

PRESETS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
        # --- по подписке, без ключа API ---------------------------------------------------------------------
        ProviderPreset(
            "chatgpt",
            "ChatGPT (подписка)",
            "",
            needs_key=False,
            # каталог моделей Codex 0.163.0 (model/list); настоящий список и умолчание — у Codex
            default_model="gpt-6.1-sol",
            models=(
                "gpt-6.1-sol",
                "gpt-6-astra",
                "gpt-6-sol",
                "gpt-6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
            ),
            login=True,
            component=True,
            group="account",
        ),
        # Claude Code: псевдонимы моделей --model (code.claude.com/docs/en/model-config); вход — в программе
        ProviderPreset(
            "claude_code",
            "Claude (подписка)",
            "",
            needs_key=False,
            default_model="sonnet",
            models=("sonnet", "opus", "haiku"),
            login=True,
            cli=True,
            group="account",
        ),
        # Gemini CLI: псевдонимы и модели из packages/core/src/config/models.ts (0.66, октябрь 2026)
        ProviderPreset(
            "gemini_cli",
            "Gemini CLI",
            "",
            needs_key=False,
            default_model="auto",
            models=("auto", "pro", "flash", "flash-lite", "gemini-3.5-flash", "gemini-3.1-pro-preview"),
            login=True,
            cli=True,
            group="account",
        ),
        # Qwen Code: модель по умолчанию — та, что выбрана в самом Qwen Code (/model); список — Coding Plan
        ProviderPreset(
            "qwen_code",
            "Qwen Code",
            "",
            needs_key=False,
            default_model="",
            models=(
                "qwen3.7-plus",
                "qwen3.6-plus",
                "qwen3-coder-plus",
                "qwen3.8-max",
                "qwen3.8-flash",
                "glm-5",
                "kimi-k2.5",
                "MiniMax-M2.5",
            ),
            login=True,
            cli=True,
            group="account",
            country="cn",
        ),
        # --- по ключу API -----------------------------------------------------------------------------------
        ProviderPreset(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            needs_key=True,
            default_model="gpt-5.4-mini",
            models=("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"),
            max_tokens_param="max_completion_tokens",
            key_url="https://platform.openai.com/api-keys",
        ),
        ProviderPreset(
            "anthropic",
            "Anthropic (Claude)",
            "https://api.anthropic.com/v1/",
            needs_key=True,
            default_model="claude-opus-5-5",
            models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
            # совместимый с OpenAI вход Anthropic не принимает response_format, а новые модели — temperature
            json_mode="prompt",
            send_temperature=False,
            key_url="https://console.anthropic.com/settings/keys",
        ),
        ProviderPreset(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            needs_key=True,
            default_model="gemini-3.5-flash",
            models=(
                "gemini-3.5-flash",
                "gemini-3.1-pro-preview",
                "gemini-3.1-flash-lite",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
            ),
            key_url="https://aistudio.google.com/apikey",
            thought_signatures=True,
        ),
        ProviderPreset(
            "xai",
            "xAI Grok",
            "https://api.x.ai/v1",
            needs_key=True,
            default_model="grok-4.7",
            models=("grok-4.7", "grok-4.6", "grok-4.5", "grok-4.3"),
            key_url="https://console.x.ai/",
        ),
        ProviderPreset(
            "mistral",
            "Mistral",
            "https://api.mistral.ai/v1",
            needs_key=True,
            default_model="mistral-large-latest",
            models=("mistral-large-latest", "mistral-medium-3-5", "mistral-small-latest"),
            stream_usage=False,  # расход приходит в последнем куске потока сам
            short_tool_ids=True,
            key_url="https://console.mistral.ai/api-keys",
        ),
        ProviderPreset(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            needs_key=True,
            default_model="openai/gpt-5.4-mini",
            models=(
                "openai/gpt-5.4-mini",
                "openai/gpt-5.4",
                "anthropic/claude-sonnet-5.5",
                "google/gemini-2.5-flash",
                "deepseek/deepseek-chat",
            ),
            key_url="https://openrouter.ai/keys",
        ),
        ProviderPreset(
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            needs_key=True,
            default_model="openai/gpt-oss-120b",
            models=("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"),
            key_url="https://console.groq.com/keys",
        ),
        # --- Китай -----------------------------------------------------------------------------------------
        # deepseek-chat и deepseek-reasoner DeepSeek убрал 24.07.2026 (их заменили модели V4)
        ProviderPreset(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com",
            needs_key=True,
            default_model="deepseek-v4-flash",
            models=("deepseek-v4-flash", "deepseek-v4-pro"),
            json_mode="object",
            key_url="https://platform.deepseek.com/api_keys",
            country="cn",
            retired=("deepseek-chat", "deepseek-reasoner"),
        ),
        ProviderPreset(
            "qwen",
            "Qwen (Alibaba Cloud)",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            needs_key=True,
            default_model="qwen3.7-plus",
            models=("qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen-plus", "qwen-flash"),
            json_mode="object",
            key_url="https://modelstudio.console.alibabacloud.com/",
            country="cn",
            regions=(
                Region(
                    "intl",
                    "Международный",
                    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    "https://modelstudio.console.alibabacloud.com/",
                ),
                Region(
                    "cn",
                    "Китай",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "https://bailian.console.aliyun.com/",
                ),
                Region(
                    "us",
                    "США",
                    "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                    "https://modelstudio.console.alibabacloud.com/us-east-1",
                ),
            ),
        ),
        ProviderPreset(
            "moonshot",
            "Kimi (Moonshot)",
            "https://api.moonshot.ai/v1",
            needs_key=True,
            default_model="kimi-k2.6",
            models=("kimi-k2.6", "kimi-k3", "kimi-k2.7-code"),
            json_mode="object",
            key_url="https://platform.kimi.ai/console/api-keys",
            country="cn",
            regions=(
                Region(
                    "intl",
                    "Международный",
                    "https://api.moonshot.ai/v1",
                    "https://platform.kimi.ai/console/api-keys",
                ),
                Region(
                    "cn",
                    "Китай",
                    "https://api.moonshot.cn/v1",
                    "https://platform.moonshot.cn/console/api-keys",
                ),
            ),
        ),
        ProviderPreset(
            "zhipu",
            "GLM (Z.ai)",
            "https://api.z.ai/api/paas/v4",
            needs_key=True,
            default_model="glm-5.2",
            models=("glm-5.2", "glm-5-turbo", "glm-5.3-flash", "glm-5"),
            json_mode="object",
            key_url="https://z.ai/manage-apikey/apikey-list",
            country="cn",
            regions=(
                Region(
                    "intl",
                    "Международный (Z.ai)",
                    "https://api.z.ai/api/paas/v4",
                    "https://z.ai/manage-apikey/apikey-list",
                ),
                Region(
                    "cn",
                    "Китай (Zhipu)",
                    "https://open.bigmodel.cn/api/paas/v4",
                    "https://open.bigmodel.cn/usercenter/apikeys",
                ),
            ),
        ),
        ProviderPreset(
            "minimax",
            "MiniMax",
            "https://api.minimax.io/v1",
            needs_key=True,
            default_model="MiniMax-M3",
            models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5"),
            json_mode="prompt",
            key_url="https://platform.minimax.io/user-center/basic-information/interface-key",
            country="cn",
            regions=(
                Region(
                    "intl",
                    "Международный",
                    "https://api.minimax.io/v1",
                    "https://platform.minimax.io/user-center/basic-information/interface-key",
                ),
                Region(
                    "cn",
                    "Китай",
                    "https://api.minimaxi.com/v1",
                    "https://platform.minimaxi.com/user-center/basic-information/interface-key",
                ),
            ),
        ),
        # Volcengine Ark (Китай) и BytePlus ModelArk (международный); списка /models у Ark нет
        ProviderPreset(
            "doubao",
            "Doubao (Volcengine)",
            "https://ark.cn-beijing.volces.com/api/v3",
            needs_key=True,
            default_model="doubao-seed-2-1-pro-260628",
            models=("doubao-seed-2-1-pro-260628", "doubao-seed-2-1-turbo-260628", "deepseek-v4-flash-260425"),
            json_mode="object",
            key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
            country="cn",
            drop_schema_keys=_SCHEMA_LENGTHS,  # Ark отвергает инструменты с ограничениями длины
            regions=(
                Region(
                    "cn",
                    "Китай (Volcengine)",
                    "https://ark.cn-beijing.volces.com/api/v3",
                    "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
                ),
                Region(
                    "intl",
                    "Международный (BytePlus)",
                    "https://ark.ap-southeast.bytepluses.com/api/v3",
                    "https://console.byteplus.com/ark/region:ark+ap-southeast-1/apiKey",
                ),
            ),
        ),
        # агрегатор открытых моделей; настоящий список приходит с /models, здесь — запасной
        ProviderPreset(
            "siliconflow",
            "SiliconFlow",
            "https://api.siliconflow.com/v1",
            needs_key=True,
            default_model="deepseek-ai/DeepSeek-V4-Flash",
            models=("deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4-Pro", "zai-org/GLM-5"),
            json_mode="object",
            key_url="https://cloud.siliconflow.com/account/ak",
            country="cn",
            regions=(
                Region(
                    "intl",
                    "Международный",
                    "https://api.siliconflow.com/v1",
                    "https://cloud.siliconflow.com/account/ak",
                ),
                Region(
                    "cn", "Китай", "https://api.siliconflow.cn/v1", "https://cloud.siliconflow.cn/account/ak"
                ),
            ),
        ),
        # --- Россия ----------------------------------------------------------------------------------------
        # Yandex AI Studio: совместимый с OpenAI API, ключ — «Api-Key <ключ>», каталог — заголовок
        # OpenAI-Project, модель — gpt://<каталог>/<модель>/<версия> (клиент дописывает каталог сам)
        ProviderPreset(
            "yandex",
            "YandexGPT",
            "https://llm.api.cloud.yandex.net/v1",
            needs_key=True,
            default_model="yandexgpt/latest",
            models=(
                "yandexgpt/latest",
                "yandexgpt-lite/latest",
                "aliceai-llm/latest",
                "qwen3-235b-a22b-fp8/latest",
                "gemma-3-27b-it/latest",
            ),
            stream_usage=False,
            json_mode="prompt",
            key_url="https://console.yandex.cloud/",
            country="ru",
            folder=True,
            auth_scheme="Api-Key",
        ),
        # --- на этом ПК и свой сервер -----------------------------------------------------------------------
        ProviderPreset(
            "ollama",
            "Ollama (на этом ПК)",
            "http://127.0.0.1:11434/v1",
            needs_key=False,
            default_model="qwen3:8b",
            models=("qwen3:8b", "qwen3:14b", "gemma3:12b", "llama3.1:8b"),
            local=True,
            group="local",
        ),
        ProviderPreset(
            "lmstudio",
            "LM Studio (на этом ПК)",
            "http://127.0.0.1:1234/v1",
            needs_key=False,
            default_model="qwen/qwen3-8b",
            models=("qwen/qwen3-8b", "google/gemma-3-12b"),
            local=True,
            stream_usage=False,
            group="local",
        ),
        ProviderPreset(
            "custom",
            "Свой сервер (OpenAI-совместимый)",
            "",
            needs_key=False,
            default_model="",
            models=(),
            stream_usage=False,
            group="custom",
        ),
    )
}


def get_preset(provider_id: str) -> ProviderPreset:
    return PRESETS.get(provider_id) or PRESETS["custom"]


# Цена за 1 млн токенов в долларах: (вход, выход). Это оценка по опубликованным прайсам — для счётчика
# расходов и месячного лимита; неизвестная модель считается бесплатной.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.28, 0.42),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "gpt-oss-120b": (0.15, 0.75),
    "gpt-oss-20b": (0.10, 0.50),
}


def _price_key(model: str) -> str:
    name = model.strip().lower().rsplit("/", 1)[-1]  # openai/gpt-5.4-mini → gpt-5.4-mini (OpenRouter, Groq)
    name = name.split(":", 1)[0]  # суффиксы вида «:free», «:beta»
    if name.startswith("claude-"):
        name = name.replace(".", "-")  # anthropic/claude-sonnet-5.5 (OpenRouter) → claude-sonnet-5-5
    return name


def price_for(model: str) -> tuple[float, float] | None:
    """Цена модели (вход, выход за 1 млн токенов) или None. Датированные версии — по базовой модели."""
    name = _price_key(model)
    if name in PRICES:
        return PRICES[name]
    best = ""
    for key in PRICES:
        if name.startswith(key + "-") and len(key) > len(best):
            best = key
    return PRICES[best] if best else None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, *, local: bool = False) -> float:
    if local:
        return 0.0
    price = price_for(model)
    if price is None:
        return 0.0
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
