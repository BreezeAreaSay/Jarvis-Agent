"""Логика окна Jarvis без Qt: история ввода, очередь подтверждений, «цифра+Enter», Esc, автоскрытие, высота,
модель анимированной сферы (OrbModel).

Модуль не импортирует PySide6 — его проверяют unit-тесты. Время передаётся явно (`now`), чтобы тесты могли
его подменять; по умолчанию — time.monotonic().
"""

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
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

    def deny_caller(self, caller: Caller) -> int:
        """«Нет» на все вопросы этого источника (мозг выключен — его вопросы больше не к месту)."""
        count = 0
        for req in self._items:
            if req.caller == caller:
                count += req.resolve(False)
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


# --- сфера Jarvis ------------------------------------------------------------------------------------
#
# Сфера — «лицо» помощника: почти чёрное стеклянное ядро, внутри клубятся светящиеся доли, снаружи —
# тёмно-фиолетовое пламя (тень Монарха). Здесь только числа: состояние, энергия с затуханием, импульсы
# и кадр как функция времени. Рисует ui/orb.py. Координаты кадра — в долях радиуса сферы, центр (0, 0),
# ось y вниз (как у экрана).

OrbState = Literal["idle", "wake", "listen", "work", "speak", "confirm", "success", "error"]
RGB = tuple[int, int, int]

ORB_LEVELS = ("grammar", "hands", "brain", "local")
ORB_EXTENT = 0.7
"""Аура, вспышка и искры — не дальше 1 + ORB_EXTENT радиуса сферы: виджет = сфера × (1 + ORB_EXTENT)."""

ORB_LOBE_LIMIT = 0.97
"""Доля со свечением целиком внутри сферы: |центр| + радиус свечения ≤ 0,97 радиуса."""

ORB_SPARKS = 9
ORB_MAX_DT = 0.1  # пропуск кадров не даёт рывка: за один тик — не больше 0,1 с
ORB_FRAME_MS = 16  # ~60 кадров в секунду, пока сфера анимируется
ORB_WAKE_S = 0.7  # пробуждение при показе окна
ORB_FLASH_S = 1.1  # вспышка успеха или ошибки
ORB_REST = 0.14
"""Энергия покоя. idle и listen выглядят одинаково: скрытие окна не дёргает сферу на рабочем столе."""

_EPS = 0.01  # разница энергии, яркости и оттенка, которую глаз не видит: дальше — покой
_PULSE_MAX = 0.6
_PULSE_TAU = 0.18
_WORK_ENERGY = {"grammar": 0.64, "hands": 0.7, "brain": 0.8, "local": 0.66}
_WORK_RATE = {"grammar": 1.6, "hands": 1.3, "brain": 0.95, "local": 1.1}


def _rgb(value: str) -> RGB:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _lerp_rgb(a: RGB, b: RGB, k: float) -> RGB:
    k = min(1.0, max(0.0, k))
    return (round(a[0] + (b[0] - a[0]) * k), round(a[1] + (b[1] - a[1]) * k), round(a[2] + (b[2] - a[2]) * k))


@dataclass(frozen=True)
class OrbPalette:
    """Цвета сферы (RGB): тело, доли, три слоя ауры, свечение, ободок, искры."""

    core: RGB  # центр тела — почти чёрный
    edge: RGB  # край тела — тёмно-фиолетовый
    lobes: tuple[RGB, RGB, RGB, RGB]  # светящиеся доли внутри
    smoke: RGB  # тёмный дым ауры
    flame: RGB  # средний язык
    ember: RGB  # внутренний, самый яркий язык
    halo: RGB  # холодное фиолетово-синее свечение вокруг
    rim: RGB  # ободок
    spark: RGB  # искры

    def mix(self, other: "OrbPalette", k: float) -> "OrbPalette":
        """Плавный переход к другой палитре: k = 0 — эта, 1 — другая."""
        a, b, c, d = (_lerp_rgb(x, y, k) for x, y in zip(self.lobes, other.lobes, strict=True))
        return OrbPalette(
            _lerp_rgb(self.core, other.core, k),
            _lerp_rgb(self.edge, other.edge, k),
            (a, b, c, d),
            _lerp_rgb(self.smoke, other.smoke, k),
            _lerp_rgb(self.flame, other.flame, k),
            _lerp_rgb(self.ember, other.ember, k),
            _lerp_rgb(self.halo, other.halo, k),
            _lerp_rgb(self.rim, other.rim, k),
            _lerp_rgb(self.spark, other.spark, k),
        )


def _palette(core: str, edge: str, lobes: tuple[str, str, str, str], *rest: str) -> OrbPalette:
    smoke, flame, ember, halo, rim, spark = (_rgb(c) for c in rest)
    a, b, c, d = (_rgb(x) for x in lobes)
    return OrbPalette(_rgb(core), _rgb(edge), (a, b, c, d), smoke, flame, ember, halo, rim, spark)


# Вся гамма — тёмно-фиолетовая; уровень лишь подмешивает оттенок.
ORB_PALETTES: dict[str, OrbPalette] = {
    # тень Монарха: почти чёрное ядро, фиолетовые доли, редкий холодный индиго-синий отсвет
    "": _palette(
        "#05030a",
        "#120a24",
        ("#5b1fa8", "#7c3aed", "#a855f7", "#4f46e5"),
        *("#160726", "#5b21b6", "#8b5cf6", "#4338ca", "#a78bfa", "#e9d5ff"),
    ),
    # грамматика — холоднее: сине-фиолетовый
    "grammar": _palette(
        "#04030c",
        "#0f0c2a",
        ("#4338ca", "#6366f1", "#8b5cf6", "#3b82f6"),
        *("#0a0c2a", "#3730a3", "#6366f1", "#3b5bdb", "#a5b4fc", "#dbeafe"),
    ),
    # руки — с маджентой
    "hands": _palette(
        "#07020a",
        "#170a24",
        ("#701a75", "#a21caf", "#d946ef", "#7c3aed"),
        *("#1e0626", "#86198f", "#c026d3", "#86198f", "#f0abfc", "#fae8ff"),
    ),
    # GPT — глубже тело и ярче ядро
    "brain": _palette(
        "#030108",
        "#0d061f",
        ("#6d28d9", "#8b5cf6", "#c084fc", "#a855f7"),
        *("#12061f", "#6d28d9", "#a78bfa", "#6d28d9", "#c4b5fd", "#f5f3ff"),
    ),
    # без облака — фиолетовый с примесью бирюзы
    "local": _palette(
        "#03050a",
        "#0e0f24",
        ("#5b1fa8", "#7c3aed", "#14b8a6", "#6d28d9"),
        *("#0a1422", "#4c1d95", "#2dd4bf", "#3f4fc4", "#7fc8e0", "#ccfbf1"),
    ),
}
ORB_SUCCESS = _rgb("#f5f3ff")  # лавандово-белая вспышка
ORB_SUCCESS_TINT = _rgb("#ddd6fe")
ORB_SUCCESS_RING = _rgb("#c4b5fd")
ORB_ERROR = _rgb("#e11d48")  # малиново-красная вспышка
ORB_ERROR_DEEP = _rgb("#881337")
ORB_ERROR_RING = _rgb("#fb7185")
ORB_CORE_LIGHT = _rgb("#f3e8ff")  # яркое ядро


@dataclass(frozen=True)
class OrbLobe:
    """Светящаяся доля внутри сферы: круг свечения радиуса r и хвост — где доля была чуть раньше."""

    x: float
    y: float
    r: float
    color: RGB
    alpha: float
    trail: tuple[tuple[float, float, float, float], ...]  # (x, y, r, яркость) от новых к старым


@dataclass(frozen=True)
class OrbWisp:
    """Тонкая дуга энергии внутри сферы: центр, радиус, начало и длина дуги (градусы, против часовой)."""

    x: float
    y: float
    radius: float
    start: float
    span: float
    width: float
    color: RGB
    alpha: float


@dataclass(frozen=True)
class OrbFlame:
    """Слой ауры одним контуром и одним градиентом: кольцо у основания и языки пламени, клонящиеся вверх.

    Язык — 7 точек (x, y) двух кубических кривых Безье: основание B1, C1, C2, кончик T, C3, C4, основание B2.
    Градиент — от точки (0, ORB_FLAME_DROP) чуть ниже центра: у основания горячая сердцевина, выше —
    фиолетовое пламя, кончики уходят в тёмный дым и гаснут (вверху — позже).
    """

    ring: float  # радиус кольца у основания (≥ 1)
    tongues: tuple[tuple[float, ...], ...]  # по 14 чисел на язык
    reach: float  # расстояние от точки градиента, где слой гаснет полностью
    stops: tuple[tuple[float, RGB, float], ...]  # (доля reach, цвет, альфа)


@dataclass(frozen=True)
class OrbSpark:
    """Искра: центр, радиус, яркость; stretch — вытянута вверх (след подъёма)."""

    x: float
    y: float
    r: float
    alpha: float
    stretch: float


@dataclass(frozen=True)
class OrbFrame:
    """Всё, что нужно отрисовке одного кадра (ui/orb.py)."""

    palette: OrbPalette
    glow: float  # итоговая яркость 0..1.2
    halo: float  # альфа внешнего свечения
    rim: float  # альфа ободка
    core: float  # альфа яркого ядра (GPT — ярче)
    lobes: tuple[OrbLobe, ...]
    wisps: tuple[OrbWisp, ...]
    flames: tuple[OrbFlame, ...]
    sparks: tuple[OrbSpark, ...]
    flash: float
    flash_rgb: RGB
    ring: float  # радиус кольца-волны (0 — нет)
    ring_alpha: float
    ring_rgb: RGB


# доли: фигуры Лиссажу — амплитуды, частоты, фазы; радиус свечения; фаза мерцания
_LOBES: tuple[tuple[float, float, float, float, float, float, float, float], ...] = (
    (0.42, 0.32, 0.61, 0.83, 0.0, 1.3, 0.34, 0.0),
    (0.34, 0.42, 0.77, 0.52, 2.1, 0.4, 0.31, 1.7),
    (0.44, 0.30, 0.47, 0.97, 4.0, 2.6, 0.28, 3.1),
    (0.28, 0.38, 1.03, 0.69, 5.2, 4.4, 0.25, 4.6),
)
_TRAIL = ((0.2, 0.86, 0.62), (0.4, 0.72, 0.36))  # хвост: на сколько назад по часам, размер, яркость

# дуги энергии: радиус, смещение центра (ax, ay), частота смещения, скорость вращения, длина, толщина, фаза
_WISPS: tuple[tuple[float, float, float, float, float, float, float, float], ...] = (
    (0.6, 0.12, 0.1, 0.37, 1.25, 120.0, 0.035, 0.0),
    (0.44, 0.1, 0.12, 0.53, -1.6, 95.0, 0.03, 2.4),
)

# Языки ауры: 16 языков вокруг сферы; вверху — выше. Один контур на всю ауру, цвет — градиентом по
# расстоянию: сердцевина → фиолетовое пламя → тёмный дым → прозрачно.
_TONGUES = 16
_TONGUE_H = 0.9  # высота самого высокого языка (вверху сферы) при полной энергии, доли радиуса
_TONGUE_W = 0.16  # полуширина основания
_TONGUE_LEAN = 0.5  # насколько языки клонятся вверх (боковые загибаются кверху, как у пламени)
_FLAME_RING = 0.08  # толщина кольца у основания при полной энергии
_DROPS = 6  # оторвавшиеся язычки: поднимаются над пламенем, сжимаются и уходят в дым (только в работе)
ORB_FLAME_DROP = 0.25
ORB_FLAME_MAX = 0.68  # кончики языков (и контрольные точки кривых) — не дальше 1,68 радиуса


def _ease_out(k: float) -> float:
    k = min(1.0, max(0.0, k))
    return 1.0 - (1.0 - k) ** 3


def _wake_bump(k: float) -> float:
    """Огибающая пробуждения: быстрый подъём (пик на ~30 %), плавный спад к нулю."""
    if k <= 0.0 or k >= 1.0:
        return 0.0
    return math.sin(math.pi * k**0.6) ** 1.2


def flash_envelope(s: float) -> float:
    """Огибающая вспышки через s секунд после начала: вспыхивает за ~0,1 с, гаснет к ORB_FLASH_S."""
    if s < 0.0 or s >= ORB_FLASH_S:
        return 0.0
    return min(1.0, (1.0 - math.exp(-s / 0.045)) * math.exp(-s / 0.3) / 0.64)


def _inside(x: float, y: float, r: float) -> tuple[float, float]:
    """Прижать центр свечения радиуса r внутрь стекла: |центр| + r ≤ ORB_LOBE_LIMIT."""
    d, room = math.hypot(x, y), max(0.0, ORB_LOBE_LIMIT - r)
    if d <= room:
        return x, y
    k = room / d
    return x * k, y * k


_TongueShape = tuple[float, float, float, float, float, float]
"""Язык без слоя: направление основания (ox, oy), высота, полуширина, качание кончика и середины."""


def _tongue(shape: _TongueShape) -> tuple[float, ...] | None:
    """Контур языка: хребет сначала идёт наружу, потом загибается вверх; поперёк бежит волна (кончик
    и середина качаются в разные стороны); к кончику язык сужается. Слишком низкий — None."""
    ox, oy, h, w, sway_tip, sway_mid = shape
    if h < 0.03:
        return None
    mx, my = 0.96 * ox, 0.96 * oy  # основание чуть внутри сферы: стык закрывает тело
    dx, dy = ox, oy - _TONGUE_LEAN  # кончик — наружу и вверх (ось y вниз)
    n = math.hypot(dx, dy)
    dx, dy = dx / n, dy / n
    ax, ay = ox + 0.35 * (dx - ox), oy + 0.35 * (dy - oy)  # низ хребта: в основном наружу
    n = math.hypot(ax, ay)
    ax, ay = ax / n, ay / n
    qx, qy = -ay, ax  # поперёк низа хребта
    px, py = -dy, dx  # поперёк направления кончика
    s1x, s1y = mx + ax * 0.42 * h - qx * sway_mid, my + ay * 0.42 * h - qy * sway_mid
    s2x, s2y = mx + dx * 0.7 * h + px * 0.6 * sway_tip, my + dy * 0.7 * h + py * 0.6 * sway_tip
    tx, ty = mx + dx * h + px * sway_tip, my + dy * h + py * sway_tip
    vx, vy = -oy, ox  # касательная к сфере у основания
    pts = [
        *(mx - vx * w, my - vy * w),
        *(s1x - qx * w * 1.25, s1y - qy * w * 1.25),  # «живот» пламени у основания
        *(s2x - px * w * 0.2, s2y - py * w * 0.2),  # верх — узкий, кончик острый
        *(tx, ty),
        *(s2x + px * w * 0.2, s2y + py * w * 0.2),
        *(s1x + qx * w * 1.25, s1y + qy * w * 1.25),
        *(mx + vx * w, my + vy * w),
    ]
    # кривая Безье лежит внутри выпуклой оболочки своих точек: точки в круге — и язык в нём
    limit = 1.0 + ORB_FLAME_MAX
    if max(math.hypot(pts[i], pts[i + 1]) for i in range(0, 14, 2)) > limit:
        span = max(math.hypot(pts[i] - mx, pts[i + 1] - my) for i in range(0, 14, 2))
        k = (limit - 0.96) / span
        pts = [(v - (mx if i % 2 == 0 else my)) * k + (mx if i % 2 == 0 else my) for i, v in enumerate(pts)]
    return tuple(pts)


def _drop(j: int, t: float, scale: float, lively: float) -> tuple[float, ...] | None:
    """Оторвавшийся язычок j: каплевидный (вверх острый, низ круглый), поднимается от кончиков пламени,
    сжимается и гаснет; формат точек — как у языка (основание B1 = B2 — низ капли)."""
    u = (j * 0.618034 + 0.37) % 1.0
    period = 0.75 + 0.45 * ((j * 0.414214) % 1.0)  # секунды часов анимации
    p = (t / period + u) % 1.0
    if p > 0.9:
        return None
    phi = -math.pi / 2 + (u - 0.5) * 1.7  # верхняя половина сферы
    rise = 1.08 + 0.3 * scale + 0.3 * p
    x = math.cos(phi) * rise + 0.04 * math.sin(2 * math.pi * p + 5 * u)
    y = math.sin(phi) * rise - 0.12 * p
    k = lively * (1.0 - p) ** 0.8
    w, h = 0.07 * k, 0.21 * k
    if h < 0.02:
        return None
    lean = 0.3 * w * math.sin(2 * math.pi * p + 3 * u)
    bx, by = x, y + 0.45 * h
    pts = (
        *(bx, by),
        *(x - 1.3 * w, y + 0.35 * h),
        *(x - 0.6 * w + lean, y - 0.3 * h),
        *(x + lean, y - 0.55 * h),
        *(x + 0.6 * w + lean, y - 0.3 * h),
        *(x + 1.3 * w, y + 0.35 * h),
        *(bx, by),
    )
    limit = 1.0 + ORB_FLAME_MAX
    if max(math.hypot(pts[i], pts[i + 1]) for i in range(0, 14, 2)) > limit:
        return None
    return pts


class OrbModel:
    """Модель сферы без Qt: состояние, энергия с затуханием, импульсы и кадр как функция времени.

    Входы — события окна: wake() (показ, хоткей), key() (ввод), begin() (запрос), set_level(), status(),
    chunk() (поток ответа), confirm(on), done(ok, cancelled), hide(), set_local(on); voice(rms) — уровень
    голоса (для голосового ввода). Одну модель рисуют сфера в окне и сфера на рабочем столе.
    tick(dt) двигает время и отвечает, нужна ли дальше анимация: False — сфера успокоилась, таймер
    останавливается. advance(now) — то же по абсолютному времени: несколько виджетов могут рисовать одну
    модель, и время не идёт вдвое быстрее. frame(t) детерминирован (суммы синусов, без random); доли —
    внутри сферы, аура и искры — в пределах 1 + ORB_EXTENT. animations=False — статичные кадры состояний.
    """

    def __init__(self, animations: bool = True) -> None:
        self.animated = bool(animations)
        self.state: OrbState = "idle"
        self.shown = False  # окно Jarvis видно (покой — listen, иначе idle)
        self.level = ""  # уровень текущего запроса — оттенок
        self.local = False
        self.busy = False
        self.t = 0.0  # часы анимации: их скорость зависит от состояния
        self.state_t = 0.0  # сколько длится текущее состояние, с
        self.energy = ORB_REST
        self.pulse = 0.0
        self.sparkle = 0.0
        self.rate = 0.0
        self.tint: dict[str, float] = dict.fromkeys(ORB_LEVELS, 0.0)
        self.flash_kind = "success"
        self.flash_t = -1.0  # с начала вспышки; < 0 — вспышки нет
        self.wake_t = -1.0  # с начала пробуждения; < 0 — нет
        self._spoke = False
        self._soft = False  # отмена: мягкое затухание
        self._animating = False
        self._clock: float | None = None
        self._listeners: list[Callable[[], None]] = []

    # --- подписка (виджеты, которые рисуют модель)

    def subscribe(self, fn: Callable[[], None]) -> None:
        """fn() вызывается после каждого входа: виджет перерисуется и при надобности запустит таймер."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    @property
    def animating(self) -> bool:
        """Нужен ли таймер кадров: False — кадр застыл, перерисовывать нечего."""
        return self._animating

    @property
    def resting(self) -> bool:
        """Окно спрятано, запроса нет, вспышка догорела (режим auto прячет сферу рабочего стола)."""
        return not self.shown and not self.busy and self.state == "idle"

    # --- входы

    def wake(self) -> None:
        """Окно показано: короткое пробуждение (~0,7 с), затем покой listen. Идёт запрос — только импульс."""
        self.shown = True
        if self.busy or self.state in ("work", "speak", "confirm"):
            self._input(pulse=0.25)
            return
        self.flash_t = -1.0  # вспышка прошлого запроса (окно спрятали посреди неё) не продолжается
        self._set("wake")
        self.wake_t = 0.0
        self._input()

    def key(self) -> None:
        """Нажатие клавиши в строке ввода: короткий импульс, который сам затухает."""
        self._input(pulse=0.3)

    def voice(self, rms: float) -> None:
        """Уровень голоса 0..1 (голосовой ввод, ~20–50 раз в секунду): импульс по громкости, сам затухает."""
        level = min(1.0, max(0.0, float(rms)))
        if self.animated:
            self.pulse = max(self.pulse, 0.5 * level)
        self._input()

    def begin(self) -> None:
        """Начался запрос: работа, пока не придёт Done."""
        self.busy = True
        self._spoke = False
        self.level = ""
        self.flash_t = -1.0
        self._set("work")
        self._input(pulse=0.35)

    def set_level(self, level: str) -> None:
        """Уровень запроса (grammar | hands | brain | local) — оттенок и скорость работы."""
        self.level = level if level in ORB_LEVELS else ""
        self._input(pulse=0.12)

    def status(self) -> None:
        """Строка «⚙ …» или результаты find — небольшой импульс."""
        self._input(pulse=0.14)

    def chunk(self) -> None:
        """Кусок ответа: пульсация в такт потоку."""
        self._spoke = True
        if self.state == "work":
            self._set("speak")
        self._input(pulse=0.2)

    def confirm(self, on: bool) -> None:
        """Карточка подтверждения видна — медленная пульсация; убрана — назад к работе или покою."""
        if on and self.state != "confirm":
            self._set("confirm")
        elif not on and self.state == "confirm":
            self._set(self._resume())
        self._input()

    def done(self, ok: bool, cancelled: bool = False) -> None:
        """Конец запроса: успех — лавандово-белая вспышка, ошибка — малиновая, отмена — мягкое затухание."""
        was_busy, self.busy = self.busy, False
        if not was_busy and self.state in ("idle", "listen", "wake"):  # запоздалый Done — сфера уже в покое
            self._input()
            return
        if cancelled or not self.animated:
            self._set(self._rest())
            self._soft = cancelled
        else:
            self._set("success" if ok else "error")
            self.flash_kind = "success" if ok else "error"
            self.flash_t = 0.0
        self._input()

    def hide(self) -> None:
        """Окно спрятано. Идёт запрос — работа продолжается (её видно на сфере рабочего стола), вспышка
        догорает; иначе — покой idle (энергия затухает сама, без скачка)."""
        self.shown = False
        self.wake_t = -1.0
        if not self.busy and self.state not in ("success", "error"):
            self._set("idle")
            self.level = ""
        self._input()

    def set_local(self, on: bool) -> None:
        """Локальный режим — оттенок бирюзы в покое."""
        self.local = bool(on)
        self._input()

    # --- время

    def tick(self, dt: float) -> bool:
        """Сдвинуть время на dt секунд. True — анимация продолжается, False — сфера успокоилась."""
        if not self.animated:
            self._animating = False
            return False
        dt = min(max(0.0, float(dt)), ORB_MAX_DT)
        before = self.state
        self.state_t += dt
        if self.wake_t >= 0.0:
            self.wake_t = self.wake_t + dt if self.wake_t + dt < ORB_WAKE_S else -1.0
        if self.flash_t >= 0.0:
            self.flash_t = self.flash_t + dt if self.flash_t + dt < ORB_FLASH_S else -1.0
        if self.state == "wake" and self.wake_t < 0.0:
            self._set(self._rest())
        if self.state in ("success", "error") and self.flash_t < 0.0:
            self._set(self._rest())
        energy, sparkle, rate = self._targets()
        tau = 0.55 if self._soft else (0.22 if energy > self.energy else 0.35)
        self.energy += (energy - self.energy) * (1.0 - math.exp(-dt / tau))
        self.pulse *= math.exp(-dt / _PULSE_TAU)
        self.sparkle += (sparkle - self.sparkle) * (1.0 - math.exp(-dt / 0.3))
        self.rate += (rate - self.rate) * (1.0 - math.exp(-dt / 0.25))
        k = 1.0 - math.exp(-dt / 0.3)
        target = self._tint_target()
        for name, w in self.tint.items():
            self.tint[name] = w + ((1.0 if name == target else 0.0) - w) * k
        self.t += dt * (self.rate + 1.4 * self.pulse + self._wake_spin())
        alive = self._alive()
        if not alive:
            self._settle()
        self._animating = alive
        if self.state != before:  # пробуждение или вспышка кончились — подписчикам (режим auto) важно
            self._notify()
        return alive

    def advance(self, now: float) -> bool:
        """Сдвинуть время до момента now (time.monotonic()); повторный вызов с тем же now не двигает его."""
        last, self._clock = self._clock, now
        if last is None or now <= last:
            return self._animating
        return self.tick(now - last)

    # --- кадр

    def glow(self) -> float:
        """Итоговая яркость: энергия + импульсы + пробуждение + дыхание подтверждения."""
        g = self.energy + self.pulse
        if self.wake_t >= 0.0:
            g += 0.75 * _wake_bump(self.wake_t / ORB_WAKE_S)
        if self.state == "confirm" and self.animated:
            g += 0.16 * math.sin(2 * math.pi * self.state_t / 2.4)
        return min(1.2, max(0.0, g))

    def palette(self) -> OrbPalette:
        """Палитра кадра: смесь уровней по весам оттенка, поверх — цвет вспышки."""
        pal = ORB_PALETTES[""]
        total = 1.0 - min(1.0, sum(self.tint.values()))
        for name, w in self.tint.items():
            if w > 0.001:
                total += w
                pal = pal.mix(ORB_PALETTES[name], w / total)
        flash = flash_envelope(self.flash_t)
        if flash <= 0.0:
            return pal
        if self.flash_kind == "error":
            red, deep, pink = ORB_ERROR, ORB_ERROR_DEEP, ORB_ERROR_RING
            return pal.mix(
                OrbPalette(pal.core, deep, (deep, red, pink, red), deep, deep, red, red, pink, pink),
                0.8 * flash,
            )
        light, ring, white = ORB_SUCCESS_TINT, ORB_SUCCESS_RING, ORB_SUCCESS
        lit = OrbPalette(
            pal.core, pal.edge, (light, light, white, light), pal.smoke, ring, light, ring, white, white
        )
        return pal.mix(lit, 0.6 * flash)

    def frame(self, t: float | None = None) -> OrbFrame:
        """Кадр в момент t часов анимации (по умолчанию — текущий)."""
        t = self.t if t is None else float(t)
        g = self.glow()
        e = min(1.0, g)
        pal = self.palette()
        flash = flash_envelope(self.flash_t)
        ring, ring_alpha, ring_rgb = self._ring(pal)
        return OrbFrame(
            palette=pal,
            glow=g,
            halo=min(1.0, 0.14 + 0.36 * e + 0.3 * flash),
            rim=min(1.0, 0.55 + 0.4 * e + 0.3 * flash),
            core=min(1.0, (0.25 + 0.75 * self.tint["brain"]) * (0.15 + 0.6 * e) + 0.6 * flash),
            lobes=tuple(self._lobe(i, t, e, pal) for i in range(len(_LOBES))),
            wisps=tuple(self._wisp(i, t, e, pal) for i in range(len(_WISPS))),
            flames=self._flames(t, g, pal),
            sparks=self._sparks(t),
            flash=flash,
            flash_rgb=ORB_ERROR if self.flash_kind == "error" else ORB_SUCCESS,
            ring=ring,
            ring_alpha=ring_alpha,
            ring_rgb=ring_rgb,
        )

    # --- внутреннее

    def _set(self, state: OrbState) -> None:
        if state != self.state:
            self.state = state
            self.state_t = 0.0
        if state not in ("listen", "idle"):
            self._soft = False

    def _rest(self) -> OrbState:
        return "listen" if self.shown else "idle"

    def _resume(self) -> OrbState:
        if not self.busy:
            return self._rest()
        return "speak" if self._spoke else "work"

    def _targets(self) -> tuple[float, float, float]:
        """(энергия, искры, скорость часов) для текущего состояния."""
        if self.state in ("work", "speak"):
            speak = self.state == "speak"
            energy = _WORK_ENERGY.get(self.level, 0.68) + (0.08 if speak else 0.0)
            return energy, 1.0, _WORK_RATE.get(self.level, 1.15) * (1.1 if speak else 1.0)
        if self.state == "confirm":
            return 0.42, 0.0, 0.5
        if self.state in ("success", "error"):
            return ORB_REST, 0.0, 0.9
        if self.state == "wake":
            return ORB_REST, 0.0, 1.0
        return ORB_REST, 0.0, 0.0  # покой: часы плавно останавливаются, кадр застывает

    def _tint_target(self) -> str:
        if self.level and (self.busy or self.state in ("success", "error")):
            return self.level
        return "local" if self.local else ""

    def _alive(self) -> bool:
        if self.state in ("wake", "work", "speak", "confirm", "success", "error"):
            return True
        if self.flash_t >= 0.0 or self.wake_t >= 0.0:
            return True
        energy, sparkle, rate = self._targets()
        if abs(self.energy - energy) > _EPS or self.pulse > _EPS or abs(self.sparkle - sparkle) > _EPS:
            return True
        if abs(self.rate - rate) > 0.01:
            return True
        target = self._tint_target()
        return any(abs(w - (1.0 if name == target else 0.0)) > _EPS for name, w in self.tint.items())

    def _settle(self) -> None:
        """Застыть: всё — в целевые значения состояния (кадр покоя детерминирован)."""
        energy, sparkle, rate = self._targets()
        self.energy, self.pulse, self.sparkle, self.rate = energy, 0.0, sparkle, rate
        target = self._tint_target()
        for name in self.tint:
            self.tint[name] = 1.0 if name == target else 0.0
        if not self.animated:
            self.flash_t = self.wake_t = -1.0
            if self.state in ("wake", "success", "error"):
                self.state = self._rest()
        self._soft = False

    def _input(self, pulse: float = 0.0) -> None:
        if self.animated:
            self.pulse = min(_PULSE_MAX, self.pulse + pulse)
        else:
            self._settle()
        was = self._animating
        self._animating = self.animated and self._alive()
        if self._animating and not was:
            self._clock = None  # первый кадр после покоя — без скачка времени
        self._notify()

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except RuntimeError:  # виджет уже удалён (Qt) — больше не звать
                self.unsubscribe(fn)

    def _wake_spin(self) -> float:
        if self.wake_t < 0.0:
            return 0.0
        return 1.6 * (1.0 - self.wake_t / ORB_WAKE_S) ** 2

    def _unfold(self) -> float:
        """Аура раскрывается из сферы в начале пробуждения."""
        if self.wake_t < 0.0:
            return 1.0
        return 0.35 + 0.65 * _ease_out(self.wake_t / (0.3 * ORB_WAKE_S))

    def _lobe(self, i: int, t: float, e: float, pal: OrbPalette) -> OrbLobe:
        ax, ay, fx, fy, px, py, r0, twinkle = _LOBES[i]
        r = r0 * (0.9 + 0.12 * math.sin(1.1 * t + 1.9 * i)) * (0.8 + 0.45 * e)

        def at(s: float, size: float) -> tuple[float, float, float]:
            x, y = _inside(ax * math.sin(fx * s + px), ay * math.sin(fy * s + py) + 0.04, r * size)
            return x, y, r * size

        x, y, _ = at(t, 1.0)
        trail = tuple((*at(t - back * (0.5 + 0.5 * self.rate), size), fade) for back, size, fade in _TRAIL)
        alpha = (0.45 + 0.55 * e) * (0.8 + 0.2 * math.sin(1.6 * t + twinkle))
        return OrbLobe(x, y, r, pal.lobes[i], min(1.0, alpha), trail)

    def _wisp(self, i: int, t: float, e: float, pal: OrbPalette) -> OrbWisp:
        radius, ax, ay, freq, spin, span, width, phase = _WISPS[i]
        x, y = ax * math.sin(freq * t + phase), ay * math.cos(1.3 * freq * t + phase)
        start = math.degrees(spin * t + phase) % 360.0
        alpha = (0.12 + 0.5 * e) * (0.7 + 0.3 * math.sin(0.9 * t + 2.0 * phase))
        return OrbWisp(x, y, radius, start, span, width, pal.lobes[2], min(1.0, alpha))

    def _flames(self, t: float, g: float, pal: OrbPalette) -> tuple[OrbFlame, ...]:
        """Аура: языки и градиент сердцевина → пламя → дым. В покое — ниже и тусклее."""
        scale = min(1.0, 0.42 + 0.62 * g) * self._unfold()
        tongues = tuple(
            x for k in range(_TONGUES) if (x := _tongue(self._tongue_shape(k, t, scale))) is not None
        )
        lively = min(1.0, max(0.0, (g - 0.3) / 0.4)) * self._unfold()  # покой — без оторвавшихся язычков
        if lively > 0.02:
            tongues += tuple(x for j in range(_DROPS) if (x := _drop(j, t, scale, lively)) is not None)
        reach = 1.0 + ORB_FLAME_DROP + ORB_FLAME_MAX * (0.7 + 0.3 * scale)
        a = 0.95 * (0.5 + 0.5 * min(1.0, g))
        d0 = 1.0 / reach
        span = 1.0 - d0
        hot = _lerp_rgb(pal.ember, pal.rim, 0.45)
        stops = (
            (0.0, hot, a),
            (d0, hot, a),
            (d0 + span * 0.2, pal.ember, a),
            (d0 + span * 0.45, pal.flame, a),
            (d0 + span * 0.72, _lerp_rgb(pal.flame, pal.smoke, 0.7), 0.8 * a),
            (1.0, pal.smoke, 0.0),
        )
        return (OrbFlame(1.0 + _FLAME_RING * scale, tongues, reach, stops),)

    def _tongue_shape(self, k: int, t: float, scale: float) -> _TongueShape:
        """Язык k в момент t: вверху сферы — выше; высота мерцает, кончик качается (суммы синусов)."""
        u = (k * 0.618034 + 0.13) % 1.0
        v = (k * 0.381966 + 0.71) % 1.0
        phi = 2 * math.pi * (k + 0.35 * (u - 0.5)) / _TONGUES + 0.06 * math.sin(0.35 * t + 6.3 * u)
        ox, oy = math.cos(phi), math.sin(phi)
        up = (1.0 - oy) / 2  # 1 — верх сферы, 0 — низ
        wave1 = math.sin((2.1 + 0.9 * u) * t + 6.3 * u)
        wave2 = math.sin((3.4 + 1.3 * v) * t + 4.1 * v + k)
        flick = 0.75 + 0.25 * (0.6 * wave1 + 0.4 * wave2)
        h = _TONGUE_H * scale * (0.2 + 0.8 * up**1.4) * (0.5 + 0.6 * v * v) * flick
        w = _TONGUE_W * (0.75 + 0.5 * u) * (0.65 + 0.35 * scale)
        phase = (1.7 + 0.6 * u) * t + 9.0 * v
        return ox, oy, h, w, 0.28 * h * math.sin(phase), 0.12 * h * math.sin(phase + 1.7)

    def _sparks(self, t: float) -> tuple[OrbSpark, ...]:
        """Искры поднимаются над сферой во время работы; детерминированно от t."""
        if self.sparkle < 0.01:
            return ()
        sparks = []
        for i in range(ORB_SPARKS):
            u = (i * 0.618034) % 1.0
            p = (t / (1.1 + 0.6 * u) + i * 0.37) % 1.0
            angle = -math.pi / 2 + (u - 0.5) * 2.4
            out = 1.02 + 0.16 * p
            x = math.cos(angle) * out + 0.05 * math.sin(2 * math.pi * 1.4 * p + i)
            y = math.sin(angle) * out - 0.3 * p
            alpha = self.sparkle * math.sin(math.pi * p) ** 1.3 * (0.6 + 0.4 * ((i * 0.37) % 1.0))
            sparks.append(OrbSpark(x, y, 0.04 * (1.0 - 0.45 * p), min(1.0, alpha), 1.0 + 1.4 * (1.0 - p)))
        return tuple(sparks)

    def _ring(self, pal: OrbPalette) -> tuple[float, float, RGB]:
        """Кольцо-волна: при пробуждении и при вспышке."""
        if self.flash_t >= 0.0:
            k, alpha0 = self.flash_t / 0.85, 0.75
            rgb = ORB_ERROR_RING if self.flash_kind == "error" else ORB_SUCCESS_RING
        elif self.wake_t >= 0.0:
            k, alpha0, rgb = self.wake_t / ORB_WAKE_S, 0.5, pal.rim
        else:
            return 0.0, 0.0, pal.rim
        if k >= 1.0:
            return 0.0, 0.0, rgb
        return 1.0 + 0.46 * _ease_out(k), alpha0 * (1.0 - k) ** 1.6, rgb
