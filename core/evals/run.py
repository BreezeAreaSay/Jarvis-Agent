"""Прогон набора задач: каждая — repeat раз (модель недетерминирована: успех — доля из N), оценка, журналы
неудачных попыток, summary.json и report.md в папке прогона, сравнение с базовой линией."""

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from evals.harness import Options, attempt
from evals.report import render, scrub, write_journal_md, write_summary
from evals.scoring import Result, compare, score, summarize
from evals.sim.render import font_ok
from evals.tasks.base import Task
from jarvis import __version__
from jarvis.ai.vision import sees_images


def needs_skip(task: Task, options: Options) -> bool:
    """Задаче нужны картинки экрана, а модель их не видит."""
    return task.vision and not sees_images(options.provider, options.model, options.vision)


async def run_tasks(
    tasks: list[Task],
    options: Options,
    *,
    repeat: int = 1,
    mistakes: bool = False,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[Result]]:
    """Все попытки по очереди (каталог данных у ядра один на процесс). mistakes — вместо эталонных решений
    детерминированного режима нарочно ошибочные: каждая — отдельной попыткой."""
    out = options.out_dir
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    skipped: list[str] = []
    plan: list[tuple[Task, int, str]] = []
    for task in tasks:
        if needs_skip(task, options):
            skipped.append(task.id)
            continue
        if mistakes:
            plan += [(task, n, name) for n, name in enumerate(task.mistakes, start=1)]
        else:
            plan += [(task, n, options.variant) for n in range(1, repeat + 1)]
    started = time.strftime("%Y-%m-%d %H:%M")
    began = time.monotonic()
    for index, (task, n, variant) in enumerate(plan, start=1):
        label = f"{task.id}-{n}" + (f"-{variant}" if variant else "")
        outcome = await attempt(task, replace(options, variant=variant), label=label)
        result = score(
            task,
            outcome.desktop,
            outcome.trace,
            attempt=n,
            variant=variant,
            script_errors=outcome.script_errors,
        )
        if outcome.journal is not None:
            if result.ok and options.journals == "failed":
                shutil.rmtree(outcome.journal, ignore_errors=True)
            else:
                md = write_journal_md(outcome.journal, {"id": task.id, "text": task.text}, result.to_json())
                if md is not None and out is not None:
                    result.journal = md.relative_to(out).as_posix()
        results.append(result)
        if progress is not None:
            mark = "✅" if result.ok else "❌"
            tail = "" if result.ok else f" — {result.reason[:160]}"
            progress(
                f"[{index:>2}/{len(plan)}] {mark} {label}: {result.steps} шагов, {result.seconds} с, "
                f"${result.cost:.4f}{tail}"
            )
    meta = {
        **options.meta(),
        "repeat": repeat,
        "mistakes": mistakes,
        "started": started,
        "duration_s": round(time.monotonic() - began, 1),
        "jarvis": __version__,
        "skipped": skipped,
        "font_ok": font_ok(),
    }
    return summarize(tasks, results, meta), results


def finish_run(summary: dict[str, Any], options: Options, baseline: Path | None) -> dict[str, Any] | None:
    """summary.json, сравнение с базовой линией и report.md — в папку прогона; ключ вычищается."""
    comparison = None
    if baseline is not None and baseline.is_file():
        comparison = compare(summary, json.loads(baseline.read_text(encoding="utf-8")))
        summary["comparison"] = comparison
    if options.out_dir is not None:
        write_summary(summary, options.out_dir)
        (options.out_dir / "report.md").write_text(render(summary, comparison), encoding="utf-8")
        scrub(options.out_dir, options.api_key)
    return comparison


def save_baseline(summary: dict[str, Any], path: Path) -> None:
    """Базовая линия — сводка без подробностей попыток (их место — папка прогона)."""
    slim = {**summary, "tasks": [{k: v for k, v in t.items() if k != "results"} for t in summary["tasks"]]}
    slim.pop("comparison", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
