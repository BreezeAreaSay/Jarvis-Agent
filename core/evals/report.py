"""Отчёт прогона: summary.json (для машин и базовой линии), report.md (людям) и journal.md у каждой
сохранённой попытки — шаги агента с ответами и картинками экрана.

report.md: итог одной строкой, таблица задач (успех как доля из N, шаги, время, токены, стоимость,
нарушения), разбор провалов (причины, нарушения, итог агента, последние шаги, ссылка на журнал) и сравнение
с базовой линией: что стало лучше, что хуже.
"""

import json
from pathlib import Path
from typing import Any

from evals.tasks.base import CATEGORIES

CHANGE = {"better": "лучше", "worse": "хуже", "same": "как было", "new": "новая", "missing": "нет в прогоне"}
MAX_RESULT = 1500


def _pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


def _money(cost: float) -> str:
    return f"${cost:.4f}" if cost else "—"


def _tokens(prompt: int, completion: int) -> str:
    if not prompt and not completion:
        return "—"
    return f"{prompt / 1000:.1f}k / {completion / 1000:.1f}k"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _model(meta: dict[str, Any]) -> str:
    if meta.get("mode") == "scripted":
        variant = f", ошибка «{meta['variant']}»" if meta.get("variant") else ""
        return f"детерминированный (модель по сценарию{variant})"
    return f"живой: {meta.get('provider')} / {meta.get('model')}"


def write_summary(summary: dict[str, Any], folder: Path) -> Path:
    path = folder / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render(summary: dict[str, Any], comparison: dict[str, Any] | None = None) -> str:
    meta, totals = summary["meta"], summary["totals"]
    lines = [
        f"# Проверка агента Jarvis — {meta.get('started', '')}",
        "",
        f"Режим: {_model(meta)} · картинки экрана: {meta.get('vision')} · повторов: "
        f"{meta.get('repeat', 1)} · задач: {totals['tasks']}",
        "",
        "## Итог",
        "",
        f"**Оценка {_pct(totals['score'])}** (средняя доля успеха по задачам) · задач без единого провала: "
        f"{totals['tasks_passed']} из {totals['tasks']} · попыток успешно: {totals['passed']} из "
        f"{totals['attempts']} · нарушений безопасности: {totals['violations']}",
        "",
        f"Шагов в среднем: {totals['steps']} · время попытки в среднем: {totals['seconds']} с · токены "
        f"(вход / выход): {_tokens(totals['prompt_tokens'], totals['completion_tokens'])} · стоимость: "
        f"{_money(totals['cost'])}",
        "",
    ]
    if totals.get("categories"):
        lines.append(
            " · ".join(f"{CATEGORIES.get(c, c)}: {_pct(r)}" for c, r in totals["categories"].items())
        )
        lines.append("")
    if meta.get("skipped"):
        lines += [f"Пропущено (нужны картинки экрана, а их нет): {', '.join(meta['skipped'])}", ""]
    if not meta.get("font_ok", True):
        lines += [
            "⚠ Шрифта с кириллицей не нашлось — на картинках экрана русский текст может не читаться.",
            "",
        ]
    lines += [
        "## Задачи",
        "",
        "| Задача | Что проверяет | Успех | Шаги | Время | Токены | Стоимость | Нарушения |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for task in summary["tasks"]:
        mark = "✅" if task["passed"] == task["attempts"] else ("❌" if task["passed"] == 0 else "⚠️")
        lines.append(
            f"| {mark} `{task['id']}` {_cell(task['title'])} | {_cell(task['checks'])} | "
            f"{task['passed']}/{task['attempts']} ({_pct(task['rate'])}) | {task['steps']} | "
            f"{task['seconds']} с | "
            f"{_tokens(task['prompt_tokens'], task['completion_tokens'])} | {_money(task['cost'])} | "
            f"{task['violations'] or '—'} |"
        )
    failed = [(t, r) for t in summary["tasks"] for r in t["results"] if not r["ok"]]
    lines += ["", "## Провалы", ""]
    if not failed:
        lines.append("Провалов нет.")
    for task, result in failed:
        variant = f" (ошибочное решение «{result['variant']}»)" if result.get("variant") else ""
        lines += [f"### `{task['id']}` — попытка {result['attempt']}{variant}", ""]
        lines.append(f"Задача: «{task['text']}»")
        lines.append("")
        for violation in result["violations"]:
            lines.append(f"- 🛑 {violation}")
        for failure in result["failures"]:
            lines.append(f"- ❌ {failure}")
        for warning in result["warnings"]:
            lines.append(f"- ⚠️ {warning}")
        said = _cell(result["summary"])[:300]
        lines.append(f"- Итог агента ({result['status']} / {result['outcome'] or '—'}): «{said}»")
        if result["questions"]:
            lines.append(f"- Вопросы: {'; '.join(_cell(q) for q in result['questions'])}")
        if result["approvals"]:
            asked = "; ".join(
                f"{_cell(a['title'])} — {'да' if a['allowed'] else 'нет'}" for a in result["approvals"]
            )
            lines.append(f"- Разрешения: {asked}")
        if result["actions"]:
            lines.append(f"- Последние шаги: {' → '.join(_cell(a) for a in result['actions'][-6:])}")
        lines.append(f"- Шагов: {result['steps']}, время: {result['seconds']} с")
        if result.get("journal"):
            lines.append(f"- Журнал: [{result['journal']}]({result['journal']})")
        lines.append("")
    warned = [(t, r) for t in summary["tasks"] for r in t["results"] if r["ok"] and r["warnings"]]
    if warned:
        lines += ["## Замечания у удачных попыток", ""]
        for task, result in warned:
            lines.append(f"- `{task['id']}` #{result['attempt']}: {'; '.join(result['warnings'])}")
        lines.append("")
    if comparison is not None:
        lines += render_comparison(comparison)
    return "\n".join(lines).rstrip() + "\n"


def render_comparison(comparison: dict[str, Any]) -> list[str]:
    base = comparison.get("baseline_meta", {})
    lines = [
        "## Сравнение с базовой линией",
        "",
        f"Базовая линия: {_model(base)}, {base.get('started', '')}",
        "",
        f"Оценка: {_pct(comparison['score_before'])} → {_pct(comparison['score'])} "
        f"({'+' if comparison['delta'] >= 0 else ''}{round(comparison['delta'] * 100)} п.) · нарушений: "
        f"{comparison['violations_before']} → {comparison['violations']}",
        "",
        f"Стало лучше: {', '.join(comparison['better']) or 'ничего'} · стало хуже: "
        f"{', '.join(comparison['worse']) or 'ничего'}",
        "",
        "| Задача | Было | Стало | Изменение | Шаги | Стоимость |",
        "|---|---|---|---|---|---|",
    ]
    for row in comparison["rows"]:
        before = _pct(row["rate_before"]) if "rate_before" in row else "—"
        now = _pct(row["rate"]) if "rate" in row else "—"
        steps = f"{row.get('steps_before', '—')} → {row.get('steps', '—')}" if "steps" in row else "—"
        cost = f"{_money(row.get('cost_before', 0))} → {_money(row.get('cost', 0))}" if "cost" in row else "—"
        lines.append(f"| `{row['id']}` | {before} | {now} | {CHANGE[row['change']]} | {steps} | {cost} |")
    lines.append("")
    return lines


def write_journal_md(folder: Path, task: dict[str, Any], result: dict[str, Any]) -> Path | None:
    """Разбор попытки: шаги агента из journal.json с картинками экрана."""
    try:
        data = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lines = [
        f"# `{task['id']}` — попытка {result['attempt']}",
        "",
        f"Задача: «{data.get('task', task['text'])}»",
        "",
        f"Итог агента: {data.get('status')} / {data.get('outcome', '—')} — "
        f"«{_cell(data.get('summary', ''))}»",
        "",
    ]
    verdict = result["violations"] + result["failures"]
    lines.append("Проверка: " + ("успех" if result["ok"] else "провал — " + "; ".join(verdict)))
    lines.append("")
    if data.get("plan"):
        lines += ["План: " + " → ".join(data["plan"]), ""]
    for step in data.get("steps", []):
        mark = "" if step.get("ok", True) else " — ❌"
        lines += [f"## {step['index']}. {step['title']}{mark}", ""]
        if step.get("name"):
            args = json.dumps(step.get("args") or {}, ensure_ascii=False)
            lines += [f"`{step['name']}` {args}", ""]
        if step.get("thought"):
            lines += [f"> {_cell(step['thought'])[:600]}", ""]
        text = str(step.get("result") or "")
        if text:
            cut = text[:MAX_RESULT] + ("\n… [обрезано]" if len(text) > MAX_RESULT else "")
            lines += ["```text", cut, "```", ""]
        for image in step.get("images") or []:
            lines += [f"![шаг {step['index']}]({image})", ""]
    path = folder / "journal.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def scrub(folder: Path, secret: str) -> None:
    """Ключ (если вдруг попал в текст) — из всех текстовых файлов прогона."""
    if not secret:
        return
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if secret in text:
                path.write_text(text.replace(secret, "••••"), encoding="utf-8")
