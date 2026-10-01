"""Отчёт eval: JSON для машин и Markdown для людей (07-evals-and-benchmarks.md §1)."""

from datetime import datetime
from pathlib import Path

from jarvis.evals.engine import EvalReport

_USAGE_COLUMNS = ("steps", "tool_calls", "model_calls", "replans", "failures")


def write_report(report: EvalReport, directory: Path) -> tuple[Path, Path]:
    """Пишет `<дата>-<режим>.json` и `.md`; возвращает пути."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.started_at:%Y%m%d-%H%M%S}-{report.mode}"
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: EvalReport) -> str:
    passed = sum(result.passed for result in report.results)
    lines = [
        f"# Eval {report.mode}: {_format_time(report.started_at)}",
        "",
        f"Прошли {passed} из {len(report.results)} сценариев.",
        "",
        "| Сценарий | Итог | Статус | "
        + " | ".join(_USAGE_COLUMNS)
        + " | Активное время, с | Длительность, мс |",
        "| --- | --- | --- | " + " | ".join("---" for _ in _USAGE_COLUMNS) + " | --- | --- |",
    ]
    for result in report.results:
        usage = [str(getattr(result.usage, column)) for column in _USAGE_COLUMNS]
        lines.append(
            f"| `{result.id}` | {'✓' if result.passed else '✗'} | {result.status} | "
            + " | ".join(usage)
            + f" | {result.usage.active_time_s:.3f} | {result.duration_ms} |"
        )
    failed = [result for result in report.results if not result.passed]
    if failed:
        lines += ["", "## Проблемы", ""]
        for result in failed:
            lines += [f"- `{result.id}`: {problem}" for problem in result.problems]
    return "\n".join(lines) + "\n"


def _format_time(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
