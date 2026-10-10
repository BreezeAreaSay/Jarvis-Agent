"""Журнал запросов: одна JSON-строка на запрос в <data>\\journal\\YYYY-MM-DD.jsonl и `jarvis stats`.

Запись — в фоновом daemon-потоке: write() только кладёт запись в очередь, горячий путь не ждёт диска.
Поля записи: ts, text (только если [journal] store_text), mode, level, reason, tool, args, ok, error,
cancelled и тайминги в мс: hotkey_to_window, route, grammar (до начала действия), hands {cache_n, prompt_n,
prompt_ms, predicted_ms, predicted_per_second, total_ms}, exec, brain {first_token, total, new_thread, model,
usage}, total.
"""

import json
import logging
import math
import queue
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pc import settings

log = logging.getLogger("jarvis")

# бюджеты AGENTS.md, мс
BUDGET_GRAMMAR_MS = 300.0
BUDGET_HANDS_MS = 1200.0
BUDGET_BRAIN_FIRST_MS = 3000.0
BUDGET_HOTKEY_MS = 100.0
# тёплый запрос рук: в промпте только сообщение пользователя; больше — кэш префикса сломан
PROMPT_N_WARN = 100.0

LEVEL_NAMES = {"grammar": "грамматика", "hands": "руки", "brain": "GPT", "local": "локально"}


def journal_dir() -> Path:
    return settings.data_dir() / "journal"


def _now_ts() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _day_of(record: dict[str, Any]) -> date:
    try:
        return datetime.fromisoformat(str(record.get("ts", ""))).date()
    except ValueError:
        return date.today()


class Journal:
    """Фоновая запись журнала. Поток стартует при первой записи; close() дописывает очередь."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        """Положить запись в очередь (микросекунды); ts добавляется, если его нет."""
        rec = dict(record)
        rec.setdefault("ts", _now_ts())
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="jarvis-journal", daemon=True)
                self._thread.start()
        self._queue.put(rec)

    def close(self, timeout: float = 5.0) -> None:
        """Дописать всё из очереди и остановить поток."""
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            rec = self._queue.get()
            if rec is None:
                return
            self._save(rec)

    @staticmethod
    def _save(rec: dict[str, Any]) -> None:
        try:
            from jarvis import config

            if not config.load().journal.store_text:
                rec.pop("text", None)
            path = journal_dir() / f"{_day_of(rec).isoformat()}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(rec, ensure_ascii=False, default=str)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except Exception as e:  # журнал не должен ронять Jarvis
            log.warning("журнал: запись не удалась: %s", e)


# --- чтение и статистика ------------------------------------------------------------------------


def read(days: int = 7, today: date | None = None) -> list[dict[str, Any]]:
    """Записи за последние days дней (сегодня включительно), от старых к новым; битые строки пропускаются."""
    day = today or date.today()
    records: list[dict[str, Any]] = []
    for back in range(max(days, 1) - 1, -1, -1):
        path = journal_dir() / f"{(day - timedelta(days=back)).isoformat()}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def percentile(values: list[float], p: float) -> float | None:
    """Перцентиль по ближайшему рангу; пусто — None."""
    vals = sorted(v for v in values if isinstance(v, int | float) and not isinstance(v, bool))
    if not vals:
        return None
    rank = max(1, math.ceil(p / 100 * len(vals)))
    return float(vals[rank - 1])


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _nums(records: list[dict[str, Any]], *path: str) -> list[float]:
    out = []
    for rec in records:
        value: Any = rec
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        n = _num(value)
        if n is not None:
            out.append(n)
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _is_error(rec: dict[str, Any]) -> bool:
    return rec.get("ok") is False and not rec.get("cancelled")


def _budget(name: str, values: list[float], limit: float) -> dict[str, Any]:
    p95 = percentile(values, 95)
    return {"name": name, "p95": p95, "limit": limit, "n": len(values), "ok": p95 is None or p95 <= limit}


def stats(days: int = 7, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Сводка журнала: по уровням, руки, мозг и нарушения бюджетов AGENTS.md."""
    recs = read(days) if records is None else records
    levels: dict[str, dict[str, Any]] = {}
    for name in ("grammar", "hands", "brain", "local"):
        group = [r for r in recs if r.get("level") == name]
        if not group:
            continue
        totals = _nums(group, "total")
        levels[name] = {
            "count": len(group),
            "p50": percentile(totals, 50),
            "p95": percentile(totals, 95),
            "errors": _share(sum(_is_error(r) for r in group), len(group)),
            "ask_gpt": _share(
                sum(str(r.get("reason", "")).startswith("hands→ask_gpt") for r in group), len(group)
            ),
        }
    # всё, что дошло до рук: сами руки плюс ушедшее от них дальше
    via_hands = [
        r for r in recs if r.get("level") == "hands" or str(r.get("reason", "")).startswith("hands→")
    ]
    hands_recs = [r for r in recs if isinstance(r.get("hands"), dict)]
    brain_recs = [r for r in recs if r.get("level") == "brain" and isinstance(r.get("brain"), dict)]
    first_new = _nums([r for r in brain_recs if r["brain"].get("new_thread") is True], "brain", "first_token")
    first_rep = _nums(
        [r for r in brain_recs if r["brain"].get("new_thread") is not True], "brain", "first_token"
    )
    # грамматика: до начала действия (в старых записях — время роутера)
    grammar_ms = [
        v
        for r in recs
        if r.get("level") == "grammar"
        for v in [_num(r.get("grammar", r.get("route")))]
        if v is not None
    ]
    hands_total = _nums(hands_recs, "hands", "total_ms")
    return {
        "days": days,
        "count": len(recs),
        "first_ts": str(recs[0].get("ts", "")) if recs else "",
        "last_ts": str(recs[-1].get("ts", "")) if recs else "",
        "levels": levels,
        "hands_ask_gpt": _share(
            sum(str(r.get("reason", "")).startswith("hands→ask_gpt") for r in via_hands), len(via_hands)
        ),
        "hands_error": _share(
            sum(str(r.get("reason", "")).startswith("hands→error") for r in via_hands), len(via_hands)
        ),
        "hands": {
            "count": len(hands_recs),
            "prompt_n_avg": _mean(_nums(hands_recs, "hands", "prompt_n")),
            "tps_avg": _mean(_nums(hands_recs, "hands", "predicted_per_second")),
            "total_ms_p50": percentile(hands_total, 50),
            "total_ms_p95": percentile(hands_total, 95),
        },
        "brain": {
            "first_token_p95_new": percentile(first_new, 95),
            "first_token_p95_repeat": percentile(first_rep, 95),
            "n_new": len(first_new),
            "n_repeat": len(first_rep),
        },
        "budgets": [
            _budget("грамматика: до начала действия", grammar_ms, BUDGET_GRAMMAR_MS),
            _budget("руки: total_ms", hands_total, BUDGET_HANDS_MS),
            _budget("GPT: до первых слов, новый тред", first_new, BUDGET_BRAIN_FIRST_MS),
            _budget("GPT: до первых слов, повторные", first_rep, BUDGET_BRAIN_FIRST_MS),
            _budget("хоткей → окно", _nums(recs, "hotkey_to_window"), BUDGET_HOTKEY_MS),
        ],
    }


def fmt_ms(value: float | None) -> str:
    """240 мс / 1,5 с; нет данных — «—»."""
    if value is None:
        return "—"
    if value < 1:
        return f"{value:.2f} мс".replace(".", ",")
    if value < 1000:
        return f"{value:.0f} мс" if value >= 10 else f"{value:.1f} мс".replace(".", ",")
    return f"{value / 1000:.1f} с".replace(".", ",")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def format_stats(st: dict[str, Any], min_tokens_per_s: float | None = None) -> str:
    """Текст для `jarvis stats`; нарушения бюджетов и тревожные средние помечены «⚠»."""
    if not st["count"]:
        return f"Журнал за {st['days']} дн. пуст: запросов не было (пишет только `jarvis run`)."
    lines = [
        f"Журнал за {st['days']} дн.: {st['count']} запросов ({st['first_ts'][:10]} … {st['last_ts'][:10]})",
        "",
    ]
    head = ("уровень", "кол-во", "p50 total", "p95 total", "ошибки", "hands→ask_gpt")
    lines.append(f"{head[0]:<11} {head[1]:>6} {head[2]:>10} {head[3]:>10} {head[4]:>7} {head[5]:>14}")
    for name, lv in st["levels"].items():
        ask_gpt = _pct(lv["ask_gpt"]) if name in ("brain", "local") else "—"
        p50, p95, errors = fmt_ms(lv["p50"]), fmt_ms(lv["p95"]), _pct(lv["errors"])
        name_ru = LEVEL_NAMES.get(name, name)
        lines.append(f"{name_ru:<11} {lv['count']:>6} {p50:>10} {p95:>10} {errors:>7} {ask_gpt:>14}")
    lines.append("")
    lines.append(
        f"Дошло до рук и ушло дальше: hands→ask_gpt {_pct(st['hands_ask_gpt'])}, "
        f"hands→error {_pct(st['hands_error'])}"
    )
    h = st["hands"]
    if h["count"]:
        prompt_n, tps = h["prompt_n_avg"], h["tps_avg"]
        cache_warn = "⚠ " if prompt_n is not None and prompt_n >= PROMPT_N_WARN else ""
        tps_warn = "⚠ " if tps is not None and min_tokens_per_s and tps < min_tokens_per_s else ""
        lines.append(f"Руки ({h['count']}):")
        lines.append(
            f"  {cache_warn}средний prompt_n {prompt_n:.0f} (вырос — кэш префикса сломался)"
            if prompt_n is not None
            else "  средний prompt_n —"
        )
        floor = f", порог {min_tokens_per_s:g}" if min_tokens_per_s else ""
        lines.append(
            f"  {tps_warn}средняя скорость {tps:.0f} т/с (упала — VRAM переполнена{floor})"
            if tps is not None
            else "  средняя скорость —"
        )
        lines.append(f"  total_ms p50 {fmt_ms(h['total_ms_p50'])}, p95 {fmt_ms(h['total_ms_p95'])}")
    b = st["brain"]
    if b["n_new"] or b["n_repeat"]:
        lines.append("GPT, p95 до первых слов:")
        lines.append(f"  новый тред {fmt_ms(b['first_token_p95_new'])} (n={b['n_new']})")
        lines.append(f"  повторные  {fmt_ms(b['first_token_p95_repeat'])} (n={b['n_repeat']})")
    lines.append("")
    lines.append("Бюджеты AGENTS.md (p95):")
    for bud in st["budgets"]:
        if not bud["n"]:
            lines.append(f"  · {bud['name']} ≤ {fmt_ms(bud['limit'])}: нет данных")
            continue
        mark = "✓" if bud["ok"] else "⚠"
        lines.append(f"  {mark} {bud['name']} ≤ {fmt_ms(bud['limit'])}: {fmt_ms(bud['p95'])} (n={bud['n']})")
    return "\n".join(lines)
