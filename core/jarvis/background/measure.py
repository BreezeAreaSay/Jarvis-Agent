"""Таблицы замеров фоновых задач: «Замеры/<Название>.md» в своей папке хранилища.

Заметка — обычная Markdown-таблица (первая колонка — «Дата», по строке на замер) и график рядом:
«Замеры/<Название>.svg» (рисует Jarvis, без плагинов Obsidian; в заметке — ![[…svg]]). Новая строка с той же
датой (у частых замеров — с тем же часом) заменяет старую: перезапуск задачи не плодит повторы. Новая колонка
добавляется сама, у старых строк она пустая. Остальной текст заметки человек может править — Jarvis трогает
только таблицу и строку с графиком.

Всё блокирующее (файлы) — из пула ввода-вывода (ctx.run_io).
"""

import math
import os
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.ai.format import one_line
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.background.vault import Vault

FOLDER = "Замеры"
DATE = "Дата"
MAX_COLUMNS = 8
MAX_ROWS = 5000
CHART_POINTS = 180  # последних замеров на графике
CHART_SERIES = 3
COLORS = ("#3b82f6", "#f59e0b", "#10b981")
_NUMBER = re.compile(r"[-−]?\d[\d\s  ]*(?:[.,]\d+)?")
_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})(?:\s+(\d{1,2}):(\d{2}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?")


def parse_number(text: str) -> float | None:
    """«81,42 ₽», «1 234,5», «-3.2%», «12 000» → число; не число — None."""
    m = _NUMBER.search(str(text or ""))
    if not m:
        return None
    raw = m.group(0).replace("−", "-")
    raw = re.sub(r"[\s  ]", "", raw).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def cell(text: Any) -> str:
    """Значение ячейки: одна строка, без «|» (ломает таблицу)."""
    return one_line(str(text if text is not None else ""), 200).replace("|", "/")


def normalize_date(text: str, *, hourly: bool) -> str:
    """«28.10.2026», «2026-10-28T21:05» → «2026-10-28» (частые замеры — «2026-10-28 21:00»); не дата — как
    есть."""
    raw = str(text or "").strip()
    m = _DMY.match(raw)
    if m:
        day, month, year = int(m[1]), int(m[2]), int(m[3])
        hour, minute = (int(m[4]), int(m[5])) if m[4] else (0, 0)
    else:
        m = _ISO.match(raw)
        if not m:
            return cell(raw)
        year, month, day = int(m[1]), int(m[2]), int(m[3])
        hour, minute = (int(m[4]), int(m[5])) if m[4] else (0, 0)
    try:
        moment = datetime(year, month, day, hour, minute)
    except ValueError:
        return cell(raw)
    return moment.strftime("%Y-%m-%d %H:00" if hourly else "%Y-%m-%d")


def stamp(moment: datetime, *, hourly: bool) -> str:
    return moment.strftime("%Y-%m-%d %H:00" if hourly else "%Y-%m-%d")


# --- таблица в заметке --------------------------------------------------------------------------------------


def _row(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [c.strip() for c in inner.split("|")]


def _is_separator(line: str) -> bool:
    cells = _row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def find_table(lines: list[str]) -> tuple[int, int] | None:
    """(первая строка, строка после таблицы) таблицы замеров — той, что начинается с колонки «Дата»."""
    for i, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        head = _row(line)
        if head and head[0].casefold() == DATE.casefold() and _is_separator(lines[i + 1]):
            end = i + 2
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            return i, end
    return None


def read_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Колонки и строки таблицы замеров из текста заметки; таблицы нет — ([], [])."""
    lines = text.splitlines()
    found = find_table(lines)
    if found is None:
        return [], []
    start, end = found
    columns = _row(lines[start])
    rows = []
    for line in lines[start + 2 : end]:
        values = _row(line)
        values = (values + [""] * len(columns))[: len(columns)]
        if any(values):
            rows.append(values)
    return columns, rows


def render_table(columns: list[str], rows: list[list[str]]) -> list[str]:
    out = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def numeric_columns(columns: list[str], rows: list[list[str]]) -> list[int]:
    """Колонки, где в большинстве заполненных строк — числа (их можно рисовать графиком)."""
    found = []
    for i in range(1, len(columns)):
        filled = [r[i] for r in rows if r[i].strip()]
        numbers = [v for v in filled if parse_number(v) is not None]
        if len(numbers) >= 2 and len(numbers) >= 0.7 * len(filled):
            found.append(i)
    return found


# --- график ------------------------------------------------------------------------------------------------


def _fmt(value: float) -> str:
    if abs(value) >= 1000 or value == int(value):
        text = f"{value:,.0f}".replace(",", " ")
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.replace(".", ",")


def render_svg(title: str, columns: list[str], rows: list[list[str]]) -> str | None:
    """Линейный график числовых колонок (до трёх) по последним замерам; рисовать нечего — None.
    Прозрачный фон и серые подписи — читается и в светлой, и в тёмной теме Obsidian."""
    series = numeric_columns(columns, rows)[:CHART_SERIES]
    rows = rows[-CHART_POINTS:]
    if not series or len(rows) < 2:
        return None
    width, height, left, right, top, bottom = 760, 320, 64, 20, 44, 40
    values = [parse_number(r[i]) for r in rows for i in series]
    known = [v for v in values if v is not None]
    if not known:
        return None
    low, high = min(known), max(known)
    if high == low:
        low, high = low - 1, high + 1
    pad = (high - low) * 0.08
    low, high = low - pad, high + pad
    plot_w, plot_h = width - left - right, height - top - bottom

    def x(index: int) -> float:
        return left + plot_w * index / max(1, len(rows) - 1)

    def y(value: float) -> float:
        return top + plot_h * (1 - (value - low) / (high - low))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" font-family="system-ui, -apple-system, Segoe UI, sans-serif" font-size="12">',
        f'<text x="{left}" y="22" fill="#8a8f98" font-size="14" font-weight="600">{escape(title)}</text>',
    ]
    for k in range(5):  # сетка и подписи значений
        value = low + (high - low) * k / 4
        yy = y(value)
        parts.append(
            f'<line x1="{left}" y1="{yy:.1f}" x2="{width - right}" y2="{yy:.1f}" stroke="#8a8f98" '
            'stroke-opacity="0.25" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{yy + 4:.1f}" fill="#8a8f98" text-anchor="end">{_fmt(value)}</text>'
        )
    marks = sorted({0, len(rows) // 2, len(rows) - 1})
    for index in marks:  # даты: первая, середина, последняя
        anchor = "start" if index == 0 else "end" if index == len(rows) - 1 else "middle"
        parts.append(
            f'<text x="{x(index):.1f}" y="{height - 14}" fill="#8a8f98" text-anchor="{anchor}">'
            f"{escape(rows[index][0][:16])}</text>"
        )
    legend_x = width - right
    for n, column in enumerate(series):
        color = COLORS[n]
        points = [(x(i), parse_number(r[column])) for i, r in enumerate(rows)]
        path, pen = [], "M"
        for px, value in points:
            if value is None:
                pen = "M"
                continue
            path.append(f"{pen}{px:.1f},{y(value):.1f}")
            pen = "L"
        parts.append(
            f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2.5" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        last = next(((px, v) for px, v in reversed(points) if v is not None), None)
        if last is not None:
            parts.append(f'<circle cx="{last[0]:.1f}" cy="{y(last[1]):.1f}" r="4" fill="{color}"/>')
        # подпись серии справа сверху: имя колонки и последнее значение
        label = f"{columns[column]}: {_fmt(last[1])}" if last is not None else columns[column]
        parts.append(
            f'<text x="{legend_x}" y="{22 + 16 * n}" fill="{color}" text-anchor="end" '
            f'font-weight="600">{escape(one_line(label, 60))}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# --- запись замера -----------------------------------------------------------------------------------------


def _values(raw: Any) -> list[tuple[str, str]]:
    """values модели: {колонка: значение} или [{column, value}] → [(колонка, значение)]."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(k), str(v)) for k, v in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                column = item.get("column") or item.get("name") or item.get("key")
                value = item.get("value")
                pairs.append((str(column or ""), "" if value is None else str(value)))
    out: list[tuple[str, str]] = []
    for column, value in pairs:
        name = cell(column).strip(" :")
        if name and value.strip():
            out.append((name[:60], cell(value)))
    return out


def table_rel(vault: "Vault", table: str) -> str:
    name = one_line(table, 80).strip(" ./\\") or "Замеры"
    name = name.split("/")[-1].split("\\")[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return f"{FOLDER}/{name}"


def add_row(
    vault: "Vault", table: str, raw_values: Any, *, hourly: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    """Строка замера в таблицу (нет таблицы — создать) и график рядом. Вернёт {path, row, replaced, columns,
    rows, chart}."""
    values = _values(raw_values)
    given_date = next((v for c, v in values if c.casefold() == DATE.casefold()), "")
    values = [(c, v) for c, v in values if c.casefold() != DATE.casefold()]
    if not values:
        raise UserFacingError("Ошибка: нет значений замера — values: {колонка: значение}")
    moment = now or datetime.now()
    key = normalize_date(given_date, hourly=hourly) if given_date else stamp(moment, hourly=hourly)
    path = vault.resolve(table_rel(vault, table), write=True)
    vault.ensure()
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.stem
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        text = ""
    lines = text.splitlines()
    columns, rows = read_table(text)
    if not columns:
        columns = [DATE]
    for column, _ in values:
        if column.casefold() not in (c.casefold() for c in columns):
            if len(columns) >= MAX_COLUMNS:
                raise UserFacingError(f"Ошибка: в таблице уже {MAX_COLUMNS} колонок — запиши в существующие")
            columns.append(column)
            rows = [[*r, ""] for r in rows]
    index = {c.casefold(): i for i, c in enumerate(columns)}
    row = next((r for r in rows if r[0] == key), None)
    replaced = row is not None
    if row is None:
        row = [key] + [""] * (len(columns) - 1)
        rows.append(row)
    for column, value in values:
        row[index[column.casefold()]] = value
    rows.sort(key=lambda r: r[0])
    rows = rows[-MAX_ROWS:]
    table_lines = render_table(columns, rows)
    rel_svg = vault.rel(path.with_suffix(".svg").resolve())
    embed = f"![[{rel_svg}]]"
    svg = render_svg(title, columns, rows)
    found = find_table(lines)
    if found is None:
        if not lines:
            lines = [
                "---",
                "type: jarvis-measurements",
                "tags: [jarvis, замеры]",
                "---",
                "",
                f"# {title}",
                "",
            ]
        if svg is not None and embed not in text:
            lines += [embed, ""]
        lines += table_lines
    else:
        start, end = found
        lines[start:end] = table_lines
        if svg is not None and embed not in text:
            lines[start:start] = [embed, ""]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if svg is not None:
        svg_path = path.with_suffix(".svg")
        tmp = svg_path.with_name(svg_path.name + ".tmp")
        tmp.write_text(svg, encoding="utf-8")
        os.replace(tmp, svg_path)
    return {
        "path": vault.rel(path.resolve()),
        "row": dict(zip(columns, row, strict=False)),
        "replaced": replaced,
        "columns": columns,
        "rows": len(rows),
        "chart": svg is not None,
    }


def load(vault: "Vault", rel: str, limit: int = 400) -> dict[str, Any] | None:
    """Таблица замеров для интерфейса: {path, name, columns, rows (старые первыми, последние limit), numeric};
    заметки нет — None."""
    try:
        path = vault.resolve(rel)
    except UserFacingError:
        return None
    if not path.is_file():
        return None
    columns, rows = read_table(path.read_text("utf-8", errors="replace"))
    return {
        "path": vault.rel(path.resolve()),
        "name": Path(rel).stem,
        "columns": columns,
        "rows": rows[-limit:],
        "total": len(rows),
        "numeric": numeric_columns(columns, rows),
        "link": vault.link(vault.rel(path.resolve())),
    }
