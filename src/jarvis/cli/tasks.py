"""Команды задач: `jarvis tasks`, `jarvis trace`, `jarvis cancel`.

Каждая команда открывает базу из JARVIS_HOME и сначала восстанавливает задачи, чей процесс
завершился посреди работы, — чтобы показывать и менять их настоящее состояние. Сами задачи
команды M2 не выполняют.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

import typer

from jarvis.app.composition import App, build_app, open_storage
from jarvis.cli.common import load_or_exit
from jarvis.core.timeline import render_model_call, render_timeline
from jarvis.domain.errors import JarvisError
from jarvis.domain.ids import TaskId
from jarvis.domain.states import ACTIVE_STATUSES, TERMINAL_STATUSES, TaskStatus, is_terminal
from jarvis.domain.task import TaskSnapshot

STATUS_FILTERS: dict[str, frozenset[TaskStatus]] = {
    "running": ACTIVE_STATUSES,
    "waiting": frozenset({TaskStatus.WAITING_CONFIRMATION}),
    "finished": TERMINAL_STATUSES,
    **{status.value.lower(): frozenset({status}) for status in TaskStatus},
}


@contextmanager
def opened_app() -> Iterator[App]:
    loaded = load_or_exit()
    try:
        storage = open_storage(loaded.home)
    except JarvisError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    try:
        app = build_app(
            loaded.config, stages={}, storage=storage, home=loaded.home, config_file=loaded.config_path
        )
        for task_id in app.tasks.recover_interrupted():
            typer.echo(f"{task_id}: процесс, который вёл задачу, завершился — FAILED (interrupted)", err=True)
        yield app
    except JarvisError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    finally:
        storage.close()


def tasks_command(
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            "-s",
            help="running, waiting, finished или статус (completed, failed, cancelled, …); можно несколько.",
        ),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="Сколько последних задач показать.")
    ] = 20,
) -> None:
    """Последние задачи: статус, маршрут, время изменения, причина завершения."""
    statuses: set[TaskStatus] | None = None
    if status:
        unknown = [name for name in status if name.lower() not in STATUS_FILTERS]
        if unknown:
            typer.echo(
                f"неизвестный статус: {', '.join(unknown)}; есть: {', '.join(STATUS_FILTERS)}", err=True
            )
            raise typer.Exit(2)
        statuses = set().union(*(STATUS_FILTERS[name.lower()] for name in status))
    with opened_app() as app:
        found = app.tasks.list_tasks(statuses=statuses, limit=limit)
    if not found:
        typer.echo("Задач нет.")
        return
    rows = [("ID", "STATUS", "ROUTE", "UPDATED", "REASON")]
    now = datetime.now().astimezone()
    rows += [_row(task, now) for task in reversed(found)]  # старые сверху, как в журнале
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


def trace_command(
    task_id: Annotated[str, typer.Argument(help="ID задачи, например task_42.")],
    as_json: Annotated[bool, typer.Option("--json", help="Полная трасса в JSON.")] = False,
    model_io: Annotated[
        bool, typer.Option("--model-io", help="После таймлайна — промпты и ответы модели (для отладки).")
    ] = False,
) -> None:
    """Таймлайн задачи: переходы, причины, ошибки, метрики."""
    with opened_app() as app:
        inspection = app.tasks.inspect(TaskId(task_id))
        calls = app.tasks.model_calls(TaskId(task_id)) if model_io else []
    if as_json:
        document = {
            "task": inspection.task.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in inspection.events],
            "metrics": inspection.metrics.model_dump(mode="json"),
        }
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))
        return
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    timeline = render_timeline(inspection.task, inspection.events, inspection.metrics, tz=local)
    typer.echo(timeline, nl=False)
    for call in calls:
        typer.echo(render_model_call(call))


def cancel_command(
    task_id: Annotated[str, typer.Argument(help="ID задачи.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Причина отмены для трассы.")
    ] = "отменено пользователем",
) -> None:
    """Отменить задачу, которую сейчас не ведёт другой процесс."""
    with opened_app() as app:
        key = TaskId(task_id)
        before = app.tasks.get(key)
        if is_terminal(before.status):
            typer.echo(f"{key} уже завершена: {before.status}")
            return
        after = app.tasks.cancel(key, reason)
    typer.echo(f"{key}: {before.status} → {after.status}")


def _row(task: TaskSnapshot, now: datetime) -> tuple[str, str, str, str, str]:
    updated = task.updated_at.astimezone(now.tzinfo)
    when = updated.strftime("%H:%M:%S" if updated.date() == now.date() else "%Y-%m-%d %H:%M")
    error = task.outcome.error if task.outcome else None
    return (
        task.id,
        task.status.value,
        task.route.value if task.route else "-",
        when,
        error.category if error else "",
    )
