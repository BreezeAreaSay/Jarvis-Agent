"""Оркестрация запроса: роутер → грамматика | руки | мозг; поток событий для окна и CLI.

handle() отдаёт Level → (Status | TextChunk | Items)* → ровно один Done последним — даже при исключении
(traceback — в лог). Если руки вернули ask_gpt или ошибку, уровень меняется: второе событие
Level("brain" | "local", "hands→…"). Тайминги (мс) — в Done.timings и в журнал (очередь, запись в фоне).
Соседние модули (router, execute) импортируются лениво — в Core(), не на первом запросе;
в handle() это уже поиск в sys.modules.
"""

import dataclasses
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from jarvis.events import Done, Event, Items, Level, LevelName, Status

log = logging.getLogger("jarvis")

NEEDS_GPT = "Это нужно GPT, а включён локальный режим"
CANCELLED = "Отменено"
HANDS_TIMINGS = ("cache_n", "prompt_n", "prompt_ms", "predicted_ms", "predicted_per_second", "total_ms")
JOURNAL_TIMINGS = ("hotkey_to_window", "route", "grammar", "hands", "exec", "brain", "total")


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


@dataclass
class _Run:
    """Состояние одного запроса: что уже отдано и что пойдёт в журнал."""

    text: str
    t0: float
    hotkey_ms: float | None = None
    level: LevelName = "grammar"
    reason: str = ""
    level_sent: bool = False
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)


class Core:
    """Один запрос за раз. cfg можно заменить на лету (трей меняет режим) — он читается на каждый запрос."""

    def __init__(self, cfg: Any, hands: Any, brain: Any, journal: Any = None) -> None:
        # импорт роутера, грамматики и execute (~20 мс) — здесь, а не на первом запросе
        from jarvis import execute, router  # noqa: F401

        self.cfg = cfg
        self.hands = hands
        self.brain = brain
        self.journal = journal
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Отменить текущий запрос: действие после отмены не выполняется, ход мозга прерывается."""
        self._cancel.set()
        if self.brain is not None:
            try:
                self.brain.cancel()
            except Exception:
                log.exception("core: brain.cancel упал")

    def handle(
        self, text: str, ctx: Any, dry: bool = False, hotkey_ms: float | None = None
    ) -> Iterator[Event]:
        """События одного запроса. dry — решение без действия ПК; hotkey_ms — «хоткей → окно» для журнала."""
        run = _Run(text=text, t0=time.perf_counter(), hotkey_ms=hotkey_ms)
        # свой флаг у каждого запроса: новый handle() не снимает отмену с ещё не закончившегося старого
        self._cancel = run.cancel
        steps = self._steps(run, text, ctx, dry)
        error = ""
        try:
            for ev in steps:
                if isinstance(ev, Done):
                    yield self._finish(run, ev)
                    return
                if isinstance(ev, Level):
                    run.level, run.reason, run.level_sent = ev.level, ev.reason, True
                yield ev
            log.error("core: запрос завершился без Done: %r", text[:80])
            error = "Запрос завершился без ответа"
        except Exception as e:
            log.exception("core: запрос упал")
            error = _human_error(e)
        finally:
            steps.close()
        if not run.level_sent:
            run.reason = run.reason or "error"
            yield Level(run.level, run.reason)
        yield self._finish(run, Done(ok=False, text=error))

    # --- ветки --------------------------------------------------------------------------------

    def _steps(self, run: _Run, text: str, ctx: Any, dry: bool) -> Iterator[Event]:
        from jarvis import router

        t = time.perf_counter()
        route = router.route(text, ctx, self.cfg.mode)
        run.timings["route"] = _ms(t)
        if not route.text.strip():
            yield Level("grammar", "empty")
            yield Done(ok=False, text="Пустая команда")
        elif route.level == "grammar" and route.hit is not None:
            yield from self._grammar(run, route, ctx, dry)
        elif route.level in ("grammar", "hands"):
            yield from self._hands(run, route, ctx, dry)
        else:  # brain | local
            yield from self._brain(run, route, ctx, route.reason)

    def _grammar(self, run: _Run, route: Any, ctx: Any, dry: bool) -> Iterator[Event]:
        hit = route.hit
        yield Level("grammar", route.reason)
        run.tool, run.args = hit.action, dict(hit.args)
        run.timings["grammar"] = _ms(run.t0)  # до начала действия
        if run.cancel.is_set():
            yield _cancelled()
            return
        yield from self._execute(run, route, ctx, "grammar", dry)

    def _hands(self, run: _Run, route: Any, ctx: Any, dry: bool) -> Iterator[Event]:
        yield Level("hands", route.reason, model=self.cfg.hands.model)
        starting = _hands_starting(self.hands)
        if starting:
            yield Status(starting, key="hands-start")
        try:
            decision = self.hands.decide(route.text, ctx)
        except Exception as e:
            log.exception("core: руки упали")
            decision = None
            error = f"руки недоступны: {e}"
        else:
            run.timings["hands"] = {k: v for k, v in decision.timings.items() if k in HANDS_TIMINGS}
            error = decision.reason if decision.kind == "error" else ""
        if starting:  # спиннер строки → ✓/✗
            up = _hands_starting(self.hands) == "" and decision is not None
            yield Status(starting, done=True, ok=up, key="hands-start")
        if run.cancel.is_set():
            yield _cancelled()
            return
        if decision is None or decision.kind == "error":
            log.warning("core: руки — ошибка: %s", error)
            yield from self._brain(run, route, ctx, "hands→error", warn=f"Руки не справились: {error}")
            return
        run.tool, run.args = decision.tool, dict(decision.args)
        if decision.tool == "ask_gpt":
            yield from self._brain(run, route, ctx, "hands→ask_gpt")
        elif decision.tool in ("reply", "clarify"):
            key = "text" if decision.tool == "reply" else "question"
            yield Done(ok=True, text=str(decision.args.get(key, "")).strip(), autohide=False)
        else:
            yield from self._execute(run, route, ctx, "hands", dry)

    def _execute(self, run: _Run, route: Any, ctx: Any, source: str, dry: bool) -> Iterator[Event]:
        from jarvis import execute

        t = time.perf_counter()
        out = execute.run(run.tool, run.args, route.text, ctx, source, dry)
        run.timings["exec"] = _ms(t)
        if out.kind == "ask_gpt":
            yield from self._brain(run, route, ctx, f"{source}→ask_gpt")
            return
        if out.items:
            yield Items(list(out.items))
        yield Done(ok=out.ok, text=out.text, autohide=bool(out.autohide) and out.kind == "done")

    def _brain(self, run: _Run, route: Any, ctx: Any, reason: str, warn: str = "") -> Iterator[Event]:
        if self.cfg.mode == "local" or route.local_only or self.brain is None or route.level == "local":
            # пришли от рук — причина остаётся «hands→…» (stats считает долю ask_gpt)
            yield Level("local", reason if "→" in reason else "local:needs_gpt")
            if warn:
                yield Status(warn, kind="warn", done=True, ok=False)
            yield Done(ok=False, text=NEEDS_GPT)
            return
        deep = bool(route.deep)
        model = self.cfg.brain.model_deep if deep else self.cfg.brain.model_quick
        yield Level("brain", reason, model=model)
        if run.cancel.is_set():
            yield _cancelled()
            return
        for ev in self.brain.ask(route.text, ctx, deep=deep):
            if isinstance(ev, Level):
                continue  # уровень уже объявлен
            if isinstance(ev, Done):
                run.timings["brain"] = brain_timings(ev.timings)
                yield ev
                return
            yield ev
        yield Done(ok=False, text="GPT не ответил")

    # --- итог ---------------------------------------------------------------------------------

    def _finish(self, run: _Run, done: Done) -> Done:
        """Дополнить Done таймингами core и причиной, записать в журнал. Не бросает."""
        try:
            timings = dict(done.timings)
            timings.update(run.timings)
            if run.hotkey_ms is not None:
                timings["hotkey_to_window"] = run.hotkey_ms
            timings["total"] = _ms(run.t0)
            final = dataclasses.replace(
                done,
                level=run.level,
                reason=run.reason,
                timings=timings,
                cancelled=done.cancelled or run.cancel.is_set(),
            )
        except Exception:
            log.exception("core: итог не собран")
            return Done(ok=False, text="Внутренняя ошибка", level=run.level, reason=run.reason)
        self._journal(run, final)
        return final

    def _journal(self, run: _Run, done: Done) -> None:
        if self.journal is None:
            return
        record: dict[str, Any] = {
            "text": run.text,
            "mode": getattr(self.cfg, "mode", ""),
            "level": done.level,
            "reason": done.reason,
            "tool": run.tool,
            "args": run.args,
            "ok": done.ok,
            "error": "" if done.ok else done.text,
            "cancelled": done.cancelled,
        }
        record.update({k: done.timings[k] for k in JOURNAL_TIMINGS if k in done.timings})
        try:
            self.journal.write(record)
        except Exception:
            log.exception("core: запись журнала не удалась")


def brain_timings(raw: dict[str, Any]) -> dict[str, Any]:
    """Тайминги мозга для журнала в мс: first_token, total, new_thread, model, usage.

    Ключи *_ms и без префикса — мс; t_first_token / t_total — секунды (как в scripts/brain_smoke.py).
    """
    out: dict[str, Any] = {}
    for key in ("first_token", "total"):
        value = raw.get(f"{key}_ms", raw.get(key))
        seconds = raw.get(f"t_{key}")
        if value is None and isinstance(seconds, int | float) and not isinstance(seconds, bool):
            value = seconds * 1000 if seconds < 600 else seconds  # 600+ «секунд» — на деле уже мс
        if isinstance(value, int | float) and not isinstance(value, bool):
            out[key] = round(float(value), 1)
    for key in ("new_thread", "model", "usage"):
        if key in raw:
            out[key] = raw[key]
    return out


def _hands_starting(hands: Any) -> str:
    """Строка «⚙ …», если сервер рук ещё не готов (Hands.status["server"]); готов или неизвестно — ""."""
    status = getattr(hands, "status", None)
    state = status.get("server") if isinstance(status, dict) else None
    if state in (None, "ready", "ok"):
        return ""
    return "Подключаюсь к рукам…" if state == "unknown" else "Запускаю руки…"


def _cancelled() -> Done:
    return Done(ok=False, text=CANCELLED, cancelled=True)


def _human_error(e: Exception) -> str:
    """Понятный текст ошибки для окна; подробности — в логе."""
    if isinstance(e, TimeoutError):
        return "Не дождался ответа (таймаут)"
    if isinstance(e, NotImplementedError):
        return "Эта часть Jarvis ещё не готова"
    detail = str(e).strip().splitlines()[0][:160] if str(e).strip() else type(e).__name__
    return f"Не получилось: {detail}"
