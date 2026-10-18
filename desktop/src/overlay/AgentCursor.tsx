// Курсор агента: агент работает своим «призрачным» курсором поверх экрана — видно, куда он жмёт и что вводит,
// а курсор человека остаётся у человека. Точки приходят в физических пикселях экрана (agent.pointer): оверлей
// лежит на основном мониторе (его начало — 0,0 и в физических точках, и в DIP), так что точка оверлея =
// физическая / масштаб − начало рабочей области, а в CSS-пикселях ещё и делится на масштаб интерфейса (zoom
// корня). Точки на других мониторах не рисуем. Мышь курсор не ловит: в нём нет [data-interactive].
import { useEffect, useRef, useState } from "react";
import { cx } from "@/ui";
import { useOverlayEvent } from "./bus";
import { useBounds, useUiZoom } from "./common";

interface Pointer {
  run_id: string;
  kind: "click" | "double" | "right" | "type" | "keys" | "scroll" | "focus";
  x: number;
  y: number;
  label?: string;
  rect?: [number, number, number, number];
  ts: number;
}

const FINISHED = new Set(["done", "stopped", "error", "idle"]);
const CLICKS = new Set(["click", "double", "right"]);
const VERB: Record<Pointer["kind"], string> = {
  click: "",
  double: "Двойной щелчок",
  right: "Правый щелчок",
  type: "Ввод",
  keys: "Клавиши",
  scroll: "Прокрутка",
  focus: "Окно",
};

export function AgentCursor() {
  const bounds = useBounds();
  const zoom = useUiZoom();
  const [pointer, setPointer] = useState<Pointer | null>(null);
  const [visible, setVisible] = useState(false);
  const [pulse, setPulse] = useState(0);
  const hideTimer = useRef(0);

  useOverlayEvent<Pointer>("agent.pointer", (p) => {
    if (!p || typeof p.x !== "number") return;
    clearTimeout(hideTimer.current);
    setPointer(p);
    setVisible(true);
    if (CLICKS.has(p.kind)) setPulse((n) => n + 1);
  });
  useOverlayEvent<{ status: string }>("agent.state", (s) => {
    if (s && FINISHED.has(s.status)) {
      clearTimeout(hideTimer.current);
      hideTimer.current = window.setTimeout(() => setVisible(false), 1500);
    }
  });
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  if (!pointer) return null;
  const scale = bounds.scale || 1;
  const local = (x: number, y: number): [number, number] => [(x / scale - bounds.x) / zoom, (y / scale - bounds.y) / zoom];
  const [px, py] = local(pointer.x, pointer.y);
  const width = bounds.width / zoom;
  const height = bounds.height / zoom;
  const onScreen = px >= 0 && py >= 0 && px <= width && py <= height;
  let box: { left: number; top: number; width: number; height: number } | null = null;
  if (pointer.rect) {
    const [l, t] = local(pointer.rect[0], pointer.rect[1]);
    const [r, b] = local(pointer.rect[2], pointer.rect[3]);
    if (r > l && b > t) box = { left: l, top: t, width: r - l, height: b - t };
  }
  const verb = VERB[pointer.kind] ?? "";
  const label = [verb, pointer.label].filter(Boolean).join(": ");

  return (
    <div className={cx("ov-ac-layer", visible && onScreen && "show")} aria-hidden>
      {box && (
        <div
          key={`box-${pointer.ts}`}
          className={cx("ov-ac-box", pointer.kind === "focus" && "is-window")}
          style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
        />
      )}
      <div className={cx("ov-ac-pointer", `is-${pointer.kind}`)} style={{ transform: `translate(${px}px, ${py}px)` }}>
        {CLICKS.has(pointer.kind) && <span key={`ripple-${pulse}`} className="ov-ac-ripple" />}
        <svg className="ov-ac-arrow" viewBox="0 0 24 24" width="26" height="26">
          <path d="M4 2.5 L4 19.5 L8.6 15.1 L11.6 21.8 L14.6 20.4 L11.7 13.9 L18 13.6 Z" />
        </svg>
        {label && <span className="ov-ac-label">{label}</span>}
      </div>
    </div>
  );
}
