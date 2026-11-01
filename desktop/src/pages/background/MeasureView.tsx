// Вкладка «Замеры» у фоновой задачи: её таблицы замеров (Замеры/<таблица>.md в хранилище) — график числовых
// колонок и последние строки. Ядро — GET /api/background/<id>/tables (docs/CONTRACTS.md §8).
import { BookOpen, ChartLine } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { bridge } from "@/lib/bridge";
import type { MeasureTable } from "@/store/background";
import { Button, Empty, notifyError } from "@/ui";

const COLORS = ["var(--accent)", "#f59e0b", "#10b981"];
const SHOW_ROWS = 30;

/** «81,42 ₽», «1 234,5» → число (как в ядре: jarvis.background.measure.parse_number). */
export function parseNumber(text: string): number | null {
  const m = String(text ?? "").match(/[-−]?\d[\d\s  ]*(?:[.,]\d+)?/);
  if (!m) return null;
  const value = Number(m[0].replace("−", "-").replace(/[\s  ]/g, "").replace(",", "."));
  return Number.isFinite(value) ? value : null;
}

function fmt(value: number): string {
  return value.toLocaleString("ru-RU", { maximumFractionDigits: Math.abs(value) >= 100 ? 1 : 2 });
}

/** Линии числовых колонок по замерам (последние 180), подписи — значения и даты. */
export function LineChart({ table }: { table: MeasureTable }) {
  const rows = table.rows.slice(-180);
  const series = table.numeric.slice(0, 3);
  if (!series.length || rows.length < 2) return null;
  const width = 640;
  const height = 220;
  const pad = { left: 52, right: 16, top: 14, bottom: 26 };
  const values = rows.flatMap((r) => series.map((i) => parseNumber(r[i]))).filter((v): v is number => v !== null);
  if (!values.length) return null;
  let low = Math.min(...values);
  let high = Math.max(...values);
  if (low === high) {
    low -= 1;
    high += 1;
  }
  const margin = (high - low) * 0.08;
  low -= margin;
  high += margin;
  const x = (i: number) => pad.left + ((width - pad.left - pad.right) * i) / (rows.length - 1);
  const y = (v: number) => pad.top + (height - pad.top - pad.bottom) * (1 - (v - low) / (high - low));
  const grid = [0, 1, 2, 3].map((k) => low + ((high - low) * k) / 3);
  const marks = [...new Set([0, Math.floor(rows.length / 2), rows.length - 1])];
  return (
    <div className="bg-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`График: ${table.name}`}>
        {grid.map((v) => (
          <g key={v}>
            <line x1={pad.left} x2={width - pad.right} y1={y(v)} y2={y(v)} className="bg-chart-grid" />
            <text x={pad.left - 8} y={y(v) + 4} textAnchor="end" className="bg-chart-label">
              {fmt(v)}
            </text>
          </g>
        ))}
        {marks.map((i) => (
          <text
            key={i}
            x={x(i)}
            y={height - 6}
            textAnchor={i === 0 ? "start" : i === rows.length - 1 ? "end" : "middle"}
            className="bg-chart-label"
          >
            {rows[i][0].slice(0, 16)}
          </text>
        ))}
        {series.map((column, n) => {
          let pen = "M";
          const path = rows
            .map((r, i) => {
              const v = parseNumber(r[column]);
              if (v === null) {
                pen = "M";
                return "";
              }
              const part = `${pen}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
              pen = "L";
              return part;
            })
            .join(" ");
          let lastIndex = rows.length - 1;
          while (lastIndex >= 0 && parseNumber(rows[lastIndex][column]) === null) lastIndex -= 1;
          const last = lastIndex >= 0 ? parseNumber(rows[lastIndex][column]) : null;
          return (
            <g key={column}>
              <path d={path} fill="none" stroke={COLORS[n]} strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
              {last !== null && <circle cx={x(lastIndex)} cy={y(last)} r={4} fill={COLORS[n]} />}
            </g>
          );
        })}
      </svg>
      <div className="bg-chart-legend">
        {series.map((column, n) => {
          const last = [...rows].reverse().map((r) => parseNumber(r[column])).find((v) => v !== null);
          return (
            <span key={column} style={{ color: COLORS[n] }}>
              <i style={{ background: COLORS[n] }} />
              {table.columns[column]}
              {last != null ? `: ${fmt(last)}` : ""}
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function MeasureView({ taskId, version }: { taskId: string; version: string }) {
  const [tables, setTables] = useState<MeasureTable[] | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .get<{ items: MeasureTable[] }>(`/api/background/${taskId}/tables`)
      .then((r) => alive && setTables(r.items))
      .catch((e) => {
        if (alive) setTables([]);
        notifyError(e);
      });
    return () => {
      alive = false;
    };
  }, [taskId, version]);
  if (tables === null) return null;
  if (!tables.length) return <Empty icon={<ChartLine />}>Замеров пока нет</Empty>;
  return (
    <div className="stack">
      {tables.map((table) => (
        <section key={table.path} className="card bg-measure">
          <div className="row">
            <ChartLine />
            <strong>{table.name}</strong>
            <span className="faint tiny">
              {table.total} {table.total === 1 ? "замер" : table.total < 5 ? "замера" : "замеров"}
            </span>
            <span className="spacer" />
            <Button size="sm" variant="soft" icon={<BookOpen />} onClick={() => bridge.openExternal(table.link)}>
              В Obsidian
            </Button>
          </div>
          <LineChart table={table} />
          <div className="bg-measure-table">
            <table>
              <thead>
                <tr>
                  {table.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...table.rows]
                  .reverse()
                  .slice(0, SHOW_ROWS)
                  .map((r) => (
                    <tr key={r[0]}>
                      {r.map((value, i) => (
                        <td key={i} className={table.numeric.includes(i) ? "num" : undefined}>
                          {value}
                        </td>
                      ))}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
