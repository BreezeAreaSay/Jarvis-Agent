"""`jarvis run "<запрос>"`: задачу ведёт агент на локальной модели.

Команда — клиент ядра: создаёт задачу, продвигает её `run_until_blocked`, показывает ход по событиям
трассы и спрашивает человека, когда вызову нужно подтверждение. Ctrl+C отменяет задачу
(`TaskService.cancel`), а не обрывает процесс посреди записи.
"""

import asyncio
import contextlib
import json
import signal
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from jarvis.app.composition import App, build_app, open_storage
from jarvis.cli.common import load_or_exit
from jarvis.core.timeline import clean_block, clean_line
from jarvis.core.trace import shorten
from jarvis.domain.approvals import ApprovalDecision, ApprovalRequest, ApprovalStatus
from jarvis.domain.errors import JarvisError
from jarvis.domain.ids import TaskId
from jarvis.domain.models import ModelRole
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent

POLL_S = 0.25
CLI_CHANNEL = "cli"


def run_command(
    text: Annotated[list[str], typer.Argument(help="Запрос на естественном языке.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Всё, кроме исполнения инструментов.")] = False,
) -> None:
    """Выполнить запрос: модель выбирает действия, Jarvis исполняет их через Tool Runtime."""
    loaded = load_or_exit()
    try:
        storage = open_storage(loaded.home)
    except JarvisError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    try:
        app = build_app(loaded.config, storage=storage, home=loaded.home, config_file=loaded.config_path)
        assert app.models is not None
        if not app.models.available(ModelRole.EXECUTOR):
            typer.echo(
                "Модель не настроена: назначьте роли executor эндпоинт в config.toml "
                "([models.endpoints.<id>] и [models.roles]); как — docs/development.md, «Настройка модели».",
                err=True,
            )
            raise typer.Exit(2)
        for task_id in app.tasks.recover_interrupted():
            typer.echo(f"{task_id}: процесс, который вёл задачу, завершился — FAILED (interrupted)", err=True)
        request = TaskRequest(
            text=" ".join(text), origin=Origin.CLI, working_directory=str(Path.cwd()), dry_run=dry_run
        )
        task_id = app.tasks.submit(request)
        typer.echo(f"{task_id}{' (dry run)' if dry_run else ''}")
        snapshot = asyncio.run(_drive(app, task_id))
    except JarvisError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    finally:
        storage.close()
    _report(snapshot)
    if snapshot.status is not TaskStatus.COMPLETED:
        raise typer.Exit(1)


async def _drive(app: App, task_id: TaskId) -> TaskSnapshot:
    loop = asyncio.get_running_loop()

    def interrupt(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(_cancel, app, task_id)

    previous = signal.signal(signal.SIGINT, interrupt)
    progress = _Progress(app, task_id)
    try:
        while True:
            follower = asyncio.create_task(progress.follow())
            try:
                snapshot = await app.tasks.run_until_blocked(task_id)
            finally:
                follower.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await follower
            progress.print_new()
            if snapshot.status is not TaskStatus.WAITING_CONFIRMATION:
                return snapshot
            pending = [item for item in app.tasks.approvals(task_id) if item.status is ApprovalStatus.PENDING]
            if not pending:
                return snapshot
            signal.signal(signal.SIGINT, previous)  # у вопроса человеку Ctrl+C — обычное прерывание
            try:
                approved = _ask(pending[-1])
            except (KeyboardInterrupt, typer.Abort):
                typer.echo("")
                return app.tasks.cancel(task_id, "отменено пользователем (Ctrl+C)")
            finally:
                signal.signal(signal.SIGINT, interrupt)
            decision = ApprovalDecision.APPROVE if approved else ApprovalDecision.DENY
            app.tasks.resolve_approval(pending[-1].id, decision, via=CLI_CHANNEL)
    finally:
        signal.signal(signal.SIGINT, previous)


def _cancel(app: App, task_id: TaskId) -> None:
    typer.echo("\nОтмена…", err=True)
    try:
        app.tasks.cancel(task_id, "отменено пользователем (Ctrl+C)")
    except JarvisError as exc:
        typer.echo(exc.message, err=True)


class _Progress:
    """Ход задачи по событиям трассы: каждое событие показывается один раз."""

    def __init__(self, app: App, task_id: TaskId) -> None:
        self._app = app
        self._task_id = task_id
        self._seen = 0

    async def follow(self) -> None:
        while True:
            self.print_new()
            await asyncio.sleep(POLL_S)

    def print_new(self) -> None:
        for event in self._app.tasks.trace(self._task_id):
            if event.seq <= self._seen:
                continue
            line = progress_line(event)
            if line is not None:
                typer.echo(line)
            self._seen = event.seq


def progress_line(event: TraceEvent) -> str | None:
    """Короткая строка хода задачи; содержимое событий — данные, управляющие символы экранируются."""
    payload = event.payload
    match event.kind:
        case EventKind.ACTION_PROPOSED:
            target = payload.get("tool") if payload.get("type") == "tool" else "ответ"
            decision = shorten(str(payload.get("decision")), 160)
            return f"· шаг {payload.get('step')}: {clean_line(target)} — {clean_line(decision)}"
        case EventKind.MODEL_CALLED if payload.get("status") == "invalid":
            problems = payload.get("problems")
            first = problems[0] if isinstance(problems, list) and problems else "?"
            return f"  ответ модели не принят: {clean_line(first)}"
        case EventKind.MODEL_CALLED if payload.get("status") == "error":
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else "?"
            return f"  модель: {clean_line(message)}"
        case EventKind.POLICY_DECIDED if payload.get("outcome") == "deny":
            return f"  отказано: {clean_line(payload.get('reason'))}"
        case EventKind.TOOL_FINISHED:
            return f"  {payload.get('tool')}: {payload.get('status')}, {payload.get('duration_ms')} мс"
        case EventKind.TOOL_VERIFIED if not payload.get("passed"):
            return "  проверка результата не пройдена"
        case _:
            return None


def _ask(approval: ApprovalRequest) -> bool:
    effects = ", ".join(f"{effect.kind.value} {effect.resource}" for effect in approval.effects) or "нет"
    arguments = json.dumps(approval.arguments, ensure_ascii=False)
    typer.echo(f"\nНужно подтверждение ({approval.id}):")
    typer.echo(f"  инструмент: {approval.call.tool_id}")
    typer.echo(f"  что: {clean_line(approval.summary)}")
    typer.echo(f"  эффекты: {clean_line(effects)}")
    typer.echo(f"  аргументы: {clean_line(arguments)}")
    typer.echo(f"  до: {approval.expires_at.astimezone():%H:%M:%S}")
    return typer.confirm("Разрешить?", default=False)


def _report(snapshot: TaskSnapshot) -> None:
    outcome = snapshot.outcome
    if snapshot.status is TaskStatus.COMPLETED and outcome is not None and outcome.answer is not None:
        typer.echo(f"\n{clean_block(outcome.answer)}")
    else:
        typer.echo(f"\n{snapshot.status}")
        error = outcome.error if outcome else None
        if error is not None:
            typer.echo(f"{error.category}: {clean_block(error.message)}", err=True)
    usage = snapshot.usage
    typer.echo(
        f"\n{snapshot.id} · шагов {usage.steps} · вызовов модели {usage.model_calls} · "
        f"инструментов {usage.tool_calls} · токенов {usage.model_tokens}\n"
        f"Подробно: jarvis trace {snapshot.id}"
    )
