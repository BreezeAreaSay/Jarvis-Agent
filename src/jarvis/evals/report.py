"""Отчёт eval: JSON для машин и Markdown для людей (07-evals-and-benchmarks.md §1)."""

from datetime import datetime
from pathlib import Path

from jarvis.evals.engine import EvalReport
from jarvis.evals.routing import RoutingReport

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
    for routing in report.routing:
        lines += ["", *render_routing(routing)]
    return "\n".join(lines) + "\n"


def render_routing(report: RoutingReport) -> list[str]:
    """Раздел набора фраз Router: главная метрика — ложные DIRECT (порог 0), затем полнота и задержка."""
    limits = report.thresholds
    lines = [
        f"## Router: `{report.id}` {'✓' if report.passed else '✗'}",
        "",
        f"Фраз {report.cases}, из них без DIRECT {report.negatives}, с DIRECT {report.direct_expected}.",
        "",
        "| Метрика | Значение | Порог |",
        "| --- | --- | --- |",
        f"| Ложные DIRECT | {report.false_direct} | 0 |",
        f"| DIRECT не того действия | {report.wrong_direct} | 0 |",
        f"| agent и clarify перепутаны | {report.wrong_strategy} | 0 |",
        f"| Полнота DIRECT | {report.direct_recall:.1%} ({report.direct_hit}/{report.direct_expected}) | "
        f"≥ {limits.min_direct_recall:.0%} |",
        f"| Решение Router p50 / p95 / max, мс | {report.p50_ms:.3f} / {report.p95_ms:.3f} / "
        f"{report.max_ms:.3f} | p95 ≤ {limits.max_p95_ms:g} |",
        "",
        "| Тег | Фраз | Верно |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {tag} | {stats.cases} | {stats.correct} |" for tag, stats in report.tags.items()]
    if report.failures:
        lines += ["", "| Фраза | Ожидалось | Решение | Вердикт |", "| --- | --- | --- | --- |"]
        for case in report.failures:
            expected = " ".join(
                str(part) for part in (case.expected, case.expected_intent, case.expected_entity) if part
            )
            actual = " ".join(
                str(part) for part in (case.actual, case.actual_intent, case.actual_entity) if part
            )
            text = case.text.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {text} | {expected} | {actual} | {case.verdict} |")
    if report.problems:
        lines += ["", *(f"- {problem}" for problem in report.problems)]
    return lines


def _format_time(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")
