"""Логика окна Jarvis без Qt: история ввода, очередь подтверждений, «цифра+Enter», Esc, автоскрытие, высота.

Модуль не импортирует PySide6 — его проверяют unit-тесты. Время передаётся явно (`now`), чтобы тесты могли
его подменять; по умолчанию — time.monotonic().
"""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from pc.result import Caller

CONFIRM_TIMEOUT_S = 60.0
"""Сколько ждать ответа человека; дальше — «нет» (как в pc.confirm_client)."""

CONFIRM_GUARD_S = 0.7
"""Нажатия и клики в первые 700 мс после появления блока подтверждения игнорируются."""

HISTORY_LIMIT = 100
MAX_HEIGHT_FRACTION = 0.4
"""Окно растёт максимум до ~40 % высоты экрана; дальше ответ прокручивается."""

LEVEL_LABELS: dict[str, str] = {"grammar": "грамматика", "hands": "руки", "brain": "GPT", "local": "локально"}


# --- история ------------------------------------------------------------------------------------


class History:
    """↑/↓ по отправленным строкам: без дублей подряд, с лимитом; черновик возвращается при спуске вниз."""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._limit = max(1, limit)
        self._items: list[str] = []
        self._pos = 0  # == len(_items) — «внизу», на черновике
        self._draft = ""

    @property
    def items(self) -> list[str]:
        return list(self._items)

    def add(self, text: str) -> None:
        """Запомнить отправленную строку и вернуться вниз."""
        text = text.strip()
        if text and (not self._items or self._items[-1] != text):
            self._items.append(text)
            del self._items[: -self._limit]
        self.reset()

    def reset(self) -> None:
        self._pos = len(self._items)
        self._draft = ""

    def up(self, current: str) -> str | None:
        """Строка выше или None (выше некуда). Уходя вверх с самого низа, сохраняет черновик."""
        if self._pos <= 0:
            return None
        if self._pos >= len(self._items):
            self._draft = current
        self._pos -= 1
        return self._items[self._pos]

    def down(self, current: str) -> str | None:
        """Строка ниже, черновик внизу или None (уже внизу)."""
        if self._pos >= len(self._items):
            return None
        self._pos += 1
        return self._items[self._pos] if self._pos < len(self._items) else self._draft


# --- «цифра+Enter» -------------------------------------------------------------------------------

_INDEX_RE = re.compile(r"\s*([0-9]{1,3})\s*\.?\s*")


def parse_index(text: str, count: int) -> int | None:
    """Номер пункта показанного списка (1..count) → индекс с нуля; иначе None — обычная отправка."""
    if count <= 0:
        return None
    m = _INDEX_RE.fullmatch(text)
    if not m:
        return None
    n = int(m.group(1))
    return n - 1 if 1 <= n <= count else None


# --- подтверждения -------------------------------------------------------------------------------


@dataclass(eq=False)
class ConfirmRequest:
    """Один вопрос человеку. Рабочий поток ждёт wait(), окно отвечает resolve() — побеждает первый ответ."""

    summary: str
    details: str = ""
    caller: Caller = "user"
    created: float = field(default_factory=time.monotonic)
    approved: bool | None = field(default=None, init=False)
    _event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def is_brain(self) -> bool:
        """Запрос от мозга — в окне пометка «просит GPT»."""
        return self.caller == "brain"

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def resolve(self, approved: bool) -> bool:
        """Ответить. True — этот вызов решил запрос; повторные ответы ничего не меняют."""
        with self._lock:
            if self._event.is_set():
                return False
            self.approved = bool(approved)
            self._event.set()
            return True

    def wait(self, timeout: float = CONFIRM_TIMEOUT_S) -> bool:
        """Ждать ответа (рабочий поток). Таймаут — «нет»."""
        if not self._event.wait(timeout):
            self.resolve(False)
        return self.approved is True

    def remaining(self, now: float | None = None, timeout: float = CONFIRM_TIMEOUT_S) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, timeout - (now - self.created))

    def expired(self, now: float | None = None, timeout: float = CONFIRM_TIMEOUT_S) -> bool:
        return self.remaining(now, timeout) <= 0.0


ConfirmKey = Literal["enter", "esc", "ctrl+enter"]


class ConfirmQueue:
    """Одна очередь для обработчика процесса и pipe; на экране — по одному, с защитой 700 мс."""

    def __init__(self, guard_s: float = CONFIRM_GUARD_S, timeout_s: float = CONFIRM_TIMEOUT_S) -> None:
        self.guard_s = guard_s
        self.timeout_s = timeout_s
        self._items: deque[ConfirmRequest] = deque()
        self.shown_at: float | None = None

    def __len__(self) -> int:
        return len(self._items)

    @property
    def current(self) -> ConfirmRequest | None:
        return self._items[0] if self._items else None

    def push(self, req: ConfirmRequest, now: float | None = None) -> bool:
        """Поставить в очередь. True — запрос стал текущим (показать)."""
        if req.done or any(r is req for r in self._items):
            return False
        self._items.append(req)
        if len(self._items) == 1:
            self.shown_at = _now(now)
            return True
        return False

    def guarded(self, now: float | None = None) -> bool:
        """Идёт защита 700 мс после появления текущего блока."""
        if self.current is None or self.shown_at is None:
            return False
        return _now(now) - self.shown_at < self.guard_s - 1e-9  # 1e-9 — погрешность float

    def key(self, name: str, now: float | None = None) -> bool | None:
        """Клавиша при показанном подтверждении → решение (True/False) или None (не к месту или защита).

        Enter и Esc — «нет» (Enter НИКОГДА не подтверждает), Ctrl+Enter — «да».
        """
        if name in ("enter", "esc"):
            return self._answer(False, now)
        if name == "ctrl+enter":
            return self._answer(True, now)
        return None

    def click(self, approved: bool, now: float | None = None) -> bool | None:
        """Клик по кнопке «Да»/«Нет» — с той же защитой 700 мс."""
        return self._answer(approved, now)

    def prune(self, now: float | None = None) -> bool:
        """Убрать отвеченные и истёкшие (истёкший — «нет»). True — сменился текущий запрос."""
        now = _now(now)
        head = self.current
        alive: deque[ConfirmRequest] = deque()
        for req in self._items:
            if not req.done and req.expired(now, self.timeout_s):
                req.resolve(False)
            if not req.done:
                alive.append(req)
        self._items = alive
        if self.current is head:
            return False
        self.shown_at = now if self._items else None
        return True

    def deny_all(self) -> int:
        """Ответить «нет» на всё (окно спрятали — человек вопросов больше не видит)."""
        count = 0
        for req in self._items:
            count += req.resolve(False)
        self._items.clear()
        self.shown_at = None
        return count

    def _answer(self, approved: bool, now: float | None) -> bool | None:
        req = self.current
        if req is None or self.guarded(now):
            return None
        req.resolve(approved)
        self._items.popleft()
        self.prune(now)
        self.shown_at = _now(now) if self._items else None
        return req.approved is True


def _now(now: float | None) -> float:
    return time.monotonic() if now is None else now


# --- Esc ------------------------------------------------------------------------------------------

EscAction = Literal["deny", "cancel", "hide"]


def esc_action(*, confirm_shown: bool, busy: bool, cancel_sent: bool) -> EscAction:
    """Esc: при идущем запросе — отмена; без запроса (или второй) — скрыть; при подтверждении — «нет»."""
    if confirm_shown:
        return "deny"
    if busy and not cancel_sent:
        return "cancel"
    return "hide"


# --- автоскрытие -----------------------------------------------------------------------------------


class AutoHide:
    """Скрыть окно через autohide_s после успешного действия без текста ответа.

    Нажатие отменяет. Мышь над окном в момент срабатывания — ждём, пока уйдёт, и отсчитываем заново.
    """

    def __init__(self, delay_s: float) -> None:
        self.delay_s = max(0.0, float(delay_s))
        self.state: Literal["off", "armed", "wait_leave"] = "off"

    def arm(self, ok: bool, autohide: bool) -> bool:
        """После Done. True — запустить таймер на delay_s."""
        self.state = "armed" if ok and autohide and self.delay_s > 0 else "off"
        return self.state == "armed"

    def cancel(self) -> None:
        self.state = "off"

    def fire(self, hovered: bool) -> bool:
        """Таймер сработал. True — скрыть окно сейчас."""
        if self.state != "armed":
            return False
        if hovered:
            self.state = "wait_leave"
            return False
        self.state = "off"
        return True

    def leave(self) -> bool:
        """Мышь ушла с окна. True — запустить таймер заново."""
        if self.state != "wait_leave":
            return False
        self.state = "armed"
        return True


# --- высота и текст --------------------------------------------------------------------------------


def max_height(screen_h: int, fraction: float = MAX_HEIGHT_FRACTION) -> int:
    return int(screen_h * fraction)


def target_height(fixed: int, flexible: int, screen_h: int, fraction: float = MAX_HEIGHT_FRACTION) -> int:
    """Целевая высота панели: fixed (строка ввода, подтверждение, тост) + ответ, не выше ~40 % экрана.

    Строка ввода и обязательные блоки видны всегда; не влезающий ответ прокручивается.
    """
    fixed = max(0, fixed)
    room = max(0, max_height(screen_h, fraction) - fixed)
    return fixed + min(max(0, flexible), room)


# Управляющие символы направления текста (RLO и т.п.) позволяют подделать вид имени файла
# («отчёт‮xcod.exe»): в окне показываем их как «�».
_BIDI = dict.fromkeys([*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C], "�")
_CONTROL = {c: " " for c in range(0x20) if c not in (0x0A, 0x09)} | {0x7F: " "}


def display_text(text: str) -> str:
    """Недоверенная строка для показа: без управляющих символов и подмены направления текста."""
    return str(text).translate(_BIDI).translate(_CONTROL).replace("\t", "    ")
