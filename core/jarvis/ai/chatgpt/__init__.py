"""«ChatGPT (подписка)»: Jarvis AI через Codex app-server и вход в аккаунт ChatGPT (без ключа API).

component — скачиваемый компонент (SDK openai-codex с бинарём Codex) и настройки запуска; runtime — процесс
Codex; turns — разбор событий хода; prompt — инструкции и контекст хода; errors — ошибки по-русски;
backend — провайдер для jarvis.ai.service.Ai и агента.
"""

from jarvis.ai.chatgpt.backend import ChatGpt

__all__ = ["ChatGpt"]
