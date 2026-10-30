"""«Отмени» и «закрой его»: что Jarvis сделал последним и как это вернуть.

Команды, которые что-то меняют обратимо, кладут сюда шаг отмены: громкость (прежний уровень), звук
(выключить или включить обратно), «сверни все окна» (развернуть обратно), свернуть или развернуть окно
(вернуть окно), таймер, напоминание (удалить), открытую программу (закрыть). «Отмени» берёт самый свежий шаг
не старше TTL_S; шагов нет — Ctrl+Z в активном окне, как раньше. «Закрой его» — последняя программа, которую
Jarvis открыл (не раньше TTL_S назад).
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

TTL_S = 300.0  # старше пяти минут — человек уже не помнит, что «отмени» вернёт
LIMIT = 10


@dataclass
class Step:
    title: str  # что вернётся: «громкость 30%», «окна развёрнуты», «напоминание «купить хлеб» удалено»
    revert: Callable[[], Awaitable[None]]
    at: float


@dataclass
class LastApp:
    target: str  # что передать платформе (exe или id программы)
    name: str  # «Блокнот»
    at: float


class UndoJournal:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._steps: list[Step] = []
        self._app: LastApp | None = None

    def push(self, title: str, revert: Callable[[], Awaitable[None]]) -> None:
        self._steps.append(Step(title, revert, self.clock()))
        del self._steps[:-LIMIT]

    def pop(self) -> Step | None:
        """Самый свежий шаг не старше TTL_S (старые выбрасываются)."""
        now = self.clock()
        self._steps = [s for s in self._steps if now - s.at <= TTL_S]
        return self._steps.pop() if self._steps else None

    def remember_app(self, target: str, name: str) -> None:
        self._app = LastApp(target, name, self.clock())

    def last_app(self) -> LastApp | None:
        app = self._app
        if app is None or self.clock() - app.at > TTL_S:
            return None
        return app

    def forget_app(self) -> None:
        self._app = None
