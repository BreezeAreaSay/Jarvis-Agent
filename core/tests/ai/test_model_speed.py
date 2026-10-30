"""Скорость модели (время до первого токена и токены в секунду) для проверочных прогонов и предупреждение о
маленькой модели на этом ПК для долгих задач."""

import dataclasses

import pytest

from jarvis.ai.llm import speed_stats, weak_local_model
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, reply


async def test_timings_recorded_per_request(ctx: AppContext, llm: ScriptedLlm) -> None:
    llm.add(reply("Привет, сэр"), reply("Ещё раз"))
    await ctx.ai.llm.chat([{"role": "user", "content": "Привет"}], stream=True, on_delta=lambda _: None)  # type: ignore[attr-defined]
    await ctx.ai.llm.chat([{"role": "user", "content": "Ещё"}])  # type: ignore[attr-defined]
    timings = list(ctx.ai.llm.timings)  # type: ignore[attr-defined]
    assert [t["model"] for t in timings] == ["gpt-5.4-mini", "gpt-5.4-mini"]
    assert timings[0]["ttft"] is not None and timings[1]["ttft"] is None  # без потока — нет первого токена
    assert all(t["completion_tokens"] == 20 and t["seconds"] >= 0 for t in timings)
    stats = speed_stats(timings)
    assert stats["requests"] == 2 and stats["ttft_s"] is not None and stats["tokens_per_s"] > 0


def test_speed_stats_math() -> None:
    timings = [
        {"seconds": 2.0, "ttft": 0.5, "completion_tokens": 30},
        {"seconds": 1.0, "ttft": None, "completion_tokens": 10},
    ]
    stats = speed_stats(timings)
    assert stats == {"requests": 2, "ttft_s": 0.5, "tokens_per_s": round(40 / 2.5, 1)}
    assert speed_stats([]) == {"requests": 0, "ttft_s": None, "tokens_per_s": None}


@pytest.mark.parametrize(
    ("provider", "model", "weak"),
    [
        ("ollama", "qwen3:8b", True),
        ("ollama", "llama3.1:8b", True),
        ("lmstudio", "gemma-3-12b-it", True),
        ("ollama", "qwen3:32b", False),
        ("ollama", "llama3.3:70b", False),
        ("ollama", "mistral-small", False),  # размер неизвестен — не пугаем
        ("openai", "gpt-5.4-mini", False),  # облачная
    ],
)
def test_weak_local_model(ctx: AppContext, provider: str, model: str, weak: bool) -> None:
    ctx.settings.patch({"ai": {"provider": provider, "model": model}})
    cfg = dataclasses.replace(ctx.ai.llm.primary_config(), model=model)  # type: ignore[attr-defined]
    warning = weak_local_model(cfg)
    assert bool(warning) is weak
    if weak:
        assert model in warning and "долгих задач" in warning
