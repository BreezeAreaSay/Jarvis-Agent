"""Одна попытка задачи: свой каталог данных и контекст ядра, мир задачи вместо Windows, агент как в жизни.

Каждая попытка — с чистого листа: временный каталог данных (настройки, база, журнал агента, ключ),
build_context("fake"), затем ctx.platform = SimPlatform(мир), ctx.agent.ui = SimUi(мир), уведомления —
SimToasts(мир). Модель — настоящая (живой режим: провайдер и ключ из параметров прогона) или по сценарию
(ScriptedLlm с эталонным решением задачи). «Человек» (SimUser) отвечает на просьбы о разрешении и на вопросы
ask_user так, как велит задача, и всё записывает: что спросили, на каком шаге, что ответил.

После попытки: след (Trace) — итог агента, разрешения, вопросы, шаги, время, токены; журнал агента
(шаги, ответы, картинки экрана) копируется в папку прогона (у неудачных — всегда), временный каталог
удаляется вместе с ключом.
"""

import asyncio
import contextlib
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.script import Driver, scripted_llm
from evals.sim.desktop import Desktop
from evals.sim.platform import SimPlatform
from evals.sim.ui import SimToasts, SimUi
from evals.tasks.base import Approval, Question, Task, Trace
from jarvis import paths
from jarvis.services import UserFacingError

SCRIPTED_KEY = "sk-scripted"  # не настоящий: детерминированный режим в сеть не ходит


@dataclass
class Options:
    """Параметры прогона (ключ — только в памяти: в отчёт и журналы не попадает)."""

    mode: str = "scripted"  # scripted | live
    provider: str = "openai"
    model: str = "scripted"
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    vision: str = "on"  # on | off | auto
    speed: str = "fast"  # settings.ai.speed
    max_steps: int = 25
    timeout_s: float = 300.0
    variant: str = ""  # детерминированный режим: "" — эталонное решение, иначе имя ошибки задачи
    journals: str = "failed"  # failed | all | none — чьи журналы класть в папку прогона
    out_dir: Path | None = None

    @property
    def scripted(self) -> bool:
        return self.mode == "scripted"

    @property
    def world_speed(self) -> float:
        """Время мира: в детерминированном режиме медленное окно грузится в 10 раз быстрее."""
        return 10.0 if self.scripted else 1.0

    def meta(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "vision": self.vision,
            "speed": self.speed,
            "max_steps": self.max_steps,
            "timeout_s": self.timeout_s,
            "variant": self.variant,
        }


@dataclass
class Outcome:
    """Что получилось в попытке — сырьё для оценки (evals.scoring)."""

    desktop: Desktop
    trace: Trace
    journal: Path | None = None  # копия журнала агента в папке прогона
    script_errors: list[str] = field(default_factory=list)


class SimUser:
    """Человек у компьютера: решает «Разрешить?» и отвечает на вопросы агента — как велит задача."""

    def __init__(self, ctx: Any, desktop: Desktop, task: Task) -> None:
        self.ctx = ctx
        self.desktop = desktop
        self.task = task
        self.approvals: list[Approval] = []
        self.questions: list[Question] = []
        self.titles: list[str] = []  # шаги агента: «Нажимаю «Написать»»
        self._asked = ""
        self._off = [
            ctx.bus.on("confirm.request", self._confirm),
            ctx.bus.on("agent.step", self._step),
            ctx.bus.on("agent.state", self._state),
            ctx.bus.on("chat.message", self._message),
        ]

    def close(self) -> None:
        for off in self._off:
            off()

    def _step(self, event: Any) -> None:
        if event.data.get("status") == "running":
            world = self.desktop.world
            with world.lock:
                world.step = int(event.data.get("index") or 0)
            self.titles.append(str(event.data.get("title") or ""))

    def _confirm(self, event: Any) -> None:
        data = event.data
        world = self.desktop.world
        with world.lock:
            approval = Approval(
                str(data.get("title") or ""),
                str(data.get("detail") or ""),
                False,
                world.step,
                world.seq,
                str(data.get("kind") or ""),
            )
            approval.allowed = bool(self.task.approve(approval, self.desktop))
        self.approvals.append(approval)
        self.ctx.confirmations.resolve(data["id"], approval.allowed)

    def _message(self, event: Any) -> None:
        message = event.data.get("message") or {}
        if message.get("role") == "assistant" and self.ctx.agent.waiting_answer:
            self._asked = str(message.get("content") or "")

    def _state(self, event: Any) -> None:
        text = str(event.data.get("text") or "")
        if event.data.get("status") == "waiting" and text.startswith("Жду ответа"):
            asyncio.get_running_loop().call_soon(self._answer, text.removeprefix("Жду ответа:").strip())

    def _answer(self, short: str) -> None:
        if not self.ctx.agent.waiting_answer:
            return
        question = self._asked or short
        self._asked = ""
        answer = self.task.answer(question, self.desktop)
        world = self.desktop.world
        with world.lock:
            self.questions.append(Question(question, answer, world.step, world.seq))
        self.ctx.agent.interject(answer)


async def attempt(task: Task, options: Options, *, label: str = "") -> Outcome:
    """Одна попытка задачи (сразу после неё — оценка в evals.scoring)."""
    from jarvis.app import build_context

    desktop = Desktop.build(speed=options.world_speed)
    task.setup(desktop)
    previous = getattr(paths, "_override", None)
    folder = Path(tempfile.mkdtemp(prefix="jarvis-eval-"))
    paths.set_data_dir(folder)
    ctx = build_context("fake")
    user: SimUser | None = None
    driver: Driver | None = None
    try:
        world = desktop.world
        ctx.platform = SimPlatform(world)
        agent: Any = ctx.agent
        agent.ui = SimUi(world)
        agent.toast_source = SimToasts(world)
        agent.pointer_delay_s = 0.0
        if options.scripted:
            agent.settle_s = 0.0
            agent.launch_settle_s = 0.0
        ctx.settings.patch(
            {
                "ai": {
                    "provider": options.provider,
                    "model": options.model,
                    "base_url": options.base_url,
                    "vision": options.vision,
                    "speed": options.speed,
                    "max_agent_steps": options.max_steps,
                    "agent_confirm": "risky",
                    "agent_journal": True,
                    "agent_notes": False,  # заметки о программах — между попытками не переносятся
                    "memory": False,
                    "web_search": False,
                    "custom_instructions": "",
                },
                "voice": {"silent_mode": True},
            }
        )
        if options.scripted:
            steps = (
                task.solution(desktop) if not options.variant else task.mistakes[options.variant][0](desktop)
            )
            llm, driver = scripted_llm(desktop, steps, task.plan)
            ctx.secrets.set("ai_api_key", SCRIPTED_KEY)
            ctx.ai.set_transport(llm.transport)  # type: ignore[attr-defined]

            async def no_sleep(_: float) -> None:
                return None

            ctx.ai.llm.sleep = no_sleep  # type: ignore[attr-defined]
        elif options.api_key:
            ctx.secrets.set("ai_api_key", options.api_key)
        await ctx.commands.refresh_apps(True)
        user = SimUser(ctx, desktop, task)
        started = time.monotonic()
        try:
            run_id = await agent.run(task.text, "text")
        except UserFacingError as e:  # агент не начал: провайдер не настроен, нет ключа
            trace = Trace(status="error", outcome="failed", summary=str(e), error=str(e))
            return Outcome(desktop, trace, None, [])
        timed_out = False
        try:
            result = await asyncio.wait_for(agent.wait(run_id), options.timeout_s)
        except TimeoutError:
            timed_out = True
            await agent.stop_run()
            result = await agent.wait(run_id)
        seconds = time.monotonic() - started
        run = agent.current
        usage = run.usage.to_json() if run is not None else {}
        trace = Trace(
            status=result.status,
            outcome=result.outcome,
            summary=result.summary,
            error=result.error,
            approvals=list(user.approvals),
            questions=list(user.questions),
            steps=run.count if run is not None else 0,
            seconds=round(seconds, 2),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            actions=[str(a.get("title") or "") for a in result.actions],
            timed_out=timed_out,
        )
        journal = run.journal.dir if run is not None and run.journal is not None else None
        copied = _keep_journal(journal, options, label or task.id) if journal is not None else None
        return Outcome(desktop, trace, copied, list(driver.errors) if driver is not None else [])
    finally:
        if user is not None:
            user.close()
        with contextlib.suppress(Exception):
            await ctx.agent.stop_run()
        with contextlib.suppress(Exception):
            await ctx.shutdown_pools()
        ctx.db.close()
        paths.set_data_dir(previous)
        shutil.rmtree(folder, ignore_errors=True)


def _keep_journal(source: Path, options: Options, label: str) -> Path | None:
    """Журнал попытки — в папку прогона (решает оценка: неудачные остаются всегда)."""
    if options.out_dir is None or options.journals == "none" or not source.is_dir():
        return None
    target = options.out_dir / "journals" / label
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)
    return target
