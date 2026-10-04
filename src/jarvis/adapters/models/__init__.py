"""Бэкенды моделей. Создаёт их только composition root; ядро видит их через `ModelGateway`.

`OpenAICompatibleBackend` — локальный сервер (только loopback); `RemoteOpenAICompatibleBackend` —
провайдер в интернете (только https, ключ по ссылке из окружения).
"""

from jarvis.adapters.models.openai_compat import OpenAICompatibleBackend
from jarvis.adapters.models.remote import RemoteOpenAICompatibleBackend

__all__ = ["OpenAICompatibleBackend", "RemoteOpenAICompatibleBackend"]
