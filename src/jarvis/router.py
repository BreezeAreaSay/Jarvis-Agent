"""Роутер: какой уровень обработает запрос — грамматика, руки, мозг или «нужно GPT, а он выключен».

Порядок: префикс («локально:», «gpt:», «думай:») → грамматика → эвристики мозга (без модели, ≤1 мс) → руки.
Без облака (mode == "local" или префикс «локально:») всё, что ушло бы мозгу, получает level "local" и reason
"local:needs_gpt" — core покажет «Это нужно GPT, а включён локальный режим»; грамматика и руки работают как
обычно, а Route.local_only=True говорит core, что и после рук (ask_gpt, ошибка) мозг недоступен.
Ответ рук ask_gpt или ошибка → мозг (reason «hands→ask_gpt» / «hands→error») решает core, не роутер.
"""

import re
from dataclasses import dataclass
from typing import Literal

from jarvis import grammar
from jarvis.context import Context
from jarvis.grammar import GrammarHit

RouteLevel = Literal["grammar", "hands", "brain", "local"]


@dataclass(frozen=True)
class Route:
    level: RouteLevel
    reason: str  # короткая строка для журнала: «grammar:open», «heur:question», «hands→ask_gpt»
    text: str  # текст без префикса
    deep: bool = False  # мозг: основная модель (high), а не быстрая
    hit: GrammarHit | None = None  # для level == "grammar"
    local_only: bool = False  # облако недоступно для этого запроса (префикс «локально:» или mode == "local")


_PREFIX = re.compile(r"^\s*(локально|gpt|гпт|думай)\s*:\s*", re.IGNORECASE)

_QUESTION = re.compile(
    r"(?<!\w)(?:почему|зачем|как (?:сделать|мне|правильно|работает|настроить|исправить|починить|установить"
    r"|удалить|убрать|включить|отключить|выключить|понять)|что (?:такое|значит|означает|лучше)"
    r"|кто (?:такой|такая|такие)|чем (?:отличается|отличаются)|в чем разница"
    r"|объясни|объясните|сравни|сравните|посоветуй|посоветуйте|расскажи|расскажите)(?!\w)"
)
_GENERATE = re.compile(
    r"(?<!\w)(?:напиши|написать|переведи|перевести|сочини|сочинить|посчитай|подсчитай|посчитать|придумай"
    r"|придумать|перескажи|пересказать|составь|составить|сформулируй|перефразируй)(?:те)?(?!\w)"
)
LONG_WORDS = 14  # длиннее — рассуждение, а не команда
QUESTION_WORDS = 4  # «?» в конце и слов больше — вопрос мозгу


def _heuristic(text: str) -> str | None:
    """Причина отправить запрос мозгу без модели или None."""
    low = text.lower().replace("ё", "е")
    words = len(low.split())
    if _QUESTION.search(low):
        return "heur:question"
    if _GENERATE.search(low):
        return "heur:generate"
    if low.rstrip().endswith("?") and words > QUESTION_WORDS:
        return "heur:question"
    if words > LONG_WORDS:
        return "heur:long"
    return None


def _brain(text: str, reason: str, deep: bool, local_only: bool) -> Route:
    if local_only:
        return Route("local", "local:needs_gpt", text, deep=deep, local_only=True)
    return Route("brain", reason, text, deep=deep)


def route(text: str, ctx: Context, mode: str) -> Route:
    """Выбрать уровень для запроса. mode — "normal" | "local" (из конфига)."""
    local_only = mode == "local"
    forced: tuple[str, bool] | None = None  # (reason, deep) от префикса gpt:/думай:
    while m := _PREFIX.match(text):
        word = m.group(1).lower()
        text = text[m.end() :]
        if word == "локально":
            local_only = True
        elif word == "думай":
            forced = ("prefix:think", True)
        else:
            forced = ("prefix:gpt", False)
    text = text.strip()
    if forced is not None:
        return _brain(text, forced[0], forced[1], local_only)
    if not text:
        return Route("hands", "empty", text, local_only=local_only)
    hit = grammar.match(text, ctx)
    if hit is not None:
        return Route("grammar", f"grammar:{hit.action}", text, hit=hit, local_only=local_only)
    reason = _heuristic(text)
    if reason is not None:
        return _brain(text, reason, False, local_only)
    return Route("hands", "hands", text, local_only=local_only)
