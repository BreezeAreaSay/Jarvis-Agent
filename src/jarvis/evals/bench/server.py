"""Сервер модели для бенчмарка: запуск llama.cpp `llama-server` с явными параметрами кандидата, проверка
GPU offload, память и скорость (07-evals-and-benchmarks.md §4).

Это код бенчмарка, а не продукта: здесь можно знать про llama.cpp (лог, `/completion`, тайминги).
Ядро Jarvis об этом не знает — агент ходит к серверу через тот же адаптер, что и в `jarvis run`.

Что доказывает работу на GPU (а не «нет ошибок — значит, работает»):
- лог загрузки: устройство (`Vulkan0: AMD Radeon RX 7600`), `offloaded N/M layers to GPU`, буферы модели,
  KV-кэша и вычислений на GPU, таблица памяти по устройствам;
- счётчики Windows (классы WMI `GPUProcessMemory`, `GPUAdapterMemory`): выделенная и общая память GPU
  процесса сервера и его адаптера — до загрузки, после и после прогона; буферы на GPU по логу, которых
  нет в выделенной памяти процесса, — признак, что модель не поместилась и драйвер унёс часть в RAM;
- скорость генерации: на CPU она в разы ниже.
"""

import hashlib
import json
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import psutil
from pydantic import BaseModel, Field, JsonValue, model_validator

HEALTH_POLL_S = 0.25
MIB = 1024 * 1024
# Буферы на GPU по логу, которых нет в выделенной памяти процесса: больше этого — вытеснение в RAM.
SPILL_MIB = 512.0
PREFETCH_TIMEOUT_S = 6 * 3600.0  # загрузка модели с Hugging Face: десятки гигабайт


class BenchServerError(Exception):
    pass


class Candidate(BaseModel, frozen=True, extra="forbid"):
    """Конфигурация «модель + сервер». Всё модельно-специфичное — здесь, а не в ядре."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    klass: Literal["fast", "balanced", "max"] = Field(alias="class")
    description: str = ""
    model: str | None = None  # путь к GGUF
    hf: str | None = None  # или репозиторий Hugging Face: "user/repo:Q4_K_M" (llama-server -hf)
    quant: str  # для отчёта: Q4_K_M, Q5_K_M …
    server: str  # путь к llama-server
    host: str = "127.0.0.1"
    port: int = 8090
    ctx: int = 16384
    gpu_layers: int | Literal["all", "auto"] = "all"
    threads: int | None = None
    batch: int = 2048
    ubatch: int = 512
    flash_attn: Literal["on", "off", "auto"] = "on"
    cache_type_k: str | None = "q8_0"
    cache_type_v: str | None = "q8_0"
    parallel: int = 1
    cpu_moe: bool = False  # все эксперты MoE — на CPU (--cpu-moe)
    n_cpu_moe: int | None = None  # эксперты первых N слоёв — на CPU (--n-cpu-moe)
    expect_offload: Literal["full", "partial"] = "full"
    extra_args: list[str] = []
    startup_timeout_s: float = 900.0
    # Запросы агента: сэмплирование и особенности шаблона чата (как [models.endpoints.*] в конфиге).
    temperature: float | None = 0.0
    top_p: float | None = None
    seed: int | None = 42
    extra_body: dict[str, JsonValue] = {}
    structured_output: bool = True

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _source(self) -> Self:
        if (self.model is None) == (self.hf is None):
            raise ValueError(f"{self.id}: укажите ровно одно — model (путь к GGUF) или hf (репозиторий)")
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class CandidatesFile(BaseModel, frozen=True, extra="forbid"):
    defaults: dict[str, JsonValue] = {}
    candidates: list[dict[str, JsonValue]] = Field(min_length=1)

    def resolve(self) -> list[Candidate]:
        return [Candidate.model_validate({**self.defaults, **item}) for item in self.candidates]


def server_args(candidate: Candidate) -> list[str]:
    """Аргументы llama-server: всё явно, `--fit off` — сервер ничего не подгоняет сам."""
    source = ["-m", candidate.model] if candidate.model is not None else ["-hf", str(candidate.hf)]
    args = [
        *source,
        "--host", candidate.host,
        "--port", str(candidate.port),
        "-c", str(candidate.ctx),
        "-ngl", str(candidate.gpu_layers),
        "-b", str(candidate.batch),
        "-ub", str(candidate.ubatch),
        "-fa", candidate.flash_attn,
        "-np", str(candidate.parallel),
        "--fit", "off",
        "--jinja",
        "-lv", "4",  # с этим уровнем лог показывает устройства, offload и буферы памяти
    ]  # fmt: skip
    if candidate.threads is not None:
        args += ["-t", str(candidate.threads)]
    if candidate.cache_type_k:
        args += ["-ctk", candidate.cache_type_k]
    if candidate.cache_type_v:
        args += ["-ctv", candidate.cache_type_v]
    if candidate.cpu_moe:
        args.append("--cpu-moe")
    if candidate.n_cpu_moe is not None:
        args += ["--n-cpu-moe", str(candidate.n_cpu_moe)]
    return [*args, *candidate.extra_args]


# --- разбор лога загрузки


class Device(BaseModel, frozen=True):
    name: str  # "Vulkan0", "CPU"
    description: str  # "AMD Radeon RX 7600"
    total_mib: int
    free_mib: int


class ServerFacts(BaseModel):
    build: str | None = None
    devices: list[Device] = []
    system_info: str | None = None
    n_threads: int | None = None
    model_file: str | None = None  # файл GGUF, который загрузил сервер (при -hf — из кэша)
    model_name: str | None = None
    file_type: str | None = None
    file_size: str | None = None
    model_params: str | None = None
    n_layer: int | None = None
    n_expert: int | None = None
    offloaded_layers: int | None = None
    total_layers: int | None = None
    buffers_mib: dict[str, dict[str, float]] = {}  # устройство → вид (model, kv, rs, compute, output) → MiB
    n_ctx: int | None = None
    n_batch: int | None = None
    n_ubatch: int | None = None
    flash_attn: str | None = None
    kv_types: str | None = None
    thinking: bool | None = None
    memory_breakdown: list[str] = []  # таблица памяти по устройствам, как её печатает сервер
    warnings: list[str] = []


_PATTERNS: dict[str, re.Pattern[str]] = {
    "build": re.compile(r"common_params_print_info: build (.+)$"),
    "device": re.compile(r"common_param:\s+- (\S+)\s*:\s*(.+?)\s*\((\d+) MiB, (\d+) MiB free\)"),
    "system_info": re.compile(r"system_info: (.+)$"),
    "threads": re.compile(r"system_info: n_threads = (\d+)"),
    "model_file": re.compile(
        r"llama_model_loader: loaded meta data with .+? tensors from (.+?) \(version GGUF"
    ),
    "model_name": re.compile(r"print_info: general\.name\s+= (.+)$"),
    "file_type": re.compile(r"print_info: file type\s+= (.+)$"),
    "file_size": re.compile(r"print_info: file size\s+= (.+)$"),
    "model_params": re.compile(r"print_info: model params\s+= (.+)$"),
    "n_layer": re.compile(r"print_info: n_layer\s+= (\d+)"),
    "n_expert": re.compile(r"print_info: n_expert\s+= (\d+)"),
    "offloaded": re.compile(r"offloaded (\d+)/(\d+) layers to GPU"),
    "buffer": re.compile(r"(\S+)\s+(model|KV|RS|compute|output) buffer size =\s+([\d.]+) MiB"),
    "n_ctx": re.compile(r"llama_context: n_ctx\s+= (\d+)"),
    "n_batch": re.compile(r"llama_context: n_batch\s+= (\d+)"),
    "n_ubatch": re.compile(r"llama_context: n_ubatch\s+= (\d+)"),
    "flash_attn": re.compile(r"llama_context: flash_attn\s+= (\S+)"),
    "kv_types": re.compile(r"llama_kv_cache: size = .*?(K \(\S+\).*)$"),
    "thinking": re.compile(r"chat template, thinking = (\d)"),
    "breakdown": re.compile(r"common_memory_breakdown_print: (\|.*)$"),
}
_WARNINGS = re.compile(
    r"no usable GPU|option will be ignored|failed to allocate|out of memory|falling back", re.I
)


def parse_server_log(text: str) -> ServerFacts:
    facts = ServerFacts()
    breakdown_done = False
    for line in text.splitlines():
        if (match := _PATTERNS["build"].search(line)) and facts.build is None:
            facts.build = match.group(1).strip()
        if match := _PATTERNS["device"].search(line):
            facts.devices.append(
                Device(
                    name=match.group(1),
                    description=match.group(2),
                    total_mib=int(match.group(3)),
                    free_mib=int(match.group(4)),
                )
            )
        if (match := _PATTERNS["system_info"].search(line)) and facts.system_info is None:
            facts.system_info = match.group(1)[:300]
            if threads := _PATTERNS["threads"].search(line):
                facts.n_threads = int(threads.group(1))
        for key in (
            "model_file", "model_name", "file_type", "file_size", "model_params", "flash_attn", "kv_types"
        ):  # fmt: skip
            if (match := _PATTERNS[key].search(line)) and getattr(facts, key) is None:
                setattr(facts, key, match.group(1).strip())
        for key in ("n_layer", "n_expert", "n_ctx", "n_batch", "n_ubatch"):
            if (match := _PATTERNS[key].search(line)) and getattr(facts, key) is None:
                setattr(facts, key, int(match.group(1)))
        if match := _PATTERNS["offloaded"].search(line):
            facts.offloaded_layers, facts.total_layers = int(match.group(1)), int(match.group(2))
        if match := _PATTERNS["buffer"].search(line):
            device, kind = match.group(1), match.group(2).lower()
            facts.buffers_mib.setdefault(device, {})[kind] = float(match.group(3))
        if (match := _PATTERNS["thinking"].search(line)) and facts.thinking is None:
            facts.thinking = match.group(1) == "1"
        if (match := _PATTERNS["breakdown"].search(line)) and not breakdown_done:
            facts.memory_breakdown.append(match.group(1).rstrip())
        elif facts.memory_breakdown and not breakdown_done:
            breakdown_done = True  # только первая таблица
        if _WARNINGS.search(line):
            facts.warnings.append(line.strip()[:300])
    return facts


def is_gpu(device: str) -> bool:
    return not device.upper().startswith("CPU") and "_HOST" not in device.upper()


class Offload(BaseModel, frozen=True):
    verdict: Literal["full", "partial", "cpu_only"]
    expected: Literal["full", "partial"]
    as_expected: bool
    gpu_devices: list[str]
    offloaded_layers: str | None  # "37/37"
    gpu_buffers_mib: float
    cpu_buffers_mib: float
    reasons: list[str]


def offload_verdict(facts: ServerFacts, candidate: Candidate) -> Offload:
    gpu = [f"{device.name}: {device.description}" for device in facts.devices if is_gpu(device.name)]
    gpu_mib = sum(sum(kinds.values()) for name, kinds in facts.buffers_mib.items() if is_gpu(name))
    cpu_mib = sum(sum(kinds.values()) for name, kinds in facts.buffers_mib.items() if not is_gpu(name))
    layers = f"{facts.offloaded_layers}/{facts.total_layers}" if facts.offloaded_layers is not None else None
    reasons: list[str] = []
    if not gpu:
        reasons.append("сервер не видит GPU (в device_info только CPU)")
    if gpu_mib == 0:
        reasons.append("на GPU нет ни одного буфера")
    if any("no usable GPU" in warning for warning in facts.warnings):
        reasons.append("сервер сообщил: no usable GPU")
    if reasons:
        verdict: Literal["full", "partial", "cpu_only"] = "cpu_only"
    elif facts.offloaded_layers is not None and facts.offloaded_layers < (facts.total_layers or 0):
        verdict = "partial"
        reasons.append(f"на GPU {layers} слоёв")
    elif candidate.cpu_moe or candidate.n_cpu_moe:
        verdict = "partial"
        reasons.append(f"эксперты MoE на CPU: {cpu_mib:.0f} MiB весов в RAM")
    else:
        verdict = "full"
    return Offload(
        verdict=verdict,
        expected=candidate.expect_offload,
        as_expected=verdict == candidate.expect_offload,
        gpu_devices=gpu,
        offloaded_layers=layers,
        gpu_buffers_mib=round(gpu_mib, 1),
        cpu_buffers_mib=round(cpu_mib, 1),
        reasons=reasons,
    )


# --- память процесса и GPU


class VramSample(BaseModel, frozen=True):
    source: str  # "windows-wmi" | "amdgpu-sysfs"
    adapter: str | None = None  # LUID адаптера Windows, к которому относятся значения
    process_dedicated_mib: float | None = None
    process_shared_mib: float | None = None
    adapter_dedicated_mib: float | None = None


def vram_sample(pid: int | None) -> VramSample | None:
    """Выделенная память GPU по счётчикам Windows; на Linux с amdgpu — занятая VRAM устройства."""
    if sys.platform == "win32":
        return _windows_vram(pid)
    used = _amdgpu_vram_used()
    return VramSample(source="amdgpu-sysfs", adapter_dedicated_mib=used) if used is not None else None


# Классы WMI счётчиков GPU: в отличие от путей Get-Counter, их имена не переводятся на язык Windows.
_WMI_SCRIPT = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$p = @(Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUProcessMemory -ErrorAction "
    "SilentlyContinue | Select-Object Name, DedicatedUsage, SharedUsage); "
    "$a = @(Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUAdapterMemory -ErrorAction "
    "SilentlyContinue | Select-Object Name, DedicatedUsage, SharedUsage); "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @{process = $p; adapter = $a}"
)
_LUID = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", re.I)


def _windows_vram(pid: int | None) -> VramSample | None:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WMI_SCRIPT],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_windows_gpu_memory(result.stdout.decode("utf-8", errors="replace"), pid)


def parse_windows_gpu_memory(text: str, pid: int | None) -> VramSample | None:
    """Память GPU из классов WMI: процесс сервера (по всем его записям на одном адаптере) и тот же адаптер.
    Без процесса — адаптер с наибольшей выделенной памятью (дискретный GPU, а не встроенный)."""
    try:
        data = json.loads(text.strip() or "null")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    process: dict[str, list[float]] = {}  # LUID → [выделенная, общая]
    adapters: dict[str, float] = {}
    for kind, target in (("process", None), ("adapter", adapters)):
        for item in _items(data.get(kind)):
            name = str(item.get("Name") or "")
            luid = _LUID.search(name)
            dedicated, shared = _bytes(item.get("DedicatedUsage")), _bytes(item.get("SharedUsage"))
            if luid is None or dedicated is None:
                continue
            key = luid.group(1).lower()
            if target is not None:
                target[key] = target.get(key, 0.0) + dedicated
            elif pid is not None and name.startswith(f"pid_{pid}_"):
                totals = process.setdefault(key, [0.0, 0.0])
                totals[0] += dedicated
                totals[1] += shared or 0.0
    if process:
        adapter = max(process, key=lambda key: process[key][0])
    elif adapters:
        adapter = max(adapters, key=lambda key: adapters[key])
    else:
        return None
    dedicated_process, shared_process = process.get(adapter, [None, None])
    return VramSample(
        source="windows-wmi",
        adapter=adapter,
        process_dedicated_mib=round(dedicated_process / MIB, 1) if dedicated_process is not None else None,
        process_shared_mib=round(shared_process / MIB, 1) if shared_process is not None else None,
        adapter_dedicated_mib=round(adapters[adapter] / MIB, 1) if adapter in adapters else None,
    )


def _items(value: object) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, dict)]  # pyright: ignore[reportUnknownVariableType]


def _bytes(value: object) -> float | None:
    try:
        return float(str(value))
    except ValueError:
        return None


def _amdgpu_vram_used() -> float | None:
    total = 0
    found = False
    for path in Path("/sys/class/drm").glob("card*/device/mem_info_vram_used"):
        try:
            total += int(path.read_text().strip())
            found = True
        except (OSError, ValueError):
            continue
    return round(total / MIB, 1) if found else None


@dataclass
class _RssWatch:
    process: psutil.Process
    peak_rss: int = 0
    peak_private: int = 0
    stop: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(1.0)

    def sample(self) -> tuple[int, int | None]:
        try:
            info = self.process.memory_info()
        except psutil.Error:
            return 0, None
        private = getattr(info, "private", None)  # Windows: память, которую процесс занял сам
        self.peak_rss = max(self.peak_rss, info.rss)
        if private is not None:
            self.peak_private = max(self.peak_private, private)
        return info.rss, private


class Memory(BaseModel):
    rss_loaded_mib: float | None = None
    private_loaded_mib: float | None = None
    rss_peak_mib: float | None = None
    private_peak_mib: float | None = None
    vram_before: VramSample | None = None
    vram_loaded: VramSample | None = None
    vram_after_run: VramSample | None = None
    vram_headroom_mib: float | None = None  # сколько VRAM осталось свободным (правило выбора: ≥ 800)

    def spill_mib(self, gpu_buffers_mib: float) -> float | None:
        """Сколько буферов сервера на GPU (по логу) не нашлось в выделенной памяти процесса (счётчик Windows),
        если больше порога: драйвер вынес их в RAM — модель «на GPU», но работает медленно. Общая память
        процесса сама по себе не признак: в ней законно живут буферы хоста Vulkan и буфер загрузки весов."""
        samples = [sample for sample in (self.vram_loaded, self.vram_after_run) if sample]
        dedicated = [
            sample.process_dedicated_mib for sample in samples if sample.process_dedicated_mib is not None
        ]
        if not dedicated or gpu_buffers_mib <= 0:
            return None
        missing = gpu_buffers_mib - min(dedicated)
        return round(missing, 1) if missing > SPILL_MIB else None


def vram_headroom(
    devices: list[Device], gpu_buffers_mib: float, samples: list[VramSample | None]
) -> float | None:
    """Свободная VRAM при работе сервера: по счётчику ОС (занятое всеми программами), иначе — из лога."""
    gpu = [device for device in devices if is_gpu(device.name)]
    if not gpu:
        return None
    used = [sample.adapter_dedicated_mib for sample in samples if sample and sample.adapter_dedicated_mib]
    if used:  # счётчик ОС: всё, что занято на адаптере (и другими программами)
        return round(gpu[0].total_mib - max(used), 1)
    return round(gpu[0].free_mib - gpu_buffers_mib, 1)  # лог: свободно до загрузки минус буферы сервера


# --- запуск и замеры


class Speed(BaseModel):
    prompt_tokens: int | None = None
    prompt_tokens_per_s: list[float] = []
    generated_tokens: int | None = None
    generation_tokens_per_s: list[float] = []
    ttft_ms: dict[str, list[float]] = {}  # размер промпта → время до первого токена (поток, без кэша)
    ttft_prompt_tokens: dict[str, int] = {}  # размер промпта → сколько токенов он занял у этой модели
    warm_latency_ms: list[float] = []  # короткий запрос со схемой, как у агента

    def medians(self) -> dict[str, float | None]:
        def median(values: Sequence[float]) -> float | None:
            return round(statistics.median(values), 1) if values else None

        result = {
            "prompt_tokens_per_s": median(self.prompt_tokens_per_s),
            "generation_tokens_per_s": median(self.generation_tokens_per_s),
            "warm_latency_ms": median(self.warm_latency_ms),
        }
        result.update({f"ttft_ms_{size}": median(values) for size, values in self.ttft_ms.items()})
        return result


@dataclass
class RunningServer:
    candidate: Candidate
    process: subprocess.Popen[bytes]
    log_path: Path
    cold_start_s: float
    watch: _RssWatch

    def facts(self) -> ServerFacts:
        return parse_server_log(self.log_path.read_text(encoding="utf-8", errors="replace"))


@contextmanager
def running_server(
    candidate: Candidate, log_path: Path, *, startup_timeout_s: float | None = None
) -> Iterator[RunningServer]:
    """Запустить сервер, дождаться /health; при выходе — остановить, даже при ошибке или Ctrl+C."""
    _ensure_port_free(candidate)
    command = [candidate.server, *server_args(candidate)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        started = time.perf_counter()
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        watch = _RssWatch(psutil.Process(process.pid))
        thread = threading.Thread(target=watch.run, daemon=True)
        try:
            _wait_healthy(candidate, process, log_path, startup_timeout_s or candidate.startup_timeout_s)
            server = RunningServer(candidate, process, log_path, time.perf_counter() - started, watch)
            thread.start()
            yield server
        finally:
            watch.stop.set()
            _stop(process)


def _ensure_port_free(candidate: Candidate) -> None:
    """Порт свободен: иначе /health ответил бы старый сервер (на Windows llama-server делит порт с ним)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex((candidate.host, candidate.port)) == 0:
            raise BenchServerError(
                f"{candidate.id}: порт {candidate.host}:{candidate.port} занят — остановите прежний "
                "llama-server или задайте другой port в файле кандидатов"
            )


def _wait_healthy(
    candidate: Candidate, process: subprocess.Popen[bytes], log_path: Path, timeout_s: float
) -> None:
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise BenchServerError(
                    f"{candidate.id}: llama-server завершился с кодом {process.returncode}:\n{tail}"
                )
            try:
                if client.get(f"{candidate.base_url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(HEALTH_POLL_S)
    raise BenchServerError(f"{candidate.id}: сервер не ответил на /health за {timeout_s:g} с")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(16 * MIB):
            digest.update(chunk)
    return digest.hexdigest()


def prefetch(candidate: Candidate, log_path: Path) -> None:
    """Модель с Hugging Face скачивается первым запуском (до /health — пока идёт загрузка); холодный старт
    меряется вторым."""
    with running_server(candidate, log_path, startup_timeout_s=PREFETCH_TIMEOUT_S):
        pass


PROMPT_SIZES = {"1k": 1000, "4k": 4000, "8k": 8000}


FILLER_LINE_TOKENS = 36  # строка заполнителя в токенизаторе Qwen2 (сверено через /tokenize)


def filler(tokens: int) -> str:
    """Текст примерно на `tokens` токенов: русский, английский, пути и числа — как в работе агента. У других
    токенизаторов размер другой — точное число токенов отчёт берёт из таймингов сервера."""
    line = "Строка {n}: файл C:/projects/gofra/src/module_{n}.py изменён, status=ok, size={size} bytes.\n"
    return "".join(line.format(n=n, size=n * 37) for n in range(tokens // FILLER_LINE_TOKENS + 1))


def measure_speed(candidate: Candidate, *, repeats: int = 3) -> Speed:
    """Скорость сервера llama.cpp: обработка промпта, генерация, время до первого токена — без кэша."""
    speed = Speed()
    with httpx.Client(base_url=candidate.base_url, timeout=600.0, trust_env=False) as client:
        for _ in range(repeats):
            prompt = _completion(client, {"prompt": filler(2000), "n_predict": 1, "cache_prompt": False})
            timings = _mapping(prompt.get("timings"))
            speed.prompt_tokens = _int(timings.get("prompt_n"))
            if (value := _float(timings.get("prompt_per_second"))) is not None:
                speed.prompt_tokens_per_s.append(round(value, 1))
            generated = _completion(
                client,
                {
                    "prompt": "Перечисли числа от 1 до 300 через запятую:",
                    "n_predict": 256,
                    "ignore_eos": True,
                    "cache_prompt": False,
                    "temperature": 0,
                },
            )
            timings = _mapping(generated.get("timings"))
            speed.generated_tokens = _int(timings.get("predicted_n"))
            if (value := _float(timings.get("predicted_per_second"))) is not None:
                speed.generation_tokens_per_s.append(round(value, 1))
            for size, tokens in PROMPT_SIZES.items():
                if tokens + 300 > candidate.ctx:
                    continue
                try:
                    elapsed, prompt_tokens = _ttft(client, filler(tokens))
                except httpx.HTTPStatusError:  # у этого токенизатора промпт вышел длиннее окна
                    continue
                speed.ttft_ms.setdefault(size, []).append(elapsed)
                if prompt_tokens is not None:
                    speed.ttft_prompt_tokens[size] = prompt_tokens
            speed.warm_latency_ms.append(_warm_request(client, candidate))
    return speed


def _completion(client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/completion", json=body)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _ttft(client: httpx.Client, prompt: str) -> tuple[float, int | None]:
    """Время до первого токена в потоке и настоящий размер промпта (из таймингов последнего сообщения)."""
    started = time.perf_counter()
    first: float | None = None
    prompt_tokens: int | None = None
    body = {"prompt": prompt, "n_predict": 8, "cache_prompt": False, "stream": True}
    with client.stream("POST", "/completion", json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            if first is None and '"content"' in line:
                first = round((time.perf_counter() - started) * 1000, 1)
            if '"timings"' in line:
                try:
                    timings = _mapping(_mapping(json.loads(line[5:])).get("timings"))
                except ValueError:
                    continue
                prompt_tokens = _int(timings.get("prompt_n"))
    return first if first is not None else round((time.perf_counter() - started) * 1000, 1), prompt_tokens


def _warm_request(client: httpx.Client, candidate: Candidate) -> float:
    """Короткий запрос, похожий на шаг агента: чат и JSON Schema, тёплый сервер."""
    schema = {
        "type": "object",
        "properties": {"answer": {"enum": ["синий", "зелёный", "красный"]}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    body: dict[str, Any] = {
        **candidate.extra_body,
        "messages": [
            {"role": "system", "content": "Отвечай одним JSON-объектом."},
            {"role": "user", "content": "Какого цвета ясное небо днём?"},
        ],
        "max_tokens": 32,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {"name": "probe", "schema": schema}},
    }
    started = time.perf_counter()
    response = client.post("/v1/chat/completions", json=body)
    response.raise_for_status()
    return round((time.perf_counter() - started) * 1000, 1)


def memory_snapshot(server: RunningServer) -> tuple[float | None, float | None]:
    rss, private = server.watch.sample()
    return (round(rss / MIB, 1) if rss else None, round(private / MIB, 1) if private else None)


def describe_host() -> dict[str, JsonValue]:
    memory = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "processor": _processor(),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "ram_gib": round(memory.total / 1024**3, 1),
        "python": platform.python_version(),
    }


def _processor() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def dump(model: BaseModel) -> JsonValue:
    return json.loads(model.model_dump_json())
