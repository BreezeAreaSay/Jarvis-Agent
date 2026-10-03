"""Бэкенды моделей. Создаёт их только composition root; ядро видит их через `ModelGateway`."""

from jarvis.adapters.models.openai_compat import OpenAICompatibleBackend

__all__ = ["OpenAICompatibleBackend"]
