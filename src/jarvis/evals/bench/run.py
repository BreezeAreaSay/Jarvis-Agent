"""Прогоны бенчмарка: эталон (проверка датасета), модель из конфига пользователя, кандидат с сервером."""

import asyncio
import hashlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx
from pydantic import JsonValue

from jarvis.app.composition import model_backends
from jarvis.domain.errors import ConfigError, ModelError
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.bench.agent import TASK_TIMEOUT_S, reference_model, run_dataset
from jarvis.evals.bench.dataset import Dataset
from jarvis.evals.bench.grading import TaskResult, summarize
from jarvis.evals.bench.report import BenchReport, ServerReport
from jarvis.evals.bench.server import (
    MIB,
    BenchServerError,
    Candidate,
    Memory,
    Speed,
    describe_host,
    dump,
    file_sha256,
    measure_speed,
    memory_snapshot,
    offload_verdict,
    parse_server_log,
    prefetch,
    running_server,
    server_args,
    vram_headroom,
    vram_sample,
)

OnResult = Callable[[TaskResult], None]


def dataset_info(dataset: Dataset, path: Path) -> dict[str, JsonValue]:
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tasks": len(dataset.tasks),
    }


def candidate_config(candidate: Candidate) -> JarvisConfig:
    """Конфиг Jarvis для кандидата: его эндпоинт назначен роли executor, остальное — по умолчанию."""
    sampling: dict[str, JsonValue] = {
        "temperature": candidate.temperature,
        "top_p": candidate.top_p,
        "seed": candidate.seed,
    }
    return JarvisConfig.model_validate(
        {
            "models": {
                "endpoints": {
                    "bench": {
                        "base_url": f"{candidate.base_url}/v1",
                        "model": candidate.id,
                        "request_timeout_s": 600,
                        "capabilities": {
                            "structured_output": candidate.structured_output,
                            "context_window": candidate.ctx,
                        },
                        "sampling": sampling,
                        "extra_body": candidate.extra_body,
                    }
                },
                "roles": {"executor": "bench"},
            }
        }
    )


def _report(
    *,
    label: str,
    started: datetime,
    dataset: Dataset,
    dataset_path: Path,
    results: list[TaskResult],
    candidate: Candidate | None = None,
    server: ServerReport | None = None,
    server_build: str | None = None,
    context_window: int | None = None,
    notes: list[str] | None = None,
) -> BenchReport:
    candidate_data = dump(candidate) if candidate is not None else None
    return BenchReport(
        label=label,
        started_at=started,
        finished_at=datetime.now(UTC),
        jarvis_version=version("jarvis-agent"),
        dataset=dataset_info(dataset, dataset_path),
        host=describe_host(),
        candidate=candidate_data if isinstance(candidate_data, dict) else None,
        server_build=server_build,
        server=server,
        context_window=context_window,
        summary=summarize(results),
        tasks=results,
        notes=notes or [],
    )


async def bench_reference(
    dataset: Dataset, dataset_path: Path, *, on_result: OnResult | None = None
) -> BenchReport:
    """Эталонные решения на scripted-модели: датасет и оценщик исправны, если успех — 100 %."""
    started = datetime.now(UTC)
    results = await run_dataset(dataset, reference_model, on_result=on_result)
    return _report(
        label="reference", started=started, dataset=dataset, dataset_path=dataset_path, results=results
    )


async def bench_configured(
    dataset: Dataset,
    dataset_path: Path,
    config: JarvisConfig,
    *,
    label: str,
    real_home: Path | None,
    real_config: Path | None = None,
    task_timeout_s: float = TASK_TIMEOUT_S,
    on_result: OnResult | None = None,
) -> BenchReport:
    """Модель из конфига пользователя (сервер запущен им самим); бюджеты и политика — по умолчанию."""
    backends = model_backends(config)
    backend = backends.get(ModelRole.EXECUTOR)
    if backend is None:
        raise ConfigError('роли executor не назначена модель: [models.roles] executor = "<эндпоинт>"')
    notes = [
        "сервер модели запущен вне бенчмарка: его параметры, offload, память и скорость в отчёт не попали "
        "(полный отчёт — `jarvis bench run`)"
    ]
    try:
        status = await backend.describe()
        build = status.server
    except ModelError as exc:
        raise ConfigError(f"сервер модели недоступен: {exc.message}") from None
    started = datetime.now(UTC)
    results = await run_dataset(
        dataset,
        lambda _task, _workspace: backend,
        config=JarvisConfig(models=config.models),
        task_timeout_s=task_timeout_s,
        real_home=real_home,
        real_config=real_config,
        on_result=on_result,
    )
    return _report(
        label=label,
        started=started,
        dataset=dataset,
        dataset_path=dataset_path,
        results=results,
        server_build=build,
        context_window=backend.info.capabilities.context_window,
        notes=notes,
    )


def bench_candidate(
    candidate: Candidate,
    dataset: Dataset,
    dataset_path: Path,
    folder: Path,
    *,
    repeats: int = 3,
    fetch: bool = True,
    real_home: Path | None = None,
    real_config: Path | None = None,
    task_timeout_s: float = TASK_TIMEOUT_S,
    on_result: OnResult | None = None,
    say: Callable[[str], None] = print,
    allow_cpu_only: bool = False,
) -> BenchReport:
    """Кандидат целиком: сервер с явными параметрами → offload, память, скорость → датасет → отчёт.

    Сервер, который не использует GPU, останавливает прогон кандидата (если не `allow_cpu_only`): это
    почти всегда не та сборка llama.cpp, а датасет на CPU шёл бы часами и мерил бы не то.
    """
    started = datetime.now(UTC)
    notes: list[str] = []
    if candidate.hf is not None and fetch:
        say(f"{candidate.id}: загрузка модели {candidate.hf}, если её нет в кэше (ход — в prefetch.log)…")
        prefetch(candidate, folder / "prefetch.log")
    vram_before = vram_sample(None)
    say(f"{candidate.id}: запуск llama-server…")
    with running_server(candidate, folder / "server.log") as server:
        facts = server.facts()
        offload = offload_verdict(facts, candidate)
        say(
            f"{candidate.id}: сервер готов за {server.cold_start_s:.1f} с; offload: {offload.verdict} "
            f"(слоёв на GPU {offload.offloaded_layers or '—'}, на GPU {offload.gpu_buffers_mib:.0f} MiB)"
        )
        if offload.verdict == "cpu_only" and not allow_cpu_only:
            raise BenchServerError(
                f"{candidate.id}: сервер работает без GPU — {'; '.join(offload.reasons)}. Нужна сборка "
                "llama.cpp с Vulkan (benchmarks/README.md); прогнать на CPU всё равно — --allow-cpu-only"
            )
        if not offload.as_expected:
            notes.append(
                f"offload {offload.verdict}, ожидался {offload.expected}: {'; '.join(offload.reasons)}"
            )
            say(f"{candidate.id}: ВНИМАНИЕ — offload не такой, как ожидался: {'; '.join(offload.reasons)}")
        rss_loaded, private_loaded = memory_snapshot(server)
        vram_loaded = vram_sample(server.process.pid)
        spill = Memory(vram_loaded=vram_loaded).spill_mib(offload.gpu_buffers_mib)
        if spill is not None:
            notes.append(
                f"после загрузки {spill:.0f} MiB буферов GPU нет в выделенной памяти: VRAM не хватило"
            )
            say(f"{candidate.id}: ВНИМАНИЕ — {spill:.0f} MiB буферов GPU вне VRAM: драйвер вынес часть в RAM")
        if sys.platform == "win32" and vram_loaded is None:
            notes.append("счётчики памяти GPU Windows недоступны: VRAM и запас — по логу сервера")
        say(f"{candidate.id}: замер скорости ({repeats} повтора)…")
        speed = _measure_speed_with_retry(
            candidate,
            repeats=repeats,
            server_alive=lambda: server.process.poll() is None,
            notes=notes,
            say=say,
        )
        say(f"{candidate.id}: датасет, {len(dataset.tasks)} задач…")
        config = candidate_config(candidate)
        backend = model_backends(config)[ModelRole.EXECUTOR]
        results = asyncio.run(
            run_dataset(
                dataset,
                lambda _task, _workspace: backend,
                config=config,
                task_timeout_s=task_timeout_s,
                real_home=real_home,
                real_config=real_config,
                on_result=on_result,
            )
        )
        vram_after = vram_sample(server.process.pid)
        memory_snapshot(server)  # последний замер — в пик
        cold_start_s = server.cold_start_s
        peak_rss, peak_private = server.watch.peak_rss, server.watch.peak_private
    # Лог — после остановки: таблицу памяти по устройствам сервер печатает при выходе (на Windows её нет:
    # там llama-server останавливается без неё).
    facts = parse_server_log((folder / "server.log").read_text(encoding="utf-8", errors="replace"))
    model_file = Path(facts.model_file) if facts.model_file else None
    model_sha256 = None
    if model_file is not None and model_file.is_file():
        say(f"{candidate.id}: sha256 файла модели…")
        model_sha256 = file_sha256(model_file)
    memory = Memory(
        rss_loaded_mib=rss_loaded,
        private_loaded_mib=private_loaded,
        rss_peak_mib=round(peak_rss / MIB, 1) or None,
        private_peak_mib=round(peak_private / MIB, 1) or None,
        vram_before=vram_before,
        vram_loaded=vram_loaded,
        vram_after_run=vram_after,
        vram_headroom_mib=vram_headroom(facts.devices, offload.gpu_buffers_mib, [vram_loaded, vram_after]),
    )
    server_report = ServerReport(
        command=[candidate.server, *server_args(candidate)],
        model_sha256=model_sha256,
        cold_start_s=round(cold_start_s, 2),
        facts=facts,
        offload=offload,
        memory=memory,
        speed=speed,
        speed_medians=speed.medians(),
    )
    return _report(
        label=candidate.id,
        started=started,
        dataset=dataset,
        dataset_path=dataset_path,
        results=results,
        candidate=candidate,
        server=server_report,
        context_window=candidate.ctx,
        notes=notes,
    )


def _measure_speed_with_retry(
    candidate: Candidate,
    *,
    repeats: int,
    server_alive: Callable[[], bool],
    notes: list[str],
    say: Callable[[str], None],
) -> Speed:
    """Повторить весь speed probe после transient HTTP disconnect.

    На Windows llama-server иногда закрывает длинный HTTP response без данных, хотя процесс и
    `/health` остаются живы. Повторяем только в этом безопасном случае: если процесс завершился,
    сохраняем исходную ошибку запуска/падения вместо того, чтобы маскировать неисправность.
    """
    try:
        return measure_speed(candidate, repeats=repeats)
    except httpx.HTTPError as first_error:
        if not server_alive():
            raise BenchServerError(
                f"{candidate.id}: сервер не ответил на замер скорости: {first_error!r}"
            ) from None
        notes.append("первый speed probe завершился HTTP-разрывом; повтор выполнен при живом llama-server")
        say(f"{candidate.id}: HTTP-разрыв на speed probe; сервер жив, повторяю замер…")
        try:
            return measure_speed(candidate, repeats=repeats)
        except httpx.HTTPError as second_error:
            raise BenchServerError(
                f"{candidate.id}: сервер не ответил на замер скорости после повтора: "
                f"первая ошибка {first_error!r}, вторая {second_error!r}"
            ) from None
