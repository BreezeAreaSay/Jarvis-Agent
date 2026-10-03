"""`jarvis bench`: бенчмарк агента на настоящей модели (benchmarks/README.md).

- `bench agent` — датасет на модели из конфига пользователя (сервер он запускает сам) или `--reference`:
  эталонные решения на scripted-модели — проверка датасета и оценщика;
- `bench run` — кандидаты из TOML: бенчмарк сам запускает llama-server с параметрами кандидата, проверяет
  offload, меряет память и скорость, прогоняет датасет и пишет отчёт по каждому и сравнение;
- `bench compare` — сравнение готовых отчётов.
"""

import asyncio
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from jarvis.cli.common import load_or_exit
from jarvis.domain.errors import ConfigError
from jarvis.evals.bench.agent import TASK_TIMEOUT_S
from jarvis.evals.bench.dataset import Dataset, DatasetError, load_dataset
from jarvis.evals.bench.grading import TaskResult
from jarvis.evals.bench.report import BenchReport, load_report, render_comparison, select, write_report
from jarvis.evals.bench.run import bench_candidate, bench_configured, bench_reference
from jarvis.evals.bench.server import BenchServerError, Candidate, CandidatesFile

DEFAULT_DATASET = Path("benchmarks/agent/dataset.yaml")
DEFAULT_CANDIDATES = Path("benchmarks/candidates.toml")
DEFAULT_RESULTS = Path("benchmarks/results")

bench_app = typer.Typer(no_args_is_help=True, help="Бенчмарк агента на настоящей модели.")

DatasetOption = Annotated[Path, typer.Option("--dataset", help="Файл датасета.")]
TasksOption = Annotated[
    list[str] | None, typer.Option("--task", "-t", help="Только задачи с этими ID (можно несколько).")
]
OutOption = Annotated[Path, typer.Option("--out", help="Папка для результатов.")]
TimeoutOption = Annotated[float, typer.Option("--timeout", help="Предел времени на одну задачу, с.")]


@bench_app.command("agent")
def bench_agent(
    dataset_path: DatasetOption = DEFAULT_DATASET,
    reference: Annotated[
        bool, typer.Option("--reference", help="Эталонные решения вместо модели: проверка датасета.")
    ] = False,
    tasks: TasksOption = None,
    label: Annotated[str | None, typer.Option("--label", help="Имя прогона в отчёте.")] = None,
    out: OutOption = DEFAULT_RESULTS,
    timeout: TimeoutOption = TASK_TIMEOUT_S,
) -> None:
    """Датасет на модели роли executor из конфига (или на эталоне с --reference)."""
    dataset = _dataset(dataset_path, tasks)
    if reference:
        report = asyncio.run(bench_reference(dataset, dataset_path, on_result=_progress))
    else:
        loaded = load_or_exit()
        try:
            report = asyncio.run(
                bench_configured(
                    dataset,
                    dataset_path,
                    loaded.config,
                    label=label or "configured",
                    real_home=loaded.home,
                    real_config=loaded.config_path,
                    task_timeout_s=timeout,
                    on_result=_progress,
                )
            )
        except ConfigError as exc:
            typer.echo(exc.message, err=True)
            raise typer.Exit(2) from None
    folder = out / f"{_stamp()}-{report.label}"
    _finish(report, folder)
    if reference and report.summary.success_rate < 1.0:
        typer.echo("Эталон решает не все задачи: датасет или оценщик неисправны.", err=True)
        raise typer.Exit(1)


@bench_app.command("run")
def bench_run(
    candidates_path: Annotated[Path, typer.Argument(help="Файл кандидатов (TOML).")] = DEFAULT_CANDIDATES,
    only: Annotated[
        list[str] | None, typer.Option("--only", "-c", help="Только кандидаты с этими ID.")
    ] = None,
    server: Annotated[
        str | None, typer.Option("--server", help="Путь к llama-server (вместо указанного в файле).")
    ] = None,
    dataset_path: DatasetOption = DEFAULT_DATASET,
    tasks: TasksOption = None,
    out: OutOption = DEFAULT_RESULTS,
    repeats: Annotated[int, typer.Option("--repeats", min=1, help="Повторов замера скорости.")] = 3,
    fetch: Annotated[
        bool,
        typer.Option("--prefetch/--no-prefetch", help="Скачать модель (-hf) до замера холодного старта."),
    ] = True,
    timeout: TimeoutOption = TASK_TIMEOUT_S,
    allow_cpu_only: Annotated[
        bool, typer.Option("--allow-cpu-only", help="Не останавливать кандидата, если сервер не видит GPU.")
    ] = False,
) -> None:
    """Кандидаты по очереди: сервер с явными параметрами → offload, память, скорость → датасет → отчёт."""
    candidates = _candidates(candidates_path, only, server)
    dataset = _dataset(dataset_path, tasks)
    loaded = load_or_exit()
    session = out / _stamp()
    reports: list[BenchReport] = []
    failed: list[str] = []
    for position, candidate in enumerate(candidates, start=1):
        typer.echo(
            f"\n=== [{position}/{len(candidates)}] {candidate.id} ({candidate.klass}, {candidate.quant})"
        )
        folder = session / candidate.id
        try:
            report = bench_candidate(
                candidate,
                dataset,
                dataset_path,
                folder,
                repeats=repeats,
                fetch=fetch,
                real_home=loaded.home,
                real_config=loaded.config_path,
                task_timeout_s=timeout,
                on_result=_progress,
                say=typer.echo,
                allow_cpu_only=allow_cpu_only,
            )
        except (BenchServerError, OSError) as exc:
            # Следующий кандидат всё равно меряется: один не поместившийся в память не обрывает сессию.
            typer.echo(str(exc), err=True)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "error.txt").write_text(str(exc), encoding="utf-8")
            failed.append(candidate.id)
            continue
        _finish(report, folder)
        reports.append(report)
    if reports:
        comparison = session / "comparison.md"
        comparison.write_text(render_comparison(reports), encoding="utf-8")
        typer.echo(f"\nСравнение: {comparison}")
        _print_ranking(reports)
    if failed:
        typer.echo(f"Не запустились: {', '.join(failed)} (подробности — error.txt и server.log)", err=True)
        raise typer.Exit(1)


@bench_app.command("compare")
def bench_compare(
    paths: Annotated[
        list[Path], typer.Argument(help="report.json, папка кандидата или папка сессии со всеми кандидатами.")
    ],
    out: Annotated[Path | None, typer.Option("--out", help="Записать сравнение в файл.")] = None,
) -> None:
    """Сравнить готовые отчёты и применить правило выбора."""
    reports: list[BenchReport] = []
    for file in [file for path in paths for file in _report_files(path)]:
        try:
            reports.append(load_report(file))
        except (OSError, ValueError) as exc:
            typer.echo(f"{file}: {exc}", err=True)
            raise typer.Exit(2) from None
    if not reports:
        typer.echo("отчёты не найдены", err=True)
        raise typer.Exit(2)
    text = render_comparison(reports)
    if out is None:
        typer.echo(text, nl=False)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"Сравнение: {out}")


def _report_files(path: Path) -> list[Path]:
    """Папка сессии (`results/<время>`) раскрывается в отчёты кандидатов: в PowerShell `*` не раскрывается."""
    if not path.is_dir():
        return [path]
    if (path / "report.json").exists():
        return [path / "report.json"]
    return sorted(path.glob("*/report.json"))


def _dataset(path: Path, tasks: list[str] | None) -> Dataset:
    try:
        dataset = load_dataset(path)
        return dataset.select(tasks) if tasks else dataset
    except DatasetError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(2) from None


def _candidates(path: Path, only: list[str] | None, server: str | None) -> list[Candidate]:
    try:
        data = CandidatesFile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        if server is not None:
            data = data.model_copy(update={"defaults": {**data.defaults, "server": server}})
        candidates = data.resolve()
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        typer.echo(f"{path}: {exc}", err=True)
        raise typer.Exit(2) from None
    ids = [candidate.id for candidate in candidates]
    if len(set(ids)) != len(ids):
        typer.echo(f"{path}: ID кандидатов повторяются", err=True)
        raise typer.Exit(2)
    if only:
        unknown = set(only) - set(ids)
        if unknown:
            typer.echo(f"нет кандидатов: {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(2)
        candidates = [candidate for candidate in candidates if candidate.id in only]
    for candidate in candidates:
        if shutil.which(candidate.server) is None:
            typer.echo(
                f"{candidate.id}: llama-server не найден: {candidate.server} (укажите --server или "
                "server в файле кандидатов)",
                err=True,
            )
            raise typer.Exit(2)
    return candidates


def _progress(result: TaskResult) -> None:
    mark = "✓" if result.success else "✗"
    tool = result.first_tool or "—"
    line = (
        f"{mark} {result.id:<28} {tool:<22} шагов {result.steps:<2} "
        f"модель {result.model_calls:<2} {result.duration_ms / 1000:6.1f} с"
    )
    typer.echo(line)
    for failure in result.failures:
        typer.echo(f"    {failure}")


def _finish(report: BenchReport, folder: Path) -> None:
    summary = report.summary
    json_path, markdown_path = write_report(report, folder)
    typer.echo(
        f"Итого {report.label}: успех {summary.success_rate * 100:.1f} % ({summary.tasks} задач), "
        f"схема с первой попытки {_pct(summary.first_response_schema_validity)}, "
        f"медиана задачи {_sec(summary.duration_ms_median)}"
    )
    typer.echo(f"Отчёт: {json_path}, {markdown_path}")


def _print_ranking(reports: list[BenchReport]) -> None:
    for position, verdict in enumerate(select(reports), start=1):
        status = "проходит пороги" if verdict.eligible else "не проходит: " + "; ".join(verdict.reasons)
        typer.echo(f"{position}. {verdict.label} — {status}")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _sec(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} с"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
