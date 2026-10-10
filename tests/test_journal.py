"""Журнал: фоновая запись JSONL, store_text, чтение по дням, stats и бюджеты на синтетике."""

import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvis import journal
from jarvis.journal import Journal, format_stats, percentile, read, stats


def _lines(data: Path) -> list[dict[str, Any]]:
    path = data / "journal" / f"{date.today().isoformat()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_write_one_json_line_per_request(_isolated_data: Path) -> None:
    j = Journal()
    j.write({"text": "открой «отчёт.docx»", "level": "grammar", "ok": True, "hands": {"prompt_n": 12}})
    j.write({"text": "громче", "level": "grammar", "ok": True})
    j.close()
    recs = _lines(_isolated_data)
    assert [r["text"] for r in recs] == ["открой «отчёт.docx»", "громче"]
    assert recs[0]["hands"] == {"prompt_n": 12} and recs[0]["ts"]
    raw = (_isolated_data / "journal" / f"{date.today().isoformat()}.jsonl").read_bytes()
    assert "отчёт".encode() in raw  # UTF-8, ensure_ascii=False


def test_store_text_false_drops_text(_isolated_data: Path, config_file: Any) -> None:
    config_file("[journal]\nstore_text = false\n")
    j = Journal()
    j.write({"text": "найди отчёт", "level": "grammar", "ok": True})
    j.close()
    recs = _lines(_isolated_data)
    assert "text" not in recs[0] and recs[0]["level"] == "grammar"


def test_write_is_background(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    seen: list[str] = []

    def slow_save(rec: dict[str, Any]) -> None:
        seen.append(threading.current_thread().name)
        started.set()
        release.wait(5)

    monkeypatch.setattr(Journal, "_save", staticmethod(slow_save))
    j = Journal()
    t = time.perf_counter()
    j.write({"level": "hands"})
    j.write({"level": "hands"})
    assert time.perf_counter() - t < 0.05  # не ждёт диска
    assert started.wait(2)
    release.set()
    j.close()
    assert seen == ["jarvis-journal", "jarvis-journal"]


def test_write_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, _isolated_data: Path) -> None:
    (_isolated_data / "journal").write_text("это файл, а не папка", encoding="utf-8")
    j = Journal()
    j.write({"level": "grammar"})
    j.close()  # не бросает


def test_read_days_and_broken_lines(_isolated_data: Path) -> None:
    folder = _isolated_data / "journal"
    folder.mkdir()
    today = date.today()
    for back, level in ((0, "grammar"), (3, "hands"), (10, "brain")):
        day = today - timedelta(days=back)
        (folder / f"{day.isoformat()}.jsonl").write_text(
            json.dumps({"ts": f"{day.isoformat()}T10:00:00", "level": level}) + "\n{битая строка\n\n",
            encoding="utf-8",
        )
    assert [r["level"] for r in read(7)] == ["hands", "grammar"]
    assert [r["level"] for r in read(30)] == ["brain", "hands", "grammar"]
    assert read(1) == [r for r in read(1) if r["level"] == "grammar"]


def test_percentile() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0 and percentile(values, 95) == 95.0
    assert percentile([7.0], 95) == 7.0 and percentile([], 50) is None
    assert percentile([3, None, True, 1], 50) == 1.0  # type: ignore[list-item]


def synthetic() -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    for i in range(20):  # грамматика 10..200 мс до действия; две ошибки
        ms = 10.0 * (i + 1)
        recs.append(
            {"ts": "2026-10-10T09:00:00", "level": "grammar", "ok": i > 1, "total": ms, "grammar": ms}
            | {"hotkey_to_window": 40.0 + i}
        )
    for i in range(10):  # руки: prompt_n 14, 40 т/с — медленно; total_ms 300..1100 и один 2000
        total = 2000.0 if i == 9 else 300.0 + 100 * i
        hands = {"prompt_n": 14, "cache_n": 1183, "predicted_per_second": 40.0, "total_ms": total}
        recs.append({"level": "hands", "ok": True, "total": total + 5, "reason": "hands", "hands": hands})
    recs.append({"level": "hands", "ok": False, "cancelled": True, "total": 50.0, "reason": "hands"})
    for i in range(4):  # мозг из рук; первые слова: новый тред 4 с, повторные ~1,5 с
        hands = {"prompt_n": 14, "predicted_per_second": 40.0, "total_ms": 400.0}
        brain = {"first_token": 4000.0 if i == 0 else 1500.0 + i, "new_thread": i == 0}
        recs.append(
            {"level": "brain", "ok": True, "total": 3000.0, "reason": "hands→ask_gpt"}
            | {"hands": hands, "brain": brain}
        )
    brain = {"first_token": 1200.0, "new_thread": False}
    recs.append({"level": "brain", "ok": True, "total": 2000.0, "reason": "heur:question", "brain": brain})
    recs.append({"level": "local", "ok": False, "total": 1.0, "reason": "local:needs_gpt"})
    recs.append({"level": "local", "ok": False, "total": 1.0, "reason": "hands→error"})
    return recs


def test_stats_on_synthetic_journal() -> None:
    st = stats(7, synthetic())
    g = st["levels"]["grammar"]
    assert g["count"] == 20 and g["p50"] == 100.0 and g["p95"] == 190.0 and g["errors"] == pytest.approx(0.1)
    h = st["levels"]["hands"]
    assert h["count"] == 11 and h["errors"] == 0.0  # отмена — не ошибка
    b = st["levels"]["brain"]
    assert b["count"] == 5 and b["ask_gpt"] == pytest.approx(0.8)
    assert st["levels"]["local"]["errors"] == 1.0
    # до рук дошли: 11 рук + 4 hands→ask_gpt + 1 hands→error
    assert st["hands_ask_gpt"] == pytest.approx(4 / 16) and st["hands_error"] == pytest.approx(1 / 16)
    assert st["hands"]["count"] == 14 and st["hands"]["prompt_n_avg"] == 14.0
    assert st["hands"]["tps_avg"] == 40.0 and st["hands"]["total_ms_p95"] == 2000.0
    assert st["brain"]["first_token_p95_new"] == 4000.0 and st["brain"]["n_new"] == 1
    assert st["brain"]["first_token_p95_repeat"] == 1503.0 and st["brain"]["n_repeat"] == 4
    budgets = {b["name"]: b for b in st["budgets"]}
    assert budgets["грамматика: до начала действия"]["ok"] is True
    assert budgets["руки: total_ms"]["ok"] is False
    assert budgets["GPT: до первых слов, новый тред"]["ok"] is False
    assert budgets["GPT: до первых слов, повторные"]["ok"] is True
    assert budgets["хоткей → окно"]["p95"] == 58.0 and budgets["хоткей → окно"]["ok"] is True


def test_format_stats_marks_violations() -> None:
    text = format_stats(stats(7, synthetic()), min_tokens_per_s=60.0)
    lines = text.splitlines()
    assert any(line.strip().startswith("⚠ руки: total_ms") for line in lines)
    assert any(line.strip().startswith("⚠ GPT: до первых слов, новый тред") for line in lines)
    assert any(line.strip().startswith("✓ грамматика") for line in lines)
    assert any(line.strip().startswith("✓ хоткей → окно") for line in lines)
    assert any("⚠ средняя скорость 40 т/с" in line for line in lines)  # ниже порога 60
    assert not any("⚠ средний prompt_n" in line for line in lines)  # 14 — кэш в порядке
    assert "грамматика" in text and "10 %" in text


def test_format_stats_prompt_n_growth_and_empty() -> None:
    recs = [{"level": "hands", "ok": True, "total": 900.0, "hands": {"prompt_n": 1200, "total_ms": 880.0}}]
    assert "⚠ средний prompt_n 1200" in format_stats(stats(7, recs))
    empty = format_stats(stats(3, []))
    assert "пуст" in empty


def test_stats_reads_files(_isolated_data: Path) -> None:
    j = Journal()
    for total in (100.0, 200.0):
        j.write({"level": "grammar", "ok": True, "total": total, "grammar": total})
    j.close()
    st = stats(1)
    assert st["count"] == 2 and st["levels"]["grammar"]["p95"] == 200.0


def test_fmt_ms() -> None:
    assert journal.fmt_ms(None) == "—"
    assert journal.fmt_ms(0.42) == "0,42 мс"
    assert journal.fmt_ms(4.25) == "4,2 мс"
    assert journal.fmt_ms(240.4) == "240 мс"
    assert journal.fmt_ms(1500) == "1,5 с"
