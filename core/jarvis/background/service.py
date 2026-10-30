"""Фоновые задачи: очередь, запуск заходов, вопросы и ответы, отмена, продолжение после перезапуска,
уведомления (docs/BACKGROUND_TASKS.md, контракт — docs/CONTRACTS.md, раздел 8).

Задач идёт одновременно не больше settings.background.parallel; остальные ждут очереди. Задача, которая
работала, когда Jarvis закрыли, при запуске снова встаёт в очередь и продолжается с плана и хода работы.
Вопрос человеку ждёт ответа в заходе до LIVE_WAIT_S; дольше — задача встаёт на паузу (waiting) и продолжится
новым заходом, когда человек ответит (кнопкой, текстом).

Повторяющаяся задача (Task.repeat, jarvis.background.repeat) между запусками ждёт в scheduled: раз в TICK_S
сервис запускает те, чьё время пришло (пропущенное, пока Jarvis был закрыт, — один раз). Итог запуска — в
историю задачи, следующий запуск — по расписанию; MAX_FAILS неудач подряд — повторы на паузе (paused).
Там же, раз в TICK_S, — «Входящие.md» в папке агента: новые строки становятся задачами
(jarvis.background.inbox).

Состояние задачи — файлом (TaskStore), заметка задачи в хранилище — следом за ним; события background.* —
сразу. Частые изменения по ходу работы пишутся раз в FLUSH_S.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from jarvis.ai.format import human_time, one_line, speech_text
from jarvis.ai.llm import weak_local_model
from jarvis.background import inbox, measure
from jarvis.background.model import Question, Task, TaskStore
from jarvis.background.repeat import clean_repeat, describe, next_run, parse_repeat
from jarvis.background.runner import Parked, Session, run_session
from jarvis.background.vault import STATUS_WORDS, Vault
from jarvis.confirm import parse_yes_no
from jarvis.db import new_id
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

FLUSH_S = 1.0
MAX_BRIEF = 8000
MAX_TASKS = 300  # законченных задач в списке (старые удаляются из Jarvis; заметки в хранилище остаются)
STOP_WAIT_S = 5.0
ALLOW = ("разреш", "да", "ок", "ok", "можно", "давай")
TICK_S = 15.0  # как часто смотреть расписание и «Входящие»
MAX_FAILS = 3  # неудачных запусков подряд — повторы на паузу
INBOX_BATCH = 5  # задач из «Входящих» за раз (вставили длинный список — разберётся за несколько раз)
# «Входящие» правили недавно — подождать: человек может ещё печатать (Obsidian сохраняет на ходу), а строка,
# взятая на полуслове, стала бы задачей с обрывком текста
INBOX_SETTLE_S = 30.0
INBOX_REMEMBER_S = 24 * 3600.0  # задача по строке поставлена, а отметить строку не вышло — столько помнить
INBOX_KEY = "background:inbox_pending"


def title_of(brief: str) -> str:
    """Короткое название задачи из её текста: первая фраза, до 70 знаков."""
    first = re.split(r"(?<=[.!?])\s|\n", brief.strip(), maxsplit=1)[0].strip(" .!?")
    first = re.sub(r"^(?:в фоне|фоном|фоново|поставь задачу|задача)\s*[:,—–-]?\s*", "", first, flags=re.I)
    title = one_line(first or brief, 70)
    return title[:1].upper() + title[1:]


def allowed(answer: str) -> bool:
    yes = parse_yes_no(answer)
    if yes is not None:
        return yes
    low = answer.casefold().strip()
    return low.startswith(ALLOW) and not low.startswith(("не ", "нет", "отклон"))


class Background(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.store = TaskStore()
        self.vault = Vault(ctx)
        self._tasks: dict[str, Task] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._notes_busy: set[str] = set()  # заметка задачи пишется сейчас…
        self._notes_again: set[str] = set()  # …и за это время задача снова изменилась
        self._waiters: dict[str, asyncio.Future[str]] = {}  # вопрос → ответ (пока заход ждёт)
        self._dirty: set[str] = set()
        self._flush: asyncio.TimerHandle | None = None
        self._started = False
        self._shutting = False
        self._unsubscribe: list[Callable[[], None]] = []
        self._ticker: asyncio.Task[None] | None = None
        self._inbox_seen: tuple[int, int] | None = None  # «Входящие» уже разобраны в таком виде (mtime, size)
        # строки, по которым задача поставлена, а отметить их в файле не вышло (файл был занят): текст → {note
        # (заметка задачи или «!причина»), ts}; помнится в базе — после перезапуска не будет второй задачи
        self._inbox_pending: dict[str, dict[str, Any]] = {}
        self._inbox_warned = ""
        self._warned_models: set[str] = set()  # о слабой модели на этом ПК уже сказали (тост — раз на модель)

    # --- жизнь сервиса --------------------------------------------------------------------------------
    async def start(self) -> None:
        loaded = await self.ctx.run_io(self.store.load_all)
        for task in loaded:
            if task.status == "running":
                task.status = "queued"
                task.add_log("Jarvis перезапущен — продолжаю с того же места", "note")
                self._dirty.add(task.id)
            self._tasks[task.id] = task
        self._started, self._shutting = True, False
        with contextlib.suppress(Exception):
            saved = self.ctx.db.kv_get(INBOX_KEY, {}) or {}
            self._inbox_pending = {
                str(k): v for k, v in saved.items() if isinstance(v, dict) and isinstance(v.get("note"), str)
            }
        self._unsubscribe.append(self.ctx.settings.on_change(self._settings_changed))
        self._schedule()
        if self._dirty:
            self._later()
        self._ticker = self.ctx.spawn(self._tick_loop(), "background-tick")

    async def stop(self) -> None:
        self._shutting = True
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker
        for off in self._unsubscribe:
            with contextlib.suppress(Exception):
                off()
        self._unsubscribe.clear()
        runners = [r for r in self._runners.values() if not r.done()]
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.wait(runners, timeout=STOP_WAIT_S)
        self._started = False
        if self._flush is not None:
            self._flush.cancel()
            self._flush = None
        for task_id in list(self._dirty):
            task = self._tasks.get(task_id)
            if task is not None:
                with contextlib.suppress(Exception):
                    self.store.save(task)
        self._dirty.clear()

    def _settings_changed(self, old: Any, new: Any) -> None:
        if old.background.parallel != new.background.parallel and self.ctx.loop is not None:
            self.ctx.loop.call_soon_threadsafe(self._schedule)
        was, now = old.background, new.background
        if (was.vault, was.folder, was.inbox) != (now.vault, now.folder, now.inbox):
            self._inbox_seen = None  # другое хранилище или папка — «Входящие» разобрать заново

    # --- для API ------------------------------------------------------------------------------------------
    def cards(self) -> list[dict[str, Any]]:
        """Сначала идущие, потом повторяющиеся (ближайший запуск первым), потом законченные (новые
        первыми)."""

        def order(t: Task) -> tuple[int, float]:
            if t.active:
                return 0, -t.created
            if t.status in ("scheduled", "paused"):
                return 1, t.next_run or float("inf")
            return 2, -t.created

        return [t.card() for t in sorted(self._tasks.values(), key=order)]

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise UserFacingError("Задача не найдена")
        return task

    def full(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        data = task.to_json()
        data["card"] = task.card()
        data["note_link"] = self.vault.link(task.note) if task.note else ""
        data["result_link"] = self.vault.link(task.result) if task.result else ""
        return data

    def create(
        self,
        brief: str,
        *,
        deliverable: str = "",
        source: str = "ui",
        title: str = "",
        repeat: dict[str, Any] | None = None,
        run_now: bool = True,
    ) -> Task:
        """Новая задача. repeat=None — повтор ищется в начале текста («каждый вечер записывай …»), {} — не
        повторять; повторяющаяся с run_now запускается сразу, без — в своё время."""
        brief = (brief or "").strip()
        if not brief:
            raise UserFacingError("Опишите задачу")
        if len(brief) > MAX_BRIEF:
            raise UserFacingError(f"Слишком длинное описание задачи — не больше {MAX_BRIEF} знаков")
        rest = brief
        if repeat is None:
            repeat, rest = parse_repeat(brief)
        repeat = clean_repeat(repeat)
        problem = self._ai_problem()
        if problem:
            raise UserFacingError(problem)
        task = Task(
            id=new_id(),
            brief=brief,
            title=one_line(title, 90) if title.strip() else title_of(rest),
            deliverable=one_line(deliverable, 500),
            source=source,
            repeat=repeat,
        )
        if repeat and run_now:
            self._begin_cycle(task, "сразу")
        elif repeat:
            task.status, task.next_run = "scheduled", next_run(repeat, time.time())
            first = human_time(task.next_run)
            task.add_log(f"Повторяется {describe(repeat)}; первый запуск — {first}", "note")
        self._tasks[task.id] = task
        self._trim()
        self._save_now(task)
        log.info("фоновая задача %s: «%s»%s", task.id, task.title, f" ({describe(repeat)})" if repeat else "")
        self._schedule()
        return task

    async def cancel(self, task_id: str) -> Task:
        """Отменить: идущую — остановить; повторяющуюся — и больше не запускать."""
        task = self.get(task_id)
        if not task.active and not (task.recurring and task.status in ("scheduled", "paused")):
            return task
        runner = self._runners.get(task_id)
        if runner is not None and not runner.done():
            runner.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({runner}, timeout=STOP_WAIT_S)
        if task.active or task.status in ("scheduled", "paused"):  # ждала очереди, ответа или расписания
            summary = "Повторы остановлены." if task.recurring else "Отменено."
            self._finish(task, "cancelled", summary=summary)
        return task

    def retry(self, task_id: str) -> Task:
        """«Ещё раз» и «Запустить сейчас»: повторяющаяся — новым запуском, дальше снова по расписанию."""
        task = self.get(task_id)
        if task.active:
            raise UserFacingError("Задача и так идёт")
        if task.recurring:
            task.fails = 0
            self._begin_cycle(task, "вручную")
        else:
            task.status, task.error, task.finished = "queued", "", 0.0
            task.add_log("Запущена снова", "note")
        self._save_now(task)
        self._schedule()
        return task

    def pause(self, task_id: str) -> Task:
        """Повторы на паузу (задача остаётся, запусков нет, пока не продолжить)."""
        task = self.get(task_id)
        if not task.recurring:
            raise UserFacingError("Эта задача не повторяется")
        if task.active:
            raise UserFacingError("Задача сейчас работает — поставить повторы на паузу можно после запуска")
        if task.status != "paused":
            task.status, task.next_run = "paused", 0.0
            task.add_log("Повторы на паузе", "note")
            self._save_now(task)
        return task

    def resume(self, task_id: str) -> Task:
        """Повторы снова по расписанию (после паузы или остановки)."""
        task = self.get(task_id)
        if not task.recurring:
            raise UserFacingError("Эта задача не повторяется")
        if task.active:
            return task
        task.status, task.fails, task.error = "scheduled", 0, ""
        task.next_run = next_run(task.repeat, time.time())
        task.add_log(f"Повторы продолжаются: следующий запуск — {human_time(task.next_run)}", "note")
        self._save_now(task)
        return task

    def set_repeat(self, task_id: str, raw: Any) -> Task:
        """Сменить расписание. Обычная законченная задача с расписанием становится повторяющейся; пустое
        расписание — повторы снимаются (задача остаётся с итогом последнего запуска)."""
        task = self.get(task_id)
        if task.active:
            raise UserFacingError("Задача сейчас работает — расписание можно сменить после запуска")
        repeat = clean_repeat(raw)
        if repeat:
            task.repeat, task.fails = repeat, 0
            if task.status != "paused":
                task.status = "scheduled"
                task.next_run = next_run(repeat, time.time())
            task.add_log(f"Расписание: {describe(repeat)}", "note")
        elif task.recurring:
            task.repeat, task.next_run = {}, 0.0
            last = task.history[-1]["status"] if task.history else "done"
            task.status = last if last in ("done", "partial", "failed", "cancelled") else "done"
            task.add_log("Повторы сняты", "note")
        self._save_now(task)
        return task

    async def tables(self, task_id: str) -> list[dict[str, Any]]:
        """Таблицы замеров задачи (для вкладки «Замеры»)."""
        task = self.get(task_id)
        out = []
        for rel in task.tables:
            table = await self.ctx.run_io(measure.load, self.vault, rel)
            if table is not None:
                out.append(table)
        return out

    async def delete(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.active:
            await self.cancel(task_id)
        self._tasks.pop(task_id, None)
        self._dirty.discard(task_id)
        await self.ctx.run_io(self.store.delete, task_id)
        self.ctx.bus.publish("background.deleted", {"id": task_id})

    def answer(self, task_id: str, question_id: str, text: str) -> Task:
        task = self.get(task_id)
        text = " ".join((text or "").split())
        if not text:
            raise UserFacingError("Пустой ответ")
        question = next((q for q in task.questions if q.id == question_id), None) if question_id else None
        question = question or task.open_question()
        if question is None or not question.open:
            raise UserFacingError("На этот вопрос уже ответили")
        question.answer, question.answered = text[:2000], time.time()
        if question.kind == "approval" and question.key and allowed(text):
            task.approved.append(question.key)
        task.add_log(f"Ответ: {one_line(text, 200)}", "note")
        waiter = self._waiters.get(question.id)
        if waiter is not None and not waiter.done():
            waiter.set_result(text)
        elif task.status == "waiting" and task.id not in self._runners:
            task.status = "queued"  # заход закончился паузой — продолжить новым
            self._schedule()
        self._save_now(task)
        return task

    def open_questions(self) -> list[tuple[Task, Question]]:
        """Вопросы задач, которые ждут ответа (старые первыми)."""
        found = [(t, q) for t in self._tasks.values() if t.active for q in t.questions if q.open]
        return sorted(found, key=lambda tq: tq[1].asked)

    # --- для захода задачи ---------------------------------------------------------------------------------
    def browser_tools(self) -> Any:
        """Браузер агента для одного захода (TaskBrowser: действия browser_*); нет Chrome или Edge — None."""
        browser = getattr(self.ctx, "agent_browser", None)
        if browser is None or not browser.available:
            return None
        from jarvis.ai.browser.task_tools import TaskBrowser

        return TaskBrowser(browser)

    async def ask(
        self,
        session: Session,
        text: str,
        options: list[str] | None = None,
        *,
        kind: str = "question",
        detail: str = "",
        key: str = "",
    ) -> str:
        """Вопрос человеку: ждать ответа до session.live_wait, потом — Parked (задача на паузе)."""
        task = session.task
        question = Question(
            new_id(), one_line(text, 500), kind, list(options or []), one_line(detail, 300), key=key
        )
        task.questions.append(question)
        task.status = "waiting"
        self._save_now(task)
        # ждать ответа — раньше уведомлений: ответ, данный, пока они уходят (Telegram, голос), не теряется
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[question.id] = future
        began = time.monotonic()
        try:
            self.ctx.bus.publish("background.question", {"task_id": task.id, "question": vars(question)})
            await self._notify("question", task, f"{task.title}: {question.text}", question)
            answer = await asyncio.wait_for(asyncio.shield(future), session.live_wait)
        except TimeoutError:
            raise Parked from None
        finally:
            self._waiters.pop(question.id, None)
            session.waited += time.monotonic() - began
        task.status = "running"
        self._save_now(task)
        return answer

    async def approve(self, session: Session, title: str, detail: str, key: str) -> None:
        """Разрешение на необратимое: уже разрешено в этой задаче — сразу; отказ — ошибка для модели."""
        if key and key in session.task.approved:
            return
        answer = await self.ask(
            session, title, ["Разрешить", "Отклонить"], kind="approval", detail=detail, key=key
        )
        if not allowed(answer):
            raise UserFacingError(
                f"Ошибка: человек не разрешил ({one_line(title, 120)}). Не делай этого и не обходи запрет "
                "другим путём — продолжай без этого или закончи (status=partial) и объясни."
            )

    def changed(self, task: Task) -> None:
        """Состояние изменилось по ходу работы: записать и показать — не чаще раза в FLUSH_S."""
        task.touch()
        self._dirty.add(task.id)
        self._later()

    # --- очередь и заходы ---------------------------------------------------------------------------------
    def _ai_problem(self) -> str | None:
        ai = getattr(self.ctx, "ai", None)
        if ai is None:
            return "Jarvis AI недоступен"
        if not self.ctx.settings.value.ai.enabled:
            return "Jarvis AI выключен в настройках"
        backend = ai.account_backend()
        return backend.problem() if backend is not None else ai.llm.problem()

    def _warn_weak_model(self, task: Task) -> None:
        """Маленькая модель на этом ПК — в ход задачи и тостом (тост — раз за запуск Jarvis на модель)."""
        ai = getattr(self.ctx, "ai", None)
        if ai is None or ai.account_backend() is not None:
            return
        try:
            warning = weak_local_model(ai.llm.config())
        except Exception:
            return
        if not warning:
            return
        task.add_log(warning, "note")
        model = ai.llm.config().model
        if model in self._warned_models:
            return
        self._warned_models.add(model)
        self.ctx.bus.publish(
            "toast",
            {
                "title": "Фоновая задача",
                "message": warning,
                "status": "warn",
                "icon": "ai",
                "ts": time.time(),
            },
        )

    def _schedule(self) -> None:
        if not self._started or self._shutting:
            return
        free = self.ctx.settings.value.background.parallel - len(self._runners)
        queued = sorted((t for t in self._tasks.values() if t.status == "queued"), key=lambda t: t.created)
        for task in queued[: max(0, free)]:
            self._runners[task.id] = self.ctx.spawn(self._run(task), f"task-{task.id}")

    async def _run(self, task: Task) -> None:
        settings = self.ctx.settings.value.ai
        task.status, task.error = "running", ""
        task.runs += 1
        task.started = task.started or time.time()
        task.provider, task.model = settings.provider, settings.model
        self._warn_weak_model(task)
        self._save_now(task)
        session: Session | None = None
        try:
            session = await run_session(self, task)
        except asyncio.CancelledError:
            if self._shutting:  # Jarvis закрывается — при запуске задача продолжится
                task.status = "running"
                self._dirty.add(task.id)
                return
            self._finish(task, "cancelled", summary="Отменено.")
        except UserFacingError as e:
            log.info("фоновая задача %s: %s", task.id, e)
            self._finish(task, "failed", error=str(e))
        except Exception as e:
            log.exception("фоновая задача %s упала", task.id)
            self._finish(task, "failed", error=f"Внутренняя ошибка: {e}")
        else:
            task.worked_s += session.worked()
            usage = session.usage.to_json()
            for key, value in usage.items():
                task.usage[key] = round(task.usage.get(key, 0) + value, 6)
            if session.outcome == "waiting" and task.open_question() is not None:
                task.status = "waiting"
                task.add_log("Жду ответа на вопрос — продолжу после него", "note")
                self._save_now(task)
            elif session.outcome == "waiting" and any(
                q.answered and q.asked >= session.opened for q in task.questions
            ):  # ответили, когда заход уже уходил на паузу — продолжить, а не закончить «сделано не всё»
                task.status = "queued"
                task.add_log("Ответ пришёл — продолжаю", "note")
                self._save_now(task)
            else:
                outcome = session.outcome if session.outcome in ("done", "partial", "failed") else "partial"
                summary = (
                    session.summary
                    or {"done": "Готово.", "partial": "Сделано не всё.", "failed": "Не вышло."}[outcome]
                )
                if session.result:
                    task.result = session.result
                self._finish(task, outcome, summary=summary)
        finally:
            self._runners.pop(task.id, None)
            self._schedule()

    def _begin_cycle(self, task: Task, reason: str) -> None:
        """Новый запуск повторяющейся задачи: свой план, лимиты действий и времени — с нуля."""
        task.cycle += 1
        task.cycle_started = time.time()
        task.cycle_steps, task.cycle_worked = task.steps, task.worked_s
        task.plan = []
        task.approved = []  # разрешение прошлого запуска («Купить») — не разрешение на каждый следующий
        task.status, task.error, task.finished, task.next_run = "queued", "", 0.0, 0.0
        task.add_log(f"Запуск №{task.cycle} — {reason}", "note")

    def _finish(self, task: Task, status: str, *, summary: str = "", error: str = "") -> None:
        """Итог захода. Повторяющаяся задача (кроме отмены) после итога ждёт следующего запуска: итог — в
        историю, MAX_FAILS неудач подряд — повторы на паузу."""
        ran = task.active
        now = time.time()
        task.summary = summary or task.summary
        task.error = error
        task.finished = now
        for question in task.questions:
            if question.open:
                question.answer, question.answered = "(без ответа)", now
        task.add_log(
            f"{STATUS_WORDS.get(status, status).capitalize()}: {one_line(summary or error, 300)}", "note"
        )
        final = status
        if task.recurring:
            if ran:
                task.add_history(status, summary or error)
            if status == "cancelled":
                task.next_run = 0.0
            else:
                task.fails = task.fails + 1 if status == "failed" else 0
                if task.fails >= MAX_FAILS:
                    final, task.next_run = "paused", 0.0
                    task.add_log(
                        f"Запуски не получились {task.fails} раза подряд — повторы на паузе: поправьте "
                        "задачу или подключение и нажмите «Продолжить»",
                        "error",
                    )
                else:
                    final, task.next_run = "scheduled", self._next_after(task, now)
                    task.add_log(f"Следующий запуск — {human_time(task.next_run)}", "note")
        task.status = final
        self._save_now(task)
        with contextlib.suppress(Exception):
            self.vault.journal(task, status)
        self.ctx.bus.publish(
            "background.done",
            {
                "task_id": task.id,
                "status": status,
                "summary": task.summary,
                "error": task.error,
                "note": task.note,
                "result": task.result,
                "next_run": task.next_run,
                "paused": final == "paused",
            },
        )
        if status != "cancelled":
            text = f"Задача «{task.title}»: {one_line(summary or error, 200)}"
            if final == "paused":
                text += " Повторы на паузе: запуски не получаются."
            self.ctx.spawn(self._notify("done", task, text), f"task-notify-{task.id}")

    @staticmethod
    def _next_after(task: Task, now: float) -> float:
        """Следующий запуск после этого: «каждые N часов» — от начала запуска (без сдвига на время работы),
        по времени дня — следующее такое время."""
        anchor = task.cycle_started if task.repeat.get("kind") == "hours" and task.cycle_started else now
        upcoming = next_run(task.repeat, anchor)
        while upcoming <= now:
            upcoming = next_run(task.repeat, upcoming)
        return upcoming

    def _trim(self) -> None:
        old = sorted(
            (t for t in self._tasks.values() if not t.active and t.status not in ("scheduled", "paused")),
            key=lambda t: t.created,
        )
        for task in old[: max(0, len(self._tasks) - MAX_TASKS)]:
            self._tasks.pop(task.id, None)
            with contextlib.suppress(Exception):
                self.store.delete(task.id)

    # --- расписание и «Входящие» ---------------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("фоновые задачи: проверка расписания и «Входящих» не удалась")

    async def tick(self, now: float | None = None) -> None:
        """Запуски, чьё время пришло, и новые строки во «Входящих» (раз в TICK_S; тесты зовут сами)."""
        if not self._started or self._shutting:
            return
        self._due(time.time() if now is None else now)
        await self._check_inbox()

    def _due(self, now: float) -> None:
        due = [t for t in self._tasks.values() if t.status == "scheduled" and 0 < t.next_run <= now]
        for task in sorted(due, key=lambda t: t.next_run):
            self._begin_cycle(task, "по расписанию")
            self._save_now(task)
        if due:
            self._schedule()

    async def _check_inbox(self) -> None:
        if not self.ctx.settings.value.background.inbox:
            return
        try:
            found = await self.ctx.run_io(self._read_inbox)
        except Exception as e:
            log.debug("«Входящие» не прочитались: %s", e)
            return
        if found is None:
            return
        text, stamp = found
        now = time.time()
        items = inbox.parse(text)
        present = {i.text for i in items}
        # своя строка, которую не вышло отметить, — помнить, пока она в файле (и не дольше суток)
        pending = {
            k: v
            for k, v in self._inbox_pending.items()
            if k in present and now - float(v.get("ts") or 0) < INBOX_REMEMBER_S
        }
        marks: dict[str, list[str]] = {}
        fresh = []
        for item in items:
            if item.text in pending:  # задача уже поставлена — только отметить строку
                marks.setdefault(item.text, []).append(str(pending[item.text]["note"]))
            else:
                fresh.append(item)
        waiting = False
        if fresh:
            problem = self._ai_problem()
            if problem:  # без модели задачи не поставить — строки ждут, пока её подключат
                waiting = True
                if self._inbox_warned != problem:
                    log.info("«Входящие»: задачи ждут — %s", problem)
                    self._inbox_warned = problem
            else:
                self._inbox_warned = ""
                for item in fresh[:INBOX_BATCH]:
                    note = self._take_inbox_item(item)
                    pending[item.text] = {"note": note, "ts": now}
                    marks.setdefault(item.text, []).append(note)
                waiting = len(fresh) > INBOX_BATCH  # остались ещё — разобрать на следующей проверке
        written = None
        if marks:
            try:
                result = await self.ctx.run_io(self._mark_inbox, marks)
            except Exception as e:
                log.info("«Входящие»: отметить взятые не вышло: %s", e)
                result = None
            if result is not None:
                written, done = result
                for marked in done:
                    pending.pop(marked, None)
        if pending != self._inbox_pending:
            self._inbox_pending = pending
            with contextlib.suppress(Exception):
                self.ctx.db.kv_set(INBOX_KEY, pending)
        if not pending and not waiting:
            self._inbox_seen = written or stamp

    def _take_inbox_item(self, item: inbox.Item) -> str:
        """Строка «Входящих» → задача; вернёт её заметку (для ссылки в строке) или «!причина», если не
        взялась."""
        try:
            task = self.create(item.brief, source="vault")
        except UserFacingError as e:
            log.info("«Входящие»: «%s» не взята: %s", one_line(item.text, 60), e)
            return f"!{e}"
        self.ctx.bus.publish(
            "toast",
            {
                "title": "Задача из «Входящих»",
                "message": one_line(task.title, 200),
                "status": "info",
                "icon": "task",
                "ts": time.time(),
                "task_id": task.id,
            },
        )
        return self.vault.task_note(task)

    def _read_inbox(self) -> tuple[str, tuple[int, int]] | None:
        """Текст «Входящих», если они изменились с прошлой проверки и их не правят прямо сейчас (нет файла, а
        папка агента есть — создать). Не UTF-8 — не трогать: перезапись испортила бы текст."""
        path = self.vault.inbox
        try:
            stat = path.stat()
        except FileNotFoundError:
            if self.vault.home.is_dir():
                self.vault.ensure()
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._inbox_seen or time.time() - stat.st_mtime < INBOX_SETTLE_S:
            return None
        try:
            return path.read_bytes().decode("utf-8"), stamp
        except UnicodeDecodeError:
            if self._inbox_warned != "encoding":
                log.warning("«Входящие» не в UTF-8 — пропускаю: %s", path)
                self._inbox_warned = "encoding"
            return None

    def _mark_inbox(self, marks: dict[str, list[str]]) -> tuple[tuple[int, int], set[str]] | None:
        """Отметить взятые строки — в свежем тексте файла (его могли править, пока ставились задачи). Вернёт
        (вид файла после записи, тексты отмеченных строк); отмечать нечего — None."""
        path = self.vault.inbox
        text = path.read_bytes().decode("utf-8")
        lines: dict[int, str] = {}
        left = {k: list(v) for k, v in marks.items()}
        done: set[str] = set()
        for item in inbox.parse(text):
            notes = left.get(item.text)
            if notes:
                lines[item.line] = notes.pop(0)
                done.add(item.text)
        if not lines:
            return None
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(inbox.mark(text, lines), encoding="utf-8")
        os.replace(tmp, path)
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size), done

    # --- запись и события --------------------------------------------------------------------------------
    def _save_now(self, task: Task) -> None:
        task.touch()
        self._dirty.add(task.id)
        self._write(task.id)
        self.ctx.bus.publish("background.updated", {"task": task.card()})

    def _later(self) -> None:
        loop = self.ctx.loop
        if self._flush is not None or loop is None or loop.is_closed():
            return
        self._flush = loop.call_later(FLUSH_S, self._flush_now)

    def _flush_now(self) -> None:
        self._flush = None
        for task_id in list(self._dirty):
            task = self._tasks.get(task_id)
            if task is not None:
                self._write(task_id)
                self.ctx.bus.publish("background.updated", {"task": task.card()})

    def _write(self, task_id: str) -> None:
        """Состояние — файлом сразу (быстро), заметка в хранилище — в пуле ввода-вывода."""
        task = self._tasks.get(task_id)
        self._dirty.discard(task_id)
        if task is None:
            return
        try:
            self.store.save(task)
        except Exception:
            log.exception("задача %s не записана", task_id)
        if task_id in self._notes_busy:
            self._notes_again.add(task_id)
        elif self.ctx.loop is not None and not self.ctx.loop.is_closed():
            self._notes_busy.add(task_id)
            self.ctx.spawn(self._write_note(task), f"task-note-{task_id}")

    async def _write_note(self, task: Task) -> None:
        """Заметка задачи в хранилище — по одной записи за раз; изменилась во время записи — ещё раз."""
        try:
            while True:
                self._notes_again.discard(task.id)
                try:
                    note = await self.ctx.run_io(self.vault.save_task, task)
                except Exception as e:
                    log.info("заметка задачи %s не записана: %s", task.id, e)
                    return
                if note != task.note:
                    task.note = note
                    with contextlib.suppress(Exception):
                        self.store.save(task)
                    self.ctx.bus.publish("background.updated", {"task": task.card()})
                if task.id not in self._notes_again:
                    return
        finally:
            self._notes_busy.discard(task.id)

    async def _notify(self, kind: str, task: Task, text: str, question: Question | None = None) -> None:
        settings = self.ctx.settings.value
        channels = (
            settings.background.notify_question if kind == "question" else settings.background.notify_done
        )
        if "toast" in channels:
            status = "info" if kind == "question" else ("error" if task.status == "failed" else "ok")
            self.ctx.bus.publish(
                "toast",
                {
                    "title": "Вопрос от фоновой задачи" if kind == "question" else "Фоновая задача",
                    "message": one_line(text, 200),
                    "status": status,
                    "icon": "task",
                    "ts": time.time(),
                    "task_id": task.id,
                },
            )
        if "voice" in channels and not settings.voice.silent_mode:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ctx.tts.say(speech_text(text)), 15.0)
        if "telegram" in channels:
            telegram = getattr(getattr(self.ctx, "remote", None), "telegram", None)
            ask = getattr(telegram, "ask_owner", None)
            notify = getattr(telegram, "notify_owner", None)
            with contextlib.suppress(Exception):
                if question is not None and callable(ask):  # с кнопками ответа: ответить можно из Telegram
                    await ask(task.id, question, text)
                elif callable(notify):
                    await notify(text)
