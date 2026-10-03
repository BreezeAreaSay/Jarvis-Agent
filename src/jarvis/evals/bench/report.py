"""Отчёт бенчмарка: JSON (всё, что измерено) и Markdown (для человека), сравнение конфигураций и правило
выбора основной модели (07-evals-and-benchmarks.md §4)."""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, JsonValue

from jarvis.evals.bench.dataset import LETTERS
from jarvis.evals.bench.grading import Summary, TaskResult
from jarvis.evals.bench.server import Memory, Offload, ServerFacts, Speed

# Правило выбора (07 §4): пороги, которые основная модель обязана пройти.
MIN_VALID_AFTER_REPAIR = 0.98
MAX_STEP_LATENCY_MS = 6000.0
MIN_VRAM_HEADROOM_MIB = 800.0


class ServerReport(BaseModel):
    command: list[str]
    model_sha256: str | None = None  # файла, который загрузил сервер: репозиторий модели может обновиться
    cold_start_s: float | None
    facts: ServerFacts
    offload: Offload
    memory: Memory
    speed: Speed
    speed_medians: dict[str, float | None]


class BenchReport(BaseModel):
    label: str
    started_at: datetime
    finished_at: datetime
    jarvis_version: str
    dataset: dict[str, JsonValue]  # путь, sha256, число задач
    host: dict[str, JsonValue]
    candidate: (
        dict[str, JsonValue] | None
    )  # конфигурация модели и сервера (без кандидата — модель из конфига)
    server_build: str | None  # сборка сервера, если её сообщает /props
    server: ServerReport | None
    context_window: int | None
    summary: Summary
    tasks: list[TaskResult]
    notes: list[str] = []

    @property
    def context_used(self) -> float | None:
        if not self.context_window:
            return None
        return round(self.summary.max_prompt_tokens / self.context_window, 3)


def write_report(report: BenchReport, folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "report.json"
    markdown_path = folder / "report.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def load_report(path: Path) -> BenchReport:
    return BenchReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _num(value: float | int | None, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}" if isinstance(value, float) else f"{value}{unit}"


def _sec(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} с"


def render_markdown(report: BenchReport) -> str:
    summary = report.summary
    lines = [
        f"# Бенчмарк агента: {report.label}",
        "",
        f"{report.started_at:%Y-%m-%d %H:%M} · Jarvis {report.jarvis_version} · датасет "
        f"`{report.dataset.get('path')}` ({report.dataset.get('tasks')} задач, sha256 "
        f"{str(report.dataset.get('sha256'))[:12]}…)",
        "",
        f"Хост: {report.host.get('platform')}, {report.host.get('processor')}, "
        f"{report.host.get('cpu_physical')} ядер / {report.host.get('cpu_count')} потоков, "
        f"RAM {report.host.get('ram_gib')} ГиБ.",
        "",
    ]
    if report.candidate is not None:
        candidate = report.candidate
        lines += [
            "## Конфигурация",
            "",
            "| Параметр | Значение |",
            "| --- | --- |",
            f"| Класс | {candidate.get('klass')} |",
            f"| Модель | {candidate.get('model') or candidate.get('hf')} |",
            f"| Квантование | {candidate.get('quant')} |",
            f"| Контекст | {candidate.get('ctx')} |",
            f"| GPU layers | {candidate.get('gpu_layers')} |",
            f"| Threads | {candidate.get('threads') or 'по умолчанию'} |",
            f"| Batch / ubatch | {candidate.get('batch')} / {candidate.get('ubatch')} |",
            f"| Flash attention | {candidate.get('flash_attn')} |",
            f"| KV-кэш | {candidate.get('cache_type_k')} / {candidate.get('cache_type_v')} |",
            f"| MoE на CPU | {'все' if candidate.get('cpu_moe') else candidate.get('n_cpu_moe') or 'нет'} |",
            f"| Сэмплирование | temperature={candidate.get('temperature')}, seed={candidate.get('seed')} |",
            f"| extra_body | `{json.dumps(candidate.get('extra_body'), ensure_ascii=False)}` |",
            "",
        ]
    if report.server is not None:
        lines += _server_section(report.server)
    elif report.server_build:
        lines += [f"Сервер: {report.server_build} (запущен вручную, замеры сервера не делались).", ""]
    lines += [
        "## Итог",
        "",
        "| Метрика | Значение |",
        "| --- | --- |",
        f"| **Task success rate** | **{_pct(summary.success_rate)}** ({summary.tasks} задач) |",
        f"| Tool selection accuracy | {_pct(summary.tool_selection_accuracy)} |",
        f"| Argument accuracy | {_pct(summary.argument_accuracy)} |",
        f"| Answer accuracy | {_pct(summary.answer_accuracy)} |",
        f"| First-response schema validity | {_pct(summary.first_response_schema_validity)} |",
        f"| Repair rate | {_pct(summary.repair_rate)} |",
        f"| Valid after repair | {_pct(summary.valid_after_repair)} |",
        f"| Multi-step success rate | {_pct(summary.multistep_success_rate)} |",
        f"| Russian/English mixed success rate | {_pct(summary.mixed_language_success_rate)} |",
        f"| Injection safety | {_pct(summary.injection_safety_rate)} |",
        f"| Task duration: mean / median | {_sec(summary.duration_ms_mean)} / "
        f"{_sec(summary.duration_ms_median)} |",
        f"| Model time per agent step (median) | {_sec(summary.step_latency_ms_median)} |",
        f"| First model response (median) | {_sec(summary.first_response_ms_median)} |",
        f"| Model calls / tool calls / steps per task | {summary.model_calls_mean} / "
        f"{summary.tool_calls_mean} / "
        f"{summary.steps_mean} |",
        f"| Tokens: prompt / completion | {summary.prompt_tokens} / {summary.completion_tokens} |",
        f"| Context used (max prompt) | {summary.max_prompt_tokens} из {_num(report.context_window)} "
        f"({_pct(report.context_used)}) |",
        f"| Timeouts | {summary.timeouts} |",
        "",
        "### По категориям",
        "",
        "| Категория | Успех |",
        "| --- | --- |",
        *(f"| {name} | {_pct(value)} |" for name, value in summary.by_category.items()),
        "",
        "## Задачи",
        "",
        "| Задача | Кат. | ✓ | 1-й инструмент | Шаги | Вызовы модели | Ремонт | Время | Почему нет |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in report.tasks:
        why = "; ".join(result.failures)[:200].replace("|", "/")
        lines.append(
            f"| {result.id} | {LETTERS[result.category]} | {'✓' if result.success else '✗'} | "
            f"{result.first_tool or '—'} | {result.steps} | {result.model_calls} | "
            f"{result.repair_attempts} | "
            f"{_sec(result.duration_ms)} | {why} |"
        )
    if report.notes:
        lines += ["", "## Заметки", "", *(f"- {note}" for note in report.notes)]
    return "\n".join(lines) + "\n"


def _server_section(server: ServerReport) -> list[str]:
    facts, offload, memory, speed = server.facts, server.offload, server.memory, server.speed_medians
    verdict = {
        "full": "все слои на GPU",
        "partial": "частично на GPU",
        "cpu_only": "CPU — GPU не используется",
    }
    devices = "; ".join(
        f"{device.name}: {device.description} ({device.total_mib} MiB, свободно {device.free_mib})"
        for device in facts.devices
    )
    thinking = "—" if facts.thinking is None else "включены" if facts.thinking else "выключены"
    mark = "✓" if offload.as_expected else "✗ НЕ ТАК, КАК ОЖИДАЛОСЬ"
    lines = [
        "## Сервер и GPU offload",
        "",
        f"Сборка llama.cpp: {facts.build or '—'}",
        "",
        f"Устройства: {devices or '—'}",
        "",
        f"**Offload: {verdict[offload.verdict]}** (ожидалось {offload.expected}) {mark}. "
        f"Слоёв на GPU: {offload.offloaded_layers or '—'}; буферы на GPU {offload.gpu_buffers_mib:.0f} MiB, "
        f"в RAM {offload.cpu_buffers_mib:.0f} MiB.",
    ]
    if offload.reasons:
        lines.append("Почему: " + "; ".join(offload.reasons) + ".")
    lines += [
        "",
        "| Параметр | Значение |",
        "| --- | --- |",
        f"| Модель | {facts.model_name or '—'}, {facts.model_params or '—'} параметров, "
        f"слоёв {facts.n_layer or '—'}"
        f"{f', экспертов {facts.n_expert}' if facts.n_expert else ''} |",
        f"| Файл | {facts.file_type or '—'}, {facts.file_size or '—'} |",
        f"| GGUF | `{facts.model_file or '—'}` |",
        f"| sha256 | `{server.model_sha256 or '—'}` |",
        f"| Контекст / batch / ubatch | {facts.n_ctx} / {facts.n_batch} / {facts.n_ubatch} |",
        f"| Flash attention | {facts.flash_attn or '—'} |",
        f"| KV-кэш | {facts.kv_types or '—'} |",
        f"| Потоки | {facts.n_threads or '—'} |",
        f"| «Размышления» шаблона | {thinking} |",
        f"| Холодный старт (до /health) | {_num(server.cold_start_s, ' с', 1)} |",
        f"| RAM сервера: после загрузки / пик | {_num(memory.rss_loaded_mib, ' MiB')} / "
        f"{_num(memory.rss_peak_mib, ' MiB')}"
        f" (private: {_num(memory.private_loaded_mib, ' MiB')} / {_num(memory.private_peak_mib, ' MiB')}) |",
        f"| VRAM процесса (Windows): выделенная / shared | {_vram(memory, 'process_dedicated_mib')} / "
        f"{_vram(memory, 'process_shared_mib')} |",
        f"| VRAM адаптера: до загрузки / после / после прогона | {_adapter(memory)} |",
        f"| Запас VRAM | {_num(memory.vram_headroom_mib, ' MiB')} |",
        f"| Обработка промпта | {_num(speed.get('prompt_tokens_per_s'), ' ток/с', 1)} |",
        f"| Генерация | {_num(speed.get('generation_tokens_per_s'), ' ток/с', 1)} |",
        f"| Время до первого токена 1k / 4k / 8k | {_num(speed.get('ttft_ms_1k'), ' мс')} / "
        f"{_num(speed.get('ttft_ms_4k'), ' мс')} / {_num(speed.get('ttft_ms_8k'), ' мс')} |",
        f"| Тёплый запрос со схемой | {_num(speed.get('warm_latency_ms'), ' мс')} |",
        "",
    ]
    if facts.memory_breakdown:
        lines += ["Память по устройствам (лог сервера):", "", "```", *facts.memory_breakdown, "```", ""]
    if facts.warnings:
        lines += ["Предупреждения сервера:", "", *(f"- `{warning}`" for warning in facts.warnings[:10]), ""]
    lines += ["Команда сервера:", "", "```", " ".join(server.command), "```", ""]
    return lines


def _vram(memory: Memory, key: str) -> str:
    sample = memory.vram_loaded
    value = getattr(sample, key) if sample is not None else None
    return _num(value, " MiB")


def _adapter(memory: Memory) -> str:
    values = [
        sample.adapter_dedicated_mib if sample is not None else None
        for sample in (memory.vram_before, memory.vram_loaded, memory.vram_after_run)
    ]
    return " / ".join(_num(value, " MiB") for value in values)


# --- сравнение


class Verdict(BaseModel, frozen=True):
    label: str
    eligible: bool
    reasons: list[str]


def select(reports: Sequence[BenchReport]) -> list[Verdict]:
    """Правило выбора: пороги (structured output после ремонта, задержка шага, запас VRAM), затем —
    наибольшая доля успешных задач. Порядок результата — лучшие первыми."""
    verdicts: list[tuple[float, Verdict]] = []
    for report in reports:
        summary = report.summary
        reasons: list[str] = []
        if summary.valid_after_repair is not None and summary.valid_after_repair < MIN_VALID_AFTER_REPAIR:
            reasons.append(f"structured output после ремонта {_pct(summary.valid_after_repair)} < 98 %")
        if (
            summary.step_latency_ms_median is not None
            and summary.step_latency_ms_median > MAX_STEP_LATENCY_MS
        ):
            reasons.append(f"шаг агента {_sec(summary.step_latency_ms_median)} > 6 с")
        headroom = report.server.memory.vram_headroom_mib if report.server else None
        if headroom is not None and headroom < MIN_VRAM_HEADROOM_MIB:
            reasons.append(f"запас VRAM {headroom:.0f} MiB < 800")
        spill = report.server.memory.shared_spill_mib() if report.server else None
        if spill is not None:
            reasons.append(f"драйвер вынес {spill:.0f} MiB в общую память GPU")
        if report.server is not None and not report.server.offload.as_expected:
            reasons.append(
                f"offload {report.server.offload.verdict}, ожидался {report.server.offload.expected}"
            )
        verdicts.append(
            (summary.success_rate, Verdict(label=report.label, eligible=not reasons, reasons=reasons))
        )
    verdicts.sort(key=lambda item: (not item[1].eligible, -item[0]))
    return [verdict for _, verdict in verdicts]


def render_comparison(reports: Sequence[BenchReport]) -> str:
    def server_value(report: BenchReport, key: str) -> float | None:
        return report.server.speed_medians.get(key) if report.server else None

    lines = [
        "# Сравнение конфигураций",
        "",
        "| Конфигурация | Класс | Квант | Успех | Инструмент | Аргументы | Схема с 1-й | После ремонта | "
        "Multi-step | RU/EN | Инъекции | Шаг (медиана) | Задача (медиана) | Генерация | Промпт | VRAM | "
        "RAM | Старт | Offload |",
        "|" + " --- |" * 19,
    ]
    for report in reports:
        summary = report.summary
        candidate = report.candidate or {}
        server = report.server
        vram = (
            server.memory.vram_loaded.adapter_dedicated_mib if server and server.memory.vram_loaded else None
        )
        if vram is None and server is not None:
            vram = server.offload.gpu_buffers_mib
        lines.append(
            f"| {report.label} | {candidate.get('klass', '—')} | {candidate.get('quant', '—')} | "
            f"**{_pct(summary.success_rate)}** | {_pct(summary.tool_selection_accuracy)} | "
            f"{_pct(summary.argument_accuracy)} | {_pct(summary.first_response_schema_validity)} | "
            f"{_pct(summary.valid_after_repair)} | {_pct(summary.multistep_success_rate)} | "
            f"{_pct(summary.mixed_language_success_rate)} | {_pct(summary.injection_safety_rate)} | "
            f"{_sec(summary.step_latency_ms_median)} | {_sec(summary.duration_ms_median)} | "
            f"{_num(server_value(report, 'generation_tokens_per_s'), ' т/с', 1)} | "
            f"{_num(server_value(report, 'prompt_tokens_per_s'), ' т/с', 0)} | {_num(vram, ' MiB')} | "
            f"{_num(server.memory.rss_peak_mib if server else None, ' MiB')} | "
            f"{_num(server.cold_start_s if server else None, ' с', 1)} | "
            f"{server.offload.verdict if server else '—'} |"
        )
    lines += ["", "## Правило выбора (07 §4)", ""]
    lines += [
        "Пороги: structured output после ремонта ≥ 98 %, медианное время модели на шаг ≤ 6 с, "
        "запас VRAM ≥ 800 MiB, offload как ожидался; среди прошедших — наибольшая доля успешных задач.",
        "",
    ]
    for position, verdict in enumerate(select(reports), start=1):
        status = "проходит" if verdict.eligible else "не проходит: " + "; ".join(verdict.reasons)
        lines.append(f"{position}. **{verdict.label}** — {status}")
    return "\n".join(lines) + "\n"
