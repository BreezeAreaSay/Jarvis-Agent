"""Фоновые задачи: очередь, запуск заходов, вопросы и ответы, отмена, продолжение после перезапуска,
уведомления (docs/BACKGROUND_TASKS.md, контракт — docs/CONTRACTS.md, раздел 8).

Задач идёт одновременно не больше settings.background.parallel; остальные ждут очереди. Задача, которая
работала, когда Jarvis закрыли, при запуске снова встаёт в очередь и продолжается с плана и хода работы.
Вопрос человеку ждёт ответа в заходе до LIVE_WAIT_S; дольше — задача встаёт на паузу (waiting) и продолжится
новым заходом, когда человек ответит (кнопкой, текстом).

Состояние задачи — файлом (TaskStore), заметка задачи в хранилище — следом за ним; события background.* —
сразу. Частые изменения по ходу работы пишутся раз в FLUSH_S.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from jarvis.ai.format import one_line, speech_text
from jarvis.background.model import Question, Task, TaskStore
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
        self._unsubscribe.append(self.ctx.settings.on_change(self._settings_changed))
        self._schedule()
        if self._dirty:
            self._later()

    async def stop(self) -> None:
        self._shutting = True
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

    # --- для API ------------------------------------------------------------------------------------------
    def cards(self) -> list[dict[str, Any]]:
        tasks = sorted(self._tasks.values(), key=lambda t: (not t.active, -t.created))
        return [t.card() for t in tasks]

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

    def create(self, brief: str, *, deliverable: str = "", source: str = "ui", title: str = "") -> Task:
        brief = (brief or "").strip()
        if not brief:
            raise UserFacingError("Опишите задачу")
        if len(brief) > MAX_BRIEF:
            raise UserFacingError(f"Слишком длинное описание задачи — не больше {MAX_BRIEF} знаков")
        problem = self._ai_problem()
        if problem:
            raise UserFacingError(problem)
        task = Task(
            id=new_id(),
            brief=brief,
            title=one_line(title, 90) if title.strip() else title_of(brief),
            deliverable=one_line(deliverable, 500),
            source=source,
        )
        self._tasks[task.id] = task
        self._trim()
        self._save_now(task)
        log.info("фоновая задача %s: «%s»", task.id, task.title)
        self._schedule()
        return task

    async def cancel(self, task_id: str) -> Task:
        task = self.get(task_id)
        if not task.active:
            return task
        runner = self._runners.get(task_id)
        if runner is not None and not runner.done():
            runner.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({runner}, timeout=STOP_WAIT_S)
        if task.active:  # ждала очереди или ответа без захода
            self._finish(task, "cancelled", summary="Отменено.")
        return task

    def retry(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.active:
            raise UserFacingError("Задача и так идёт")
        task.status, task.error, task.finished = "queued", "", 0.0
        task.add_log("Запущена снова", "note")
        self._save_now(task)
        self._schedule()
        return task

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
        self.ctx.bus.publish("background.question", {"task_id": task.id, "question": vars(question)})
        await self._notify("question", task, f"{task.title}: {question.text}")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[question.id] = future
        began = time.monotonic()
        try:
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

    def _finish(self, task: Task, status: str, *, summary: str = "", error: str = "") -> None:
        task.status = status
        task.summary = summary or task.summary
        task.error = error
        task.finished = time.time()
        for question in task.questions:
            if question.open:
                question.answer, question.answered = "(без ответа)", time.time()
        task.add_log(
            f"{STATUS_WORDS.get(status, status).capitalize()}: {one_line(summary or error, 300)}", "note"
        )
        self._save_now(task)
        with contextlib.suppress(Exception):
            self.vault.journal(task)
        self.ctx.bus.publish(
            "background.done",
            {
                "task_id": task.id,
                "status": status,
                "summary": task.summary,
                "error": task.error,
                "note": task.note,
                "result": task.result,
            },
        )
        if status != "cancelled":
            text = f"Задача «{task.title}»: {one_line(summary or error, 200)}"
            self.ctx.spawn(self._notify("done", task, text), f"task-notify-{task.id}")

    def _trim(self) -> None:
        old = sorted((t for t in self._tasks.values() if not t.active), key=lambda t: t.created)
        for task in old[: max(0, len(self._tasks) - MAX_TASKS)]:
            self._tasks.pop(task.id, None)
            with contextlib.suppress(Exception):
                self.store.delete(task.id)

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

    async def _notify(self, kind: str, task: Task, text: str) -> None:
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
            notify = getattr(telegram, "notify_owner", None)
            if callable(notify):
                with contextlib.suppress(Exception):
                    await notify(text)
