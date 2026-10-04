"""Ссылки на секреты (ADR 0027): в конфиге — `api_key = "env:ИМЯ"`, значение — в окружении.

Значение читается только здесь и только при сборке приложения; в `JarvisConfig`, в `config show`, в
трассе и в журналах его нет. Ссылку проверяет схема настроек (`SECRET_REF`).
"""

import os
from collections.abc import Mapping

from jarvis.domain.settings import SECRET_REF


def resolve_secret(reference: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Значение секрета по ссылке `env:ИМЯ`; None — переменная не задана или пуста."""
    if SECRET_REF.fullmatch(reference) is None:
        raise ValueError("ссылка на секрет должна иметь вид env:ИМЯ")  # значение ссылки не повторяется
    environ = os.environ if env is None else env
    value = environ.get(reference.removeprefix("env:"), "")
    return value.strip() or None
