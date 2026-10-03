"""Бенчмарк с настоящим llama-server: только если заданы JARVIS_TEST_LLAMA_SERVER (путь к llama-server) и
JARVIS_TEST_GGUF (файл модели). В CI пропускается.

Проверяется оркестрация, а не ум модели: сервер запускается с параметрами кандидата и останавливается,
лог разобран (сборка, устройства, буферы), вердикт offload вынесен, скорость и память измерены, датасет
прошёл через настоящий адаптер, отчёт записан. Годится и для сборки CPU с крошечной моделью
(тогда вердикт — cpu_only), и для Vulkan-сборки на ПК.
"""

import os
from pathlib import Path

import pytest

from jarvis.evals.bench.dataset import load_dataset
from jarvis.evals.bench.report import render_markdown, write_report
from jarvis.evals.bench.run import bench_candidate
from jarvis.evals.bench.server import Candidate, file_sha256

SERVER = os.environ.get("JARVIS_TEST_LLAMA_SERVER")
MODEL = os.environ.get("JARVIS_TEST_GGUF")
DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "agent" / "dataset.yaml"

pytestmark = pytest.mark.skipif(
    not (SERVER and MODEL), reason="нет JARVIS_TEST_LLAMA_SERVER и JARVIS_TEST_GGUF: сервер не настроен"
)


def test_candidate_end_to_end(tmp_path: Path) -> None:
    candidate = Candidate.model_validate(
        {
            "id": "live",
            "class": "fast",
            "model": MODEL,
            "quant": "?",
            "server": SERVER,
            "port": 18931,
            "ctx": 8192,
            "cache_type_k": None,  # у крошечных моделей голова меньше блока q8_0
            "cache_type_v": None,
            "extra_args": ["--reasoning", "off", "--no-mmproj"],
        }
    )
    dataset = load_dataset(DATASET).select(["a02_cwd"])
    report = bench_candidate(
        candidate, dataset, DATASET, tmp_path, repeats=1, say=lambda _line: None, allow_cpu_only=True
    )
    server = report.server
    assert server is not None
    assert server.facts.build
    assert server.facts.devices
    assert server.facts.n_ctx == 8192
    assert server.facts.memory_breakdown  # таблица печатается при выходе: лог разобран после остановки
    assert server.offload.verdict in ("full", "partial", "cpu_only")
    assert server.speed_medians["generation_tokens_per_s"]
    assert server.memory.rss_peak_mib
    assert server.command[0] == SERVER
    assert server.model_sha256 == file_sha256(Path(str(MODEL)))
    assert report.tasks[0].model_calls >= 1
    _, markdown = write_report(report, tmp_path / "report")
    assert "## Сервер и GPU offload" in render_markdown(report)
    assert markdown.exists()
