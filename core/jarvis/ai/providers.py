"""Провайдеры Jarvis AI: адреса OpenAI-совместимых API, модели по умолчанию и примерные цены токенов.

Все провайдеры говорят на протоколе OpenAI /chat/completions; различия (как просить JSON-ответ, понимает ли
провайдер stream_options, как называется лимит длины ответа) описаны в ProviderPreset и учитываются клиентом
jarvis.ai.llm. Цены — оценка для счётчика расходов и месячного лимита, точные суммы — в кабинете провайдера.
"""

from dataclasses import dataclass
from typing import Any, Literal

JsonMode = Literal["schema", "object", "prompt"]


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    title: str
    base_url: str  # "" — адрес задаёт человек (свой сервер)
    needs_key: bool
    default_model: str
    models: tuple[str, ...]
    local: bool = False  # работает на этом ПК: без ключа, без прокси, бесплатно
    stream_usage: bool = True  # понимает stream_options.include_usage
    json_mode: JsonMode = "schema"  # как просить ответ строго в JSON
    max_tokens_param: str = "max_tokens"
    send_temperature: bool = True
    key_url: str = ""  # где получить ключ

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
        }


PRESETS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
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
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            needs_key=True,
            default_model="deepseek-chat",
            models=("deepseek-chat", "deepseek-reasoner"),
            json_mode="object",
            key_url="https://platform.deepseek.com/api_keys",
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
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            needs_key=True,
            default_model="openai/gpt-oss-120b",
            models=("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"),
            key_url="https://console.groq.com/keys",
        ),
        ProviderPreset(
            "ollama",
            "Ollama (на этом ПК)",
            "http://127.0.0.1:11434/v1",
            needs_key=False,
            default_model="qwen3:8b",
            models=("qwen3:8b", "qwen3:14b", "gemma3:12b", "llama3.1:8b"),
            local=True,
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
        ),
        ProviderPreset(
            "custom",
            "Свой сервер (OpenAI-совместимый)",
            "",
            needs_key=False,
            default_model="",
            models=(),
            stream_usage=False,
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
