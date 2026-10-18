"""Провайдеры Jarvis AI через официальные программы человека: Claude Code, Gemini CLI, Qwen Code.

locate — где программа и как её запускать без cmd.exe; process — процесс без окна консоли; specs — флаги,
окружение и настройки каждой программы; stream — разбор stream-json; prompt — промпты; errors — ошибки
по-русски; backend — провайдер для jarvis.ai.service.Ai и агента.
"""

from jarvis.ai.cli.backend import CliBackend, CliProviders

__all__ = ["CliBackend", "CliProviders"]
