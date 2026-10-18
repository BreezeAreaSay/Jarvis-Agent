"""Движки синтеза речи: piper (локально), windows, edge, openai, fish. Имена — как в settings.tts.engine."""

from typing import TYPE_CHECKING

from jarvis.voice.engines.base import Engine, SynthOptions, TtsEngineError
from jarvis.voice.engines.edge import EdgeEngine
from jarvis.voice.engines.fish import FishEngine
from jarvis.voice.engines.openai import OpenAIEngine
from jarvis.voice.engines.piper import PiperEngine
from jarvis.voice.engines.windows import WindowsEngine

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.voice.models import ModelManager

ENGINE_NAMES = ("piper", "windows", "edge", "openai", "fish")

__all__ = [
    "ENGINE_NAMES",
    "EdgeEngine",
    "Engine",
    "FishEngine",
    "OpenAIEngine",
    "PiperEngine",
    "SynthOptions",
    "TtsEngineError",
    "WindowsEngine",
    "create_engines",
]


def create_engines(ctx: "AppContext", models: "ModelManager") -> dict[str, Engine]:
    return {
        "piper": PiperEngine(models),
        "windows": WindowsEngine(),
        "edge": EdgeEngine(),
        "openai": OpenAIEngine(ctx),
        "fish": FishEngine(ctx),
    }
