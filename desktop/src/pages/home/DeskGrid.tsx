// Сетка стола: 4 колонки, виджеты по клеткам; в режиме правки — перетаскивание, размер, удаление, «+».
import { Plus, Settings2, X } from "lucide-react";
import { type PointerEvent as ReactPointerEvent, useMemo, useRef, useState } from "react";
import type { WidgetPlacement } from "@/api/types";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { cx } from "@/ui";
import { WIDGET_BY_TYPE } from "@/widgets/registry";

export const COLS = 4;
const GAP = 12;

type Rect = { x: number; y: number; w: number; h: number };

function overlaps(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

export function fits(rect: Rect, widgets: WidgetPlacement[], ignoreId?: string): boolean {
  if (rect.x < 0 || rect.y < 0 || rect.x + rect.w > COLS) return false;
  return widgets.every((w) => w.id === ignoreId || !overlaps(rect, w));
}

export function findSpot(widgets: WidgetPlacement[], w: number, h: number, prefer?: { x: number; y: number }): Rect {
  if (prefer && fits({ ...prefer, w, h }, widgets)) return { ...prefer, w, h };
  for (let y = 0; y < 200; y++) {
    for (let x = 0; x + w <= COLS; x++) {
      if (fits({ x, y, w, h }, widgets)) return { x, y, w, h };
    }
  }
  return { x: 0, y: 0, w, h };
}

interface Drag {
  id: string;
  mode: "move" | "resize";
  offX: number;
  offY: number;
  rect: Rect;
  ok: boolean;
}

export function DeskGrid({
  widgets,
  editing,
  rowHeight = 104,
  onChange,
  onAddAt,
  onConfigure,
}: {
  widgets: WidgetPlacement[];
  editing: boolean;
  rowHeight?: number;
  onChange: (widgets: WidgetPlacement[]) => void;
  onAddAt: (cell: { x: number; y: number }) => void;
  onConfigure: (widget: WidgetPlacement) => void;
}) {
  const gridRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);

  const rows = useMemo(() => {
    const bottom = widgets.reduce((m, w) => Math.max(m, w.y + w.h), 0);
    return Math.max(bottom + (editing ? 1 : 0), editing ? 4 : 1);
  }, [widgets, editing]);

  const freeCells = useMemo(() => {
    if (!editing) return [];
    const cells: { x: number; y: number }[] = [];
    for (let y = 0; y < rows; y++)
      for (let x = 0; x < COLS; x++)
        if (widgets.every((w) => !overlaps({ x, y, w: 1, h: 1 }, w))) cells.push({ x, y });
    return cells;
  }, [widgets, rows, editing]);

  const cellAt = (clientX: number, clientY: number) => {
    const el = gridRef.current!;
    const box = el.getBoundingClientRect();
    const colW = (box.width - GAP * (COLS - 1)) / COLS;
    const cx_ = Math.floor((clientX - box.left) / (colW + GAP));
    const cy_ = Math.floor((clientY - box.top + el.scrollTop * 0) / (rowHeight + GAP));
    return { cx: Math.max(0, Math.min(COLS - 1, cx_)), cy: Math.max(0, cy_) };
  };

  const begin = (e: ReactPointerEvent, w: WidgetPlacement, mode: Drag["mode"]) => {
    e.preventDefault();
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    const { cx: pcx, cy: pcy } = cellAt(e.clientX, e.clientY);
    setDrag({ id: w.id, mode, offX: pcx - w.x, offY: pcy - w.y, rect: { x: w.x, y: w.y, w: w.w, h: w.h }, ok: true });
  };

  const move = (e: ReactPointerEvent) => {
    if (!drag) return;
    const { cx: pcx, cy: pcy } = cellAt(e.clientX, e.clientY);
    const widget = widgets.find((w) => w.id === drag.id)!;
    const def = WIDGET_BY_TYPE[widget.type];
    let rect: Rect;
    if (drag.mode === "move") {
      rect = {
        x: Math.max(0, Math.min(COLS - widget.w, pcx - drag.offX)),
        y: Math.max(0, pcy - drag.offY),
        w: widget.w,
        h: widget.h,
      };
    } else {
      const [minW, minH] = def?.min ?? [1, 1];
      const [maxW, maxH] = def?.max ?? [4, 6];
      rect = {
        x: widget.x,
        y: widget.y,
        w: Math.max(minW, Math.min(maxW, COLS - widget.x, pcx - widget.x + 1)),
        h: Math.max(minH, Math.min(maxH, pcy - widget.y + 1)),
      };
    }
    if (rect.x !== drag.rect.x || rect.y !== drag.rect.y || rect.w !== drag.rect.w || rect.h !== drag.rect.h)
      setDrag({ ...drag, rect, ok: fits(rect, widgets, drag.id) });
  };

  const end = () => {
    if (!drag) return;
    if (drag.ok) onChange(widgets.map((w) => (w.id === drag.id ? { ...w, ...drag.rect } : w)));
    setDrag(null);
  };

  return (
    <div
      ref={gridRef}
      className="desk-grid"
      style={{ "--row-h": `${rowHeight}px`, gridTemplateRows: `repeat(${rows}, ${rowHeight}px)` } as React.CSSProperties}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={() => setDrag(null)}
    >
      {freeCells.map((c) => (
        <button
          key={`slot-${c.x}-${c.y}`}
          className="desk-slot"
          style={{ gridColumn: `${c.x + 1} / span 1`, gridRow: `${c.y + 1} / span 1` }}
          onClick={() => onAddAt(c)}
          aria-label="Добавить виджет"
        >
          <Plus />
        </button>
      ))}
      {widgets.map((w) => {
        const def = WIDGET_BY_TYPE[w.type];
        const Comp = def?.component;
        return (
          <div
            key={w.id}
            className={cx("desk-cell", editing && "editing", drag?.id === w.id && "dragging")}
            style={{ gridColumn: `${w.x + 1} / span ${w.w}`, gridRow: `${w.y + 1} / span ${w.h}` }}
          >
            {Comp ? (
              <ErrorBoundary compact area={`виджет «${def.title}»`}>
                <Comp
                  id={w.id}
                  w={w.w}
                  h={w.h}
                  config={w.config ?? {}}
                  editing={editing}
                  setConfig={(config) => onChange(widgets.map((x) => (x.id === w.id ? { ...x, config } : x)))}
                />
              </ErrorBoundary>
            ) : (
              <div className="widget center faint" style={{ flexDirection: "column", gap: 6, textAlign: "center" }}>
                {def?.icon}
                <span className="small">{def?.title ?? w.type}</span>
                <span className="tiny">скоро</span>
              </div>
            )}
            {editing && (
              <>
                <div className="cell-move" onPointerDown={(e) => begin(e, w, "move")} />
                <div className="cell-tools">
                  <button className="icon-btn" aria-label="Настроить" onClick={() => onConfigure(w)}>
                    <Settings2 />
                  </button>
                  <button
                    className="icon-btn"
                    aria-label="Убрать"
                    onClick={() => onChange(widgets.filter((x) => x.id !== w.id))}
                  >
                    <X />
                  </button>
                </div>
                <div className="cell-resize" onPointerDown={(e) => begin(e, w, "resize")} />
              </>
            )}
          </div>
        );
      })}
      {drag && (
        <div
          className={cx("desk-ghost", !drag.ok && "bad")}
          style={{
            gridColumn: `${drag.rect.x + 1} / span ${drag.rect.w}`,
            gridRow: `${drag.rect.y + 1} / span ${drag.rect.h}`,
          }}
        />
      )}
    </div>
  );
}
