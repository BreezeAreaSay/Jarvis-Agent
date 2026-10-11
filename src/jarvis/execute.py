"""Исполнение решений грамматики и рук: вызов src/pc с caller="user", ответ — Result.text.

reply / clarify / ask_gpt не исполняются, а возвращаются наверх. Решение РУК исполняется, только если цель
взята из текста команды или равна "@cur" (AGENTS.md, «Пути, цели и недоверенные данные»): заголовок окна —
недоверенный текст и может содержать «команды». open и focus взаимозаменяемы для приложений и папок.

Отмена: каждое удачное действие пользователя запоминается в ctx.undo (одно, последнее; свежесть — TTL
контекста). Обратимые — громкость (прежний уровень и mute из Result.data["prev"]), свернуть/развернуть окно
(restore того же hwnd), свернуть все (UndoMinimizeALL), напоминание и заметка памяти; остальное — «Это не
отменить».
Напоминания, память и помощь — только у грамматики, без модели; это не действия ПК (в policy их нет).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Any, Literal

from rapidfuzz import fuzz

from jarvis import memory, reminders
from jarvis.context import CUR, Context
from jarvis.memory import StoreError
from jarvis.reminders import plural
from pc import apps, audio, files, media, procs, system, windows
from pc.result import Result
from pc.windows import WindowInfo

log = logging.getLogger("jarvis")

OutcomeKind = Literal["done", "reply", "clarify", "ask_gpt"]
Source = Literal["grammar", "hands"]

FUZZ_MIN = 80.0
HANDS_ACTIONS = {"open", "close", "focus", "win", "find", "vol", "media", "kill"}
# только у грамматики, без модели: отмена, напоминания, память, помощь
LOCAL_ACTIONS = {"undo", "remind", "reminders", "cancel_reminder", "remember", "forget", "memory", "help"}
GRAMMAR_ACTIONS = HANDS_ACTIONS | {"open_found", "lock", "clock"} | LOCAL_ACTIONS
# действия, которые что-то меняют на ПК: удачное — «последнее действие» для «отмени»
STATEFUL = {"open", "close", "focus", "win", "vol", "media", "kill", "open_found", "lock"}
# поле с целью: оно должно быть взято из текста команды (или "@cur")
TARGET_KEY = {
    "open": "target",
    "close": "target",
    "focus": "target",
    "win": "target",
    "find": "query",
    "kill": "name",
}
REQUIRED = {
    **TARGET_KEY, "win": "action", "media": "action", "open_found": "index",
    "remember": "text", "forget": "which",
}  # fmt: skip

# слова темы: без них media, vol и win без цели не исполняются; для "@cur" — глагол действия
TOPIC: dict[str, tuple[str, ...]] = {
    "media": (
        "трек", "песн", "музык", "пауз", "плеер", "воспроизвед", "дальше", "следующ", "предыдущ", "видео",
        "прошл", "продолж", "останов", "мелоди", "композиц", "клип", "play",
    ),
    "vol": (
        "громк", "звук", "тише", "громче", "тихо", "громко", "заглуш", "беззвуч", "mute", "мьют", "volume",
    ),
    "win": ("окн", "сверн", "разверн", "восстанов", "весь экран", "полный экран", "минимиз", "максимиз"),
    "open": ("откр", "запус", "покаж", "зайд", "перейд"),
    "close": ("закр", "выруб"),
    "focus": ("переключ", "покаж", "перейд", "верни", "вернись", "фокус", "активир", "откр", "разверн"),
    "kill": ("убей", "убить", "убива", "прибей", "заверш", "процесс", "килл", "kill"),
}  # fmt: skip
REF_WORDS = {
    "его", "ее", "это", "этот", "эту", "эта", "этого", "этой", "оно", "него", "нее", "ней", "туда", "тут",
    "здесь", "их", "них",
}  # fmt: skip
REF_STEMS = ("окн", "текущ", "активн")
NOT_UNDERSTOOD = {
    "open": "Не понял, что именно открыть.",
    "close": "Не понял, что именно закрыть.",
    "focus": "Не понял, что именно показать.",
    "win": "Не понял, какое окно.",
    "find": "Не понял, что именно найти.",
    "kill": "Не понял, что именно завершить.",
}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
# действия без ответа, который надо читать: окно можно спрятать (списки и помощь — нет)
AUTOHIDE = {"open", "close", "focus", "win", "vol", "media", "lock", "open_found", "kill"}
AUTOHIDE |= {"undo", "remind", "cancel_reminder", "remember", "forget"}
NOTHING_TO_UNDO = "Нечего отменять"
CANT_UNDO = "Это не отменить"
ASK_WHEN = (
    "Во сколько напомнить? Например: «напомни завтра в 9 позвонить маме» или «напомни через 10 минут …»"
)
HELP_TEXT = """Частые команды выполняю сразу, без модели; остальное разбирает локальная модель, \
вопросы и тексты — GPT.
• Окна: «сверни телегу», «разверни на весь экран», «закрой его», «сверни все окна».
• Приложения и папки: «открой хром», «переключись на ворд», «открой загрузки».
• Звук и музыка: «громкость 30», «тише», «выключи звук», «пауза», «следующий трек».
• Файлы: «найди файл смета», потом «открой второй».
• Напоминания: «напомни через 10 минут вынуть пиццу», «таймер на 5 минут», «какие напоминания», \
«отмени напоминание 1».
• Память: «запомни, что я пью кофе без сахара», «что ты обо мне помнишь», «забудь про кофе». \
Память видит GPT — пароли туда не пиши.
• Отмена: «отмени» — вернёт громкость, окна, напоминание или заметку как было.
• Префиксы: «gpt: …» — спросить GPT, «думай: …» — подумать подольше, «локально: …» — без облака.
• Окно: Enter — выполнить, Esc — отменить или скрыть, ↑/↓ — прошлые команды, цифра и Enter — пункт списка."""

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]+:", re.IGNORECASE)  # от двух букв: «C:» — это диск, не схема
_DRIVE = re.compile(r"^[a-z]:", re.IGNORECASE)
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Outcome:
    """Итог команды: done — действие выполнено (или нет: ok=False); reply/clarify/ask_gpt — наверх."""

    kind: OutcomeKind
    ok: bool
    text: str
    items: list[str] = field(default_factory=list)
    autohide: bool = False
    action: str = ""


def _now() -> datetime:
    return datetime.now()


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _is_url(value: str) -> bool:
    return bool(_SCHEME.match(value)) or value.casefold().startswith("www.")


def _is_path(value: str) -> bool:
    return "\\" in value or "/" in value or bool(_DRIVE.match(value)) or value.startswith(("~", "%"))


def from_text(value: str, text: str, kind: str | None = None) -> bool:
    """Цель взята из текста команды: URL и пути — буквально; остальное — partial_ratio ≥ 80."""
    v, t = _norm(value).strip(), _norm(text)
    if not v:
        return False
    if kind == "url" or _is_url(v) or _is_path(v):
        return v in t
    if len(v) <= 2:
        return v in _WORD.findall(t)
    score = fuzz.partial_ratio(v, t) if len(v) <= len(t) else fuzz.ratio(v, t)
    return score >= FUZZ_MIN or _same_app(value, text)


def _same_app(value: str, text: str) -> bool:
    """Модель нормализовала имя («телегу» → «Telegram»): цель и 1–3 слова команды — одно приложение.

    lookup, а не resolve: перебор слов не должен запускать обновление инвентаря при промахе.
    """
    app, _ = apps.lookup(value)
    if app is None:
        return False
    words = _WORD.findall(_norm(text))
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            found, _ = apps.lookup(" ".join(words[i : i + size]))
            if found is not None and found.app_id == app.app_id:
                return True
    return False


def _has_topic(tool: str, norm_text: str) -> bool:
    return any(stem in norm_text for stem in TOPIC.get(tool, ()))


def _mentions_cur(tool: str, norm_text: str) -> bool:
    """«его/это/туда/окно» или глагол самого действия: "@cur" не берётся из ниоткуда."""
    words = _WORD.findall(norm_text)
    if any(w in REF_WORDS or w.startswith(REF_STEMS) for w in words):
        return True
    return _has_topic(tool, norm_text)


def _hands_refusal(tool: str, args: dict[str, Any], text: str) -> str | None:
    """Почему решение рук нельзя исполнять (None — можно)."""
    t = _norm(text)
    if (tool in ("media", "vol") or (tool == "win" and not args.get("target"))) and not _has_topic(tool, t):
        return "Не понял, что сделать."
    key = TARGET_KEY.get(tool)
    value = args.get(key) if key else None
    if value is None or (tool == "win" and args.get("action") == "minimize_all"):
        return None
    if not isinstance(value, str):
        return NOT_UNDERSTOOD[tool]
    if value == CUR:
        return None if _mentions_cur(tool, t) else NOT_UNDERSTOOD[tool]
    return None if from_text(value, text, args.get("kind")) else NOT_UNDERSTOOD[tool]


def _resolve_cur(tool: str, args: dict[str, Any], ctx: Context) -> bool:
    """Подставить "@cur" (и цель win по умолчанию); False — цели нет."""
    key = TARGET_KEY.get(tool)
    if tool == "win":
        if args.get("action") == "minimize_all":
            args["target"] = None
            return True
        if not args.get("target"):
            args["target"] = CUR
    if not key or args.get(key) != CUR:
        return True
    cur = ctx.cur_target()
    if cur is None:
        return False
    if isinstance(cur, str) and _is_path(cur) and tool != "open":
        cur = PureWindowsPath(cur).name or cur  # окно файла ищется по имени
    args[key] = cur
    return True


def _dry_text(tool: str, args: dict[str, Any]) -> str:
    parts = [f"{k}={v}" for k, v in args.items() if v is not None]
    return " ".join(["(dry)", tool, *parts])


def run(
    tool: str,
    args: dict[str, Any] | None,
    text: str,
    ctx: Context | None,
    source: Source,
    dry: bool = False,
) -> Outcome:
    """Исполнить решение грамматики или рук. Все вызовы pc — с caller="user"."""
    ctx = ctx if ctx is not None else Context()
    args = dict(args or {})
    if tool == "reply":
        return Outcome("reply", True, str(args.get("text") or "").strip() or "Готов помочь.")
    if tool == "clarify":
        return Outcome("clarify", True, str(args.get("question") or "").strip() or "Что именно сделать?")
    if tool == "ask_gpt":
        return Outcome("ask_gpt", True, "")
    if tool not in (HANDS_ACTIONS if source == "hands" else GRAMMAR_ACTIONS):
        return Outcome("clarify", False, "Не понял, что сделать.")
    key = REQUIRED.get(tool)
    if key and args.get(key) in (None, ""):
        return Outcome("clarify", False, "Не понял, что сделать.")
    if source == "hands":
        refusal = _hands_refusal(tool, args, text)
        if refusal:
            log.info("execute: решение рук %s не исполнено: %s", tool, refusal)
            return Outcome("clarify", False, refusal)
    shown = dict(args)
    if not _resolve_cur(tool, args, ctx):
        return Outcome("clarify", True, "Какое окно?")
    if dry:
        return Outcome("done", True, _dry_text(tool, shown), action=tool)
    before = ctx.undo
    try:
        out = _HANDLERS[tool](args, ctx)
    except Exception as e:
        log.exception("execute: %s упало", tool)
        return Outcome("done", False, f"Не вышло: {e}"[:200], action=tool)
    if out.ok and out.kind == "done" and tool in STATEFUL and ctx.undo is before:
        ctx.set_undo({"kind": "none", "action": tool})  # последнее действие необратимо: «Это не отменить»
    return out


# --- действия ----------------------------------------------------------------------------------------


def _done(r: Result, action: str, items: list[str] | None = None) -> Outcome:
    return Outcome("done", r.ok, r.text, items or [], r.ok and action in AUTOHIDE, action)


def _find_window(target: str | int) -> WindowInfo | None:
    try:
        return windows.find_window(target)
    except Exception:
        log.exception("execute: find_window упал")
        return None


def _focus_info(info: WindowInfo, ctx: Context) -> Outcome:
    r = windows.focus_target(info.hwnd, "user")
    if r.ok:
        ctx.remember(info.title, "window", info.hwnd)
    return _done(r, "focus")


def _open_name(target: str, kind: str | None, ctx: Context) -> Outcome:
    if kind == "url" or _is_url(target):
        return _done(files.open_target(target, "url", "user"), "open")
    folder = files.known_folder(target) if kind not in ("file", "url") else None
    if folder:
        info = _find_window(target)
        if info is not None and info.exe.casefold() == "explorer.exe":
            return _focus_info(info, ctx)  # папка уже открыта
        r = files.open_target(folder, "folder", "user")
        if r.ok:
            ctx.remember(folder, "folder")
        return _done(r, "open")
    if kind in ("folder", "file") or _is_path(target):
        r = files.open_target(target, kind, "user")
        if r.ok:
            ctx.remember(target, kind or "file")
        return _done(r, "open")
    info = _find_window(target)
    if info is not None:
        return _focus_info(info, ctx)  # приложение уже открыто
    r = files.open_target(target, "app", "user")
    if r.ok:
        app, _score = apps.resolve(target)
        ctx.remember(app.name if app else target, "app")
    return _done(r, "open")


def _open(args: dict[str, Any], ctx: Context) -> Outcome:
    target = args["target"]
    if isinstance(target, int):
        return _focus(args, ctx)
    return _open_name(target, args.get("kind"), ctx)


def _focus(args: dict[str, Any], ctx: Context) -> Outcome:
    target = args["target"]
    info = _find_window(target)
    if info is not None:
        return _focus_info(info, ctx)
    if isinstance(target, str):
        return _open_name(target, None, ctx)  # окна нет — открыть
    return _done(windows.focus_target(target, "user"), "focus")


def _close(args: dict[str, Any], ctx: Context) -> Outcome:
    target = args["target"]
    info = _find_window(target)
    r = windows.close_target(target, "user")
    if r.ok and info is not None:
        ctx.remember(info.title, "window", info.hwnd)
    return _done(r, "close")


def _win(args: dict[str, Any], ctx: Context) -> Outcome:
    target = args.get("target")
    action = args["action"]
    info = _find_window(target) if target is not None else None
    r = windows.window_action(action, target, "user")
    if r.ok and info is not None:
        ctx.remember(info.title, "window", info.hwnd)
    if r.ok and action == "minimize_all":
        ctx.set_undo({"kind": "minimize_all"})
    elif r.ok and action in ("minimize", "maximize") and info is not None:
        ctx.set_undo({"kind": "window", "action": action, "hwnd": info.hwnd})
    return _done(r, "win")


def _find(args: dict[str, Any], ctx: Context) -> Outcome:
    r = files.find(args["query"], args.get("kind") or "any", "user")
    items = [p for p in r.data if isinstance(p, str)] if isinstance(r.data, list) else []
    if not r.ok:
        return Outcome("done", False, r.text, action="find")
    ctx.set_found(items)
    return Outcome("done", True, f"Нашёл {len(items)}" if items else "Ничего не нашёл", items, False, "find")


def _vol(args: dict[str, Any], ctx: Context) -> Outcome:
    kwargs = {k: args[k] for k in ("set", "delta", "mute") if args.get(k) is not None}
    r = audio.volume(**kwargs, caller="user")
    prev = r.data.get("prev") if r.ok and isinstance(r.data, dict) else None
    if isinstance(prev, dict) and isinstance(prev.get("level"), int) and isinstance(prev.get("muted"), bool):
        ctx.set_undo({"kind": "volume", "level": prev["level"], "muted": prev["muted"]})
    return _done(r, "vol")


def _media(args: dict[str, Any], ctx: Context) -> Outcome:
    return _done(media.media(args["action"], "user"), "media")


def _kill(args: dict[str, Any], ctx: Context) -> Outcome:
    name = args["name"]
    if isinstance(name, int):
        info = _find_window(name)
        if info is None:
            return Outcome("done", False, "Окно уже закрыто", action="kill")
        name = info.exe
    r = procs.kill(name, "user")
    if r.ok:
        ctx.remember(name, "process")
    return _done(r, "kill")


def _open_found(args: dict[str, Any], ctx: Context) -> Outcome:
    items = ctx.found_items()
    if not items:
        return Outcome("done", False, "Сначала найди файлы", action="open_found")
    index = args["index"]
    if not isinstance(index, int) or isinstance(index, bool):
        return Outcome("clarify", False, "Какой по счёту?")
    i = len(items) - 1 if index == -1 else index - 1
    if not 0 <= i < len(items):
        return Outcome("done", False, f"Нет пункта {index}: найдено {len(items)}", action="open_found")
    r = files.open_target(items[i], None, "user")
    if r.ok:
        ctx.remember(items[i], "file")
    return _done(r, "open_found")


def _lock(args: dict[str, Any], ctx: Context) -> Outcome:
    return _done(system.lock("user"), "lock")


def _clock(args: dict[str, Any], ctx: Context) -> Outcome:
    now = _now()
    if args.get("what") == "date":
        text = f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}"
    else:
        text = f"Сейчас {now:%H:%M}"
    return Outcome("done", True, text, action="clock")


# --- отмена ------------------------------------------------------------------------------------------


def _undo(args: dict[str, Any], ctx: Context) -> Outcome:
    """Вернуть последнее действие как было; удачно — забыть его (вторая «отмени» — «Нечего отменять»)."""
    rec = ctx.undo_record()
    if rec is None:
        return Outcome("done", False, NOTHING_TO_UNDO, action="undo")
    fn = _UNDO.get(str(rec.get("kind")))
    if fn is None:
        return Outcome("done", False, CANT_UNDO, action="undo")
    try:
        out = fn(rec)
    except StoreError as e:
        return Outcome("done", False, str(e), action="undo")
    if out.ok:
        ctx.clear_undo()
    return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _undone(text: str) -> Outcome:
    return Outcome("done", True, text, autohide=True, action="undo")


def _undo_volume(rec: dict[str, Any]) -> Outcome:
    level, muted = _int(rec.get("level")), rec.get("muted")
    if level is None or not isinstance(muted, bool):
        return Outcome("done", False, CANT_UNDO, action="undo")
    r = audio.restore(level, muted, caller="user")
    if not r.ok:
        return _done(r, "undo")
    return _undone(f"Вернул громкость {level} %" + (", звук выключен" if muted else ""))


def _undo_window(rec: dict[str, Any]) -> Outcome:
    hwnd = _int(rec.get("hwnd"))
    if hwnd is None:
        return Outcome("done", False, CANT_UNDO, action="undo")
    return _done(windows.window_action("restore", hwnd, "user"), "undo")


def _undo_minimize_all(rec: dict[str, Any]) -> Outcome:
    return _done(windows.window_action("undo_minimize_all", None, "user"), "undo")


def _undo_reminder_add(rec: dict[str, Any]) -> Outcome:
    gone = reminders.cancel_ids([str(rec.get("id"))])
    if not gone:
        return Outcome("done", False, "Напоминание уже сработало или отменено", action="undo")
    return _undone(f"Отменил напоминание: {gone[0].text}")


def _undo_reminder_cancel(rec: dict[str, Any]) -> Outcome:
    items = rec.get("items")
    back = reminders.restore(items if isinstance(items, list) else [])
    if not back:
        return Outcome("done", False, "Эти напоминания уже есть", action="undo")
    if len(back) == 1:
        return _undone(f"Вернул напоминание {reminders.format_due(back[0].due_ts)}: {back[0].text}")
    return _undone(f"Вернул {len(back)} {plural(len(back), ('напоминание', 'напоминания', 'напоминаний'))}")


def _undo_memory_add(rec: dict[str, Any]) -> Outcome:
    gone = memory.forget_ids([str(rec.get("id"))])
    if not gone:
        return Outcome("done", False, "Этой заметки уже нет", action="undo")
    return _undone(f"Забыл: {gone[0].text}")


def _undo_memory_forget(rec: dict[str, Any]) -> Outcome:
    items = rec.get("items")
    back = memory.restore(items if isinstance(items, list) else [])
    if not back:
        return Outcome("done", False, "Эти заметки уже есть", action="undo")
    if len(back) == 1:
        return _undone(f"Вспомнил: {back[0].text}")
    return _undone(f"Вспомнил {len(back)} {plural(len(back), ('заметку', 'заметки', 'заметок'))}")


_UNDO: dict[str, Callable[[dict[str, Any]], Outcome]] = {
    "volume": _undo_volume,
    "window": _undo_window,
    "minimize_all": _undo_minimize_all,
    "reminder_add": _undo_reminder_add,
    "reminder_cancel": _undo_reminder_cancel,
    "memory_add": _undo_memory_add,
    "memory_forget": _undo_memory_forget,
}  # «none» и неизвестное — «Это не отменить»


# --- напоминания -------------------------------------------------------------------------------------


def _reminder_lines(items: list[reminders.Reminder]) -> str:
    """Строки «2. завтра в 8:15 — позвонить маме»; номер — как в общем списке (для «отмени напоминание 2»)."""
    order = {r.id: i for i, r in enumerate(reminders.pending(), 1)}
    return "\n".join(
        f"{order.get(r.id, i)}. {reminders.format_due(r.due_ts)} — {r.text}" for i, r in enumerate(items, 1)
    )


def _remind(args: dict[str, Any], ctx: Context) -> Outcome:
    due = args.get("due")
    if due is None:
        return Outcome("clarify", True, ASK_WHEN, action="remind")
    text = str(args.get("text") or "").strip()
    try:
        item = reminders.add(text, due)
    except StoreError as e:
        return Outcome("done", False, str(e), action="remind")
    ctx.set_undo({"kind": "reminder_add", "id": item.id})
    tail = f": {item.text}" if text else ""
    return Outcome("done", True, f"Напомню {reminders.format_due(item.due_ts)}{tail}", [], True, "remind")


def _reminders(args: dict[str, Any], ctx: Context) -> Outcome:
    items = reminders.pending()
    if not items:
        return Outcome("done", True, "Напоминаний нет", action="reminders")
    return Outcome("done", True, "Напоминания:\n" + _reminder_lines(items), action="reminders")


def _cancel_reminder(args: dict[str, Any], ctx: Context) -> Outcome:
    try:
        removed, candidates = reminders.cancel(args.get("which"))
    except StoreError as e:
        return Outcome("done", False, str(e), action="cancel_reminder")
    if candidates:
        question = "Какое отменить? Скажи номер, например «отмени напоминание 1»:\n"
        return Outcome("clarify", True, question + _reminder_lines(candidates), action="cancel_reminder")
    if not removed:
        text = "Не нашёл такого напоминания" if reminders.pending() else "Напоминаний нет"
        return Outcome("done", False, text, action="cancel_reminder")
    ctx.set_undo({"kind": "reminder_cancel", "items": [asdict(r) for r in removed]})
    n = len(removed)
    if n == 1:
        text = f"Отменил напоминание {reminders.format_due(removed[0].due_ts)}: {removed[0].text}"
    else:
        text = f"Отменил {n} {plural(n, ('напоминание', 'напоминания', 'напоминаний'))}"
    return Outcome("done", True, text, [], True, "cancel_reminder")


# --- память и помощь ---------------------------------------------------------------------------------


def _remember(args: dict[str, Any], ctx: Context) -> Outcome:
    try:
        item, added = memory.remember(str(args["text"]))
    except StoreError as e:
        return Outcome("done", False, str(e), action="remember")
    if not added:
        return Outcome("done", True, f"Это я уже помню: {item.text}", [], True, "remember")
    ctx.set_undo({"kind": "memory_add", "id": item.id})
    return Outcome("done", True, f"Запомнил: {item.text}", [], True, "remember")


def _forget(args: dict[str, Any], ctx: Context) -> Outcome:
    try:
        gone = memory.forget(args["which"])
    except StoreError as e:
        return Outcome("done", False, str(e), action="forget")
    if not gone:
        text = "Не нашёл такого в памяти" if memory.items() else "Я и так ничего о тебе не помню"
        return Outcome("done", False, text, action="forget")
    ctx.set_undo({"kind": "memory_forget", "items": [asdict(i) for i in gone]})
    n = len(gone)
    text = f"Забыл: {gone[0].text}" if n == 1 else f"Забыл {n} {plural(n, ('заметку', 'заметки', 'заметок'))}"
    return Outcome("done", True, text, [], True, "forget")


def _memory(args: dict[str, Any], ctx: Context) -> Outcome:
    items = memory.items()
    if not items:
        return Outcome(
            "done", True, "Я пока ничего о тебе не помню. Скажи: «запомни, что …»", action="memory"
        )
    lines = "\n".join(f"{i}. {item.text}" for i, item in enumerate(items, 1))
    return Outcome("done", True, f"Я помню:\n{lines}\nЗабыть: «забудь 2» или «забудь всё».", action="memory")


def _help(args: dict[str, Any], ctx: Context) -> Outcome:
    return Outcome("done", True, HELP_TEXT, action="help")


_HANDLERS: dict[str, Callable[[dict[str, Any], Context], Outcome]] = {
    "open": _open,
    "close": _close,
    "focus": _focus,
    "win": _win,
    "find": _find,
    "vol": _vol,
    "media": _media,
    "kill": _kill,
    "open_found": _open_found,
    "lock": _lock,
    "clock": _clock,
    "undo": _undo,
    "remind": _remind,
    "reminders": _reminders,
    "cancel_reminder": _cancel_reminder,
    "remember": _remember,
    "forget": _forget,
    "memory": _memory,
    "help": _help,
}
