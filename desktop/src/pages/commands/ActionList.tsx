// Список действий редактора: нумерованные строки «1 | Launch.File | ⛓ | OBS Studio | 📁 | ⊗», перетаскивание
// за номер (и стрелками с клавиатуры), дублирование, удаление.
import { CircleX, Copy, GripVertical, Route } from "lucide-react";
import { type CSSProperties, type PointerEvent as ReactPointerEvent, useRef, useState } from "react";
import type { ActionType } from "@/api/types";
import { cx } from "@/ui";
import { ActionTypeSelect, defaultSlot, ValueCells } from "./ActionFields";
import { isRequired, slotOf, unknownAction } from "./model";

export interface DraftAction {
  key: string;
  type: string;
  value: string;
}

interface Drag {
  from: number;
  to: number;
  dy: number;
  step: number;
}

export function ActionList({
  actions,
  catalog,
  byType,
  phrases,
  tried,
  autoOpenKey,
  onAutoOpened,
  onChange,
  onMove,
  onRemove,
  onDuplicate,
}: {
  actions: DraftAction[];
  catalog: ActionType[];
  byType: Map<string, ActionType>;
  /** текст фраз активации — чтобы предупредить, если слот {number} в них не встречается */
  phrases: string;
  tried: boolean;
  /** только что добавленное действие — сразу открыть выбор типа */
  autoOpenKey?: string | null;
  onAutoOpened?: () => void;
  onChange: (key: string, patch: Partial<DraftAction>) => void;
  onMove: (from: number, to: number) => void;
  onRemove: (key: string) => void;
  onDuplicate: (key: string) => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  // сразу после броска строки встают на места без анимации (иначе мигнут из старых сдвигов)
  const [settling, setSettling] = useState(false);

  const focusHandle = (key: string) =>
    requestAnimationFrame(() =>
      listRef.current?.querySelector<HTMLElement>(`[data-key="${key}"] .act-num`)?.focus({ preventScroll: false }),
    );

  const startDrag = (index: number, e: ReactPointerEvent<HTMLButtonElement>) => {
    if (e.button !== 0 || actions.length < 2) return;
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>("[data-act-row]") ?? []);
    const rects = rows.map((r) => r.getBoundingClientRect());
    if (!rects[index]) return;
    const step = rects.length > 1 ? rects[1].top - rects[0].top : rects[index].height + 8;
    const centers = rects.map((r) => r.top + r.height / 2);
    const startY = e.clientY;
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    let last: Drag | null = null;
    const move = (ev: PointerEvent) => {
      const dy = ev.clientY - startY;
      if (!last && Math.abs(dy) < 4) return;
      const c = centers[index] + dy;
      let to = 0;
      for (let i = 0; i < centers.length; i++) if (i !== index && c > centers[i]) to++;
      last = { from: index, to, dy, step };
      setDrag(last);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      if (!last) return;
      setSettling(true);
      setDrag(null);
      if (last.to !== last.from) onMove(last.from, last.to);
      requestAnimationFrame(() => requestAnimationFrame(() => setSettling(false)));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };

  const offset = (i: number): CSSProperties | undefined => {
    if (!drag) return undefined;
    if (i === drag.from) return { transform: `translateY(${drag.dy}px)`, transition: "none", zIndex: 5 };
    if (drag.from < drag.to && i > drag.from && i <= drag.to) return { transform: `translateY(${-drag.step}px)` };
    if (drag.from > drag.to && i >= drag.to && i < drag.from) return { transform: `translateY(${drag.step}px)` };
    return { transform: "translateY(0)" };
  };

  return (
    <div className={cx("act-list", drag && "dragging", settling && "settling")} ref={listRef}>
      {actions.map((action, i) => {
        const at = byType.get(action.type) ?? unknownAction(action.type);
        const param = at.params[0];
        const slot = slotOf(action.value);
        const linked = !!param && !!slot;
        const slotMissing = linked && !phrases.includes(`{${slot}}`);
        const invalid = tried && isRequired(at) && !action.value.trim();
        return (
          <div
            key={action.key}
            data-act-row={i}
            data-key={action.key}
            className={cx("act-row", drag?.from === i && "lifted", invalid && "invalid")}
            style={offset(i)}
          >
            <button
              type="button"
              className="act-cell act-num"
              aria-label={`Действие ${i + 1}. Перетащите или нажмите ↑ ↓, чтобы изменить порядок`}
              title="Перетащите, чтобы изменить порядок"
              onPointerDown={(e) => startDrag(i, e)}
              onKeyDown={(e) => {
                const to = e.key === "ArrowUp" ? i - 1 : e.key === "ArrowDown" ? i + 1 : -1;
                if (to < 0 || to >= actions.length) return;
                e.preventDefault();
                onMove(i, to);
                focusHandle(action.key);
              }}
            >
              <span className="n">{i + 1}</span>
              <GripVertical className="grip" />
            </button>
            <div className="act-cell act-type-cell">
              <ActionTypeSelect
                value={action.type}
                catalog={catalog}
                autoOpen={autoOpenKey === action.key}
                onAutoOpened={onAutoOpened}
                onChange={(type) => {
                  const next = byType.get(type);
                  const sameKind = next?.params[0]?.kind === param?.kind;
                  onChange(action.key, { type, value: sameKind && next?.params.length ? action.value : "" });
                }}
              />
            </div>
            <button
              type="button"
              className={cx("act-cell act-link", linked && "on")}
              disabled={!param}
              aria-pressed={linked}
              aria-label="Взять значение из фразы"
              title={
                param
                  ? linked
                    ? "Значение берётся из фразы — нажмите, чтобы ввести вручную"
                    : "Взять значение из фразы ({number}, {app}, {text}…)"
                  : "У этого действия нет значения"
              }
              onClick={() => onChange(action.key, { value: linked ? "" : defaultSlot(param?.kind) })}
            >
              <Route />
            </button>
            <ValueCells
              value={action.value}
              onChange={(value) => onChange(action.key, { value })}
              param={param}
              at={at}
              linked={linked}
              slotMissing={slotMissing}
              invalid={invalid}
            />
            <button
              type="button"
              className="act-cell act-btn"
              title="Дублировать"
              aria-label={`Дублировать действие ${i + 1}`}
              onClick={() => onDuplicate(action.key)}
            >
              <Copy />
            </button>
            <button
              type="button"
              className="act-cell act-btn act-remove"
              title="Удалить"
              aria-label={`Удалить действие ${i + 1}`}
              onClick={() => onRemove(action.key)}
            >
              <CircleX />
            </button>
          </div>
        );
      })}
    </div>
  );
}
