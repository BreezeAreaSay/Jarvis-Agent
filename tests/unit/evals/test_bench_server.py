"""Сервер кандидата и отчёт: аргументы llama-server, разбор лога, вердикт offload, правило выбора.

Лог CPU — настоящий (llama.cpp b92761a), логи Vulkan — синтетические, в формате тех же строк llama.cpp:
GPU в контейнере тестов нет (tests/fixtures/llama_server/README.md).
"""

import json
import socket
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.domain.models import ModelRole
from jarvis.evals.bench import run as bench_run
from jarvis.evals.bench.dataset import load_dataset
from jarvis.evals.bench.report import (
    BenchReport,
    ServerReport,
    load_report,
    render_comparison,
    render_markdown,
    select,
    write_report,
)
from jarvis.evals.bench.run import bench_reference, candidate_config
from jarvis.evals.bench.server import (
    BenchServerError,
    Candidate,
    CandidatesFile,
    Memory,
    Speed,
    VramSample,
    offload_verdict,
    parse_server_log,
    parse_windows_gpu_memory,
    running_server,
    server_args,
    vram_headroom,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "llama_server"
CPU_LOG = FIXTURES / "server_log_cpu.txt"
CANDIDATES = ROOT / "benchmarks" / "candidates.toml"
DATASET = ROOT / "benchmarks" / "agent" / "dataset.yaml"

VULKAN_FULL = (FIXTURES / "server_log_full_vulkan.txt").read_text(encoding="utf-8")
VULKAN_MOE = (FIXTURES / "server_log_moe_vulkan.txt").read_text(encoding="utf-8")
VULKAN_PARTIAL = (FIXTURES / "server_log_partial_vulkan.txt").read_text(encoding="utf-8")


def candidate(**fields: Any) -> Candidate:
    data: dict[str, Any] = {"id": "test-q4", "class": "balanced", "hf": "org/model-GGUF:Q4_K_M"}
    data.update({"quant": "Q4_K_M", "server": "llama-server"}, **fields)
    return Candidate.model_validate(data)


# --- кандидаты и аргументы сервера


def test_server_args_are_explicit() -> None:
    args = server_args(candidate(threads=6, extra_args=["--reasoning", "off"]))
    assert args[:2] == ["-hf", "org/model-GGUF:Q4_K_M"]
    joined = " ".join(args)
    for part in ("-c 16384", "-ngl all", "-fa on", "-np 1", "--fit off", "--jinja", "-t 6", "-ctk q8_0"):
        assert part in joined
    assert args[-2:] == ["--reasoning", "off"]
    moe = server_args(candidate(model="D:/m.gguf", hf=None, cpu_moe=True, n_cpu_moe=4))
    assert moe[:2] == ["-m", "D:/m.gguf"]
    assert "--cpu-moe" in moe
    assert moe[moe.index("--n-cpu-moe") + 1] == "4"


def test_candidate_needs_exactly_one_model_source() -> None:
    with pytest.raises(ValueError, match="ровно одно"):
        candidate(model="D:/m.gguf")
    with pytest.raises(ValueError, match="ровно одно"):
        candidate(hf=None)


def test_repository_candidates() -> None:
    """Файл кандидатов в репозитории: все классы, сравнение Q4 и Q5 на одном размере, MoE — частичный."""
    candidates = CandidatesFile.model_validate(
        tomllib.loads(CANDIDATES.read_text(encoding="utf-8"))
    ).resolve()
    ids = [item.id for item in candidates]
    assert len(set(ids)) == len(ids)
    assert {item.klass for item in candidates} == {"fast", "balanced", "max"}
    by_repo: dict[str, set[str]] = {}
    for item in candidates:
        by_repo.setdefault(str(item.hf).split(":")[0], set()).add(item.quant)
    assert any({"Q4_K_M", "Q5_K_M"} <= quants for quants in by_repo.values())
    for item in candidates:
        assert item.ctx >= 8192  # требование роли исполнителя
        assert "--fit" not in item.extra_args  # подгонку сервером отключает сам бенчмарк
        if item.klass == "max":
            assert item.cpu_moe
            assert item.expect_offload == "partial"


def test_candidate_config_points_the_executor_at_the_server() -> None:
    config = candidate_config(candidate(port=8123, ctx=12288, extra_body={"cache_prompt": True}))
    assert config.models.roles[ModelRole.EXECUTOR] == "bench"
    endpoint = config.models.endpoints["bench"]
    assert endpoint.base_url == "http://127.0.0.1:8123/v1"
    assert endpoint.capabilities.context_window == 12288
    assert endpoint.capabilities.structured_output
    assert endpoint.sampling.temperature == 0.0
    assert endpoint.extra_body == {"cache_prompt": True}


def test_speed_probe_retries_a_transient_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def flaky_speed(_candidate: Candidate, *, repeats: int) -> Speed:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError("server disconnected without sending a response")
        return Speed(generation_tokens_per_s=[42.0])

    monkeypatch.setattr(bench_run, "measure_speed", flaky_speed)
    notes: list[str] = []
    result = bench_run._measure_speed_with_retry(
        candidate(), repeats=3, server_alive=lambda: True, notes=notes, say=lambda _message: None
    )

    assert attempts == 2
    assert result.generation_tokens_per_s == [42.0]
    assert "повтор выполнен" in notes[0]


# --- лог сервера и offload


def test_cpu_log_is_cpu_only() -> None:
    facts = parse_server_log(CPU_LOG.read_text(encoding="utf-8"))
    assert "b92761a" in str(facts.build)
    assert facts.model_file == "/scratch/llm/tiny-qwen2.gguf"
    assert [device.name for device in facts.devices] == ["CPU"]
    assert facts.n_threads == 4
    assert facts.n_ctx == 8192
    assert facts.flash_attn == "enabled"
    assert facts.thinking is False
    assert facts.buffers_mib["CPU_Mapped"] == {"model": 37.41}
    assert len(facts.memory_breakdown) == 2
    assert any("no usable GPU" in warning for warning in facts.warnings)
    offload = offload_verdict(facts, candidate())
    assert offload.verdict == "cpu_only"
    assert not offload.as_expected
    assert offload.gpu_buffers_mib == 0
    assert "сервер не видит GPU (в device_info только CPU)" in offload.reasons


def test_vulkan_full_offload() -> None:
    facts = parse_server_log(VULKAN_FULL)
    offload = offload_verdict(facts, candidate())
    assert offload.verdict == "full"
    assert offload.as_expected
    assert offload.gpu_devices == ["Vulkan0: AMD Radeon RX 7600"]
    assert str(facts.model_file).endswith("\\Qwen3.5-9B-Q4_K_M.gguf")
    assert offload.offloaded_layers == "33/33"
    assert offload.gpu_buffers_mib == 6172.0
    assert offload.cpu_buffers_mib == 330.6  # эмбеддинги и буферы хоста — законно в RAM
    assert len(facts.memory_breakdown) == 3  # первая таблица, а не повтор при выходе
    assert vram_headroom(facts.devices, offload.gpu_buffers_mib, [None]) == 1168.0
    sample = VramSample(source="windows-wmi", adapter_dedicated_mib=7600.0)
    assert vram_headroom(facts.devices, offload.gpu_buffers_mib, [sample, None]) == 576.0


def test_moe_with_experts_on_cpu_is_partial_as_expected() -> None:
    offload = offload_verdict(parse_server_log(VULKAN_MOE), candidate(cpu_moe=True, expect_offload="partial"))
    assert offload.verdict == "partial"
    assert offload.as_expected
    assert offload.cpu_buffers_mib == 14800.0


def test_layers_left_on_cpu_are_not_full_offload() -> None:
    offload = offload_verdict(parse_server_log(VULKAN_PARTIAL), candidate())
    assert offload.verdict == "partial"
    assert not offload.as_expected
    assert offload.reasons == ["на GPU 20/33 слоёв"]


def test_spill_is_gpu_buffers_missing_from_dedicated_memory() -> None:
    def memory(dedicated: float, shared: float = 900.0) -> Memory:
        sample = VramSample(source="windows-wmi", process_dedicated_mib=dedicated, process_shared_mib=shared)
        return Memory(vram_loaded=sample)

    # общая память (буфер загрузки весов, буферы хоста Vulkan) — не вытеснение
    assert memory(6200.0).spill_mib(6172.0) is None
    assert memory(4100.0).spill_mib(6172.0) == 2072.0
    assert Memory().spill_mib(6172.0) is None  # счётчиков нет — не судим
    assert memory(0.0).spill_mib(0.0) is None  # на GPU ничего — это вердикт offload, не вытеснение


WMI = {
    "process": [
        {
            "Name": "pid_4242_luid_0x00000000_0x0000D1B0_phys_0",
            "DedicatedUsage": 6442450944,
            "SharedUsage": 943718400,
        },
        {"Name": "pid_4242_luid_0x00000000_0x0000C3A1_phys_0", "DedicatedUsage": 0, "SharedUsage": 1048576},
        {"Name": "pid_77_luid_0x00000000_0x0000D1B0_phys_0", "DedicatedUsage": 524288000, "SharedUsage": 0},
    ],
    "adapter": [
        {"Name": "luid_0x00000000_0x0000D1B0_phys_0", "DedicatedUsage": "7516192768", "SharedUsage": 0},
        {"Name": "luid_0x00000000_0x0000C3A1_phys_0", "DedicatedUsage": 134217728, "SharedUsage": 0},
    ],
}


def test_windows_gpu_memory_from_wmi() -> None:
    """Процесс сервера — по его записям на одном адаптере; адаптер — тот же (не сумма с встроенным GPU)."""
    sample = parse_windows_gpu_memory(json.dumps(WMI), 4242)
    assert sample == VramSample(
        source="windows-wmi",
        adapter="0x00000000_0x0000d1b0",
        process_dedicated_mib=6144.0,
        process_shared_mib=900.0,
        adapter_dedicated_mib=7168.0,
    )
    before = parse_windows_gpu_memory(json.dumps(WMI), None)  # до запуска: адаптер с наибольшей памятью
    assert before is not None
    assert before.adapter == "0x00000000_0x0000d1b0"
    assert before.process_dedicated_mib is None


@pytest.mark.parametrize("text", ["", "не JSON", "null", '{"process": null, "adapter": null}', "[]"])
def test_windows_gpu_memory_unavailable(text: str) -> None:
    assert parse_windows_gpu_memory(text, 4242) is None


def test_recurrent_state_buffers_count_as_gpu() -> None:
    """Qwen3.5: у слоёв с линейным вниманием — буфер состояния (RS), а не только KV."""
    rs_line = "0.00.500.001 I llama_memory_recurrent:    Vulkan0 RS buffer size =    50.25 MiB\n"
    log = VULKAN_FULL.replace("0.00.500.002 I sched_reserve:", rs_line + "0.00.500.002 I sched_reserve:")
    facts = parse_server_log(log)
    assert facts.buffers_mib["Vulkan0"]["rs"] == 50.25
    assert offload_verdict(facts, candidate()).gpu_buffers_mib == 6222.2


def test_busy_port_is_refused(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with (
            pytest.raises(BenchServerError, match="занят"),
            running_server(candidate(port=port, server="no-such-server"), tmp_path / "server.log"),
        ):
            pass


# --- отчёт и правило выбора


@pytest.fixture
async def report() -> BenchReport:
    dataset = load_dataset(DATASET).select(["a01_list_here", "d01_backend_config", "h01_framework"])
    return await bench_reference(dataset, DATASET)


def with_server(report: BenchReport, *, label: str, log: str = VULKAN_FULL, **memory: Any) -> BenchReport:
    facts = parse_server_log(log)
    cand = candidate(id=label)
    offload = offload_verdict(facts, cand)
    speed = Speed(prompt_tokens_per_s=[900.0, 1000.0], generation_tokens_per_s=[40.0, 42.0, 41.0])
    server = ServerReport(
        command=["llama-server", *server_args(cand)],
        cold_start_s=4.2,
        facts=facts,
        offload=offload,
        memory=Memory(**{"vram_headroom_mib": 1168.0, **memory}),
        speed=speed,
        speed_medians=speed.medians(),
    )
    data = {"label": label, "server": server, "candidate": cand.model_dump(mode="json")}
    return report.model_copy(update=data)


@pytest.mark.anyio
async def test_markdown_shows_configuration_offload_and_metrics(report: BenchReport) -> None:
    text = render_markdown(with_server(report, label="qwen-q4"))
    for part in (
        "| Квантование | Q4_K_M |",
        "Vulkan0: AMD Radeon RX 7600 (8176 MiB, свободно 7340)",
        "**Offload: все слои на GPU** (ожидалось full) ✓",
        "Слоёв на GPU: 33/33",
        "| Генерация | 41.0 ток/с |",
        "--fit off",
        "| **Task success rate** | **100.0 %** (3 задач) |",
        "| Multi-step success rate | 100.0 % |",
        "| Russian/English mixed success rate | 100.0 % |",
        "| H multistep | 100.0 % |",
        "| a01_list_here | A | ✓ |",
    ):
        assert part in text, part


@pytest.mark.anyio
async def test_selection_rule(report: BenchReport) -> None:
    best = with_server(report, label="best")
    slow_summary = report.summary.model_copy(update={"step_latency_ms_median": 9000.0, "success_rate": 1.0})
    slow = with_server(report, label="slow").model_copy(update={"summary": slow_summary})
    weaker = with_server(report, label="weaker").model_copy(
        update={"summary": report.summary.model_copy(update={"success_rate": 0.9})}
    )
    tight = with_server(report, label="tight", vram_headroom_mib=300.0)
    spilled = with_server(
        report,
        label="spilled",
        vram_loaded=VramSample(source="windows-wmi", process_dedicated_mib=4172.0, process_shared_mib=2000.0),
    )
    cpu = with_server(report, label="cpu", log=CPU_LOG.read_text(encoding="utf-8"))
    verdicts = select([weaker, slow, tight, best, spilled, cpu])
    assert [verdict.label for verdict in verdicts if verdict.eligible] == ["best", "weaker"]
    reasons = {verdict.label: " ".join(verdict.reasons) for verdict in verdicts}
    assert "шаг агента 9.0 с > 6 с" in reasons["slow"]
    assert "запас VRAM 300 MiB < 800" in reasons["tight"]
    assert "драйвер вынес 2000 MiB буферов GPU в RAM" in reasons["spilled"]
    assert "offload cpu_only" in reasons["cpu"]
    comparison = render_comparison([weaker, best])
    assert "1. **best** — проходит" in comparison
    assert "| best | balanced | Q4_K_M | **100.0 %** |" in comparison


@pytest.mark.anyio
async def test_report_round_trip(report: BenchReport, tmp_path: Path) -> None:
    original = with_server(report, label="qwen-q4")
    json_path, markdown_path = write_report(original, tmp_path / "run")
    assert markdown_path.read_text(encoding="utf-8").startswith("# Бенчмарк агента: qwen-q4")
    assert load_report(json_path) == original
