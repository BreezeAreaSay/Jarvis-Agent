// Всплывающее меню раздела: привязка к кнопке или к точке (правый клик), уход от краёв окна,
// стрелки ↑/↓, Enter, Esc. Рисуется в document.body, поэтому не обрезается карточками.
import { Check } from "lucide-react";
import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cx } from "@/ui";

export interface MenuItem {
  key: string;
  label: ReactNode;
  icon?: ReactNode;
  hint?: ReactNode;
  checked?: boolean;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

type Placement = "bottom-start" | "bottom-end" | "top-start" | "top-end";

export function Menu({
  anchor,
  point,
  items,
  onClose,
  placement = "bottom-start",
  header,
  footer,
  width,
  maxHeight = 320,
  label,
  empty,
}: {
  anchor?: HTMLElement | null;
  point?: { x: number; y: number };
  items: MenuItem[];
  onClose: () => void;
  placement?: Placement;
  header?: ReactNode;
  footer?: ReactNode;
  width?: number;
  maxHeight?: number;
  label?: string;
  empty?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const m = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left: number;
    let top: number;
    if (point) {
      left = point.x;
      top = point.y;
      if (left + m.width > vw - 8) left = vw - m.width - 8;
      if (top + m.height > vh - 8) top = Math.max(8, point.y - m.height);
    } else {
      const a = anchor?.getBoundingClientRect() ?? new DOMRect(vw / 2, vh / 2, 0, 0);
      const up = placement.startsWith("top");
      const end = placement.endsWith("end");
      left = end ? a.right - m.width : a.left;
      top = up ? a.top - m.height - 6 : a.bottom + 6;
      if (!up && top + m.height > vh - 8 && a.top - m.height - 6 > 8) top = a.top - m.height - 6;
      if (up && top < 8 && a.bottom + 6 + m.height < vh - 8) top = a.bottom + 6;
    }
    setPos({ left: Math.max(8, Math.min(left, vw - m.width - 8)), top: Math.max(8, top) });
  }, [anchor, point, placement, items.length]);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || anchor?.contains(t)) return;
      closeRef.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeRef.current();
        anchor?.focus();
      }
    };
    const onResize = () => closeRef.current();
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    window.addEventListener("blur", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("blur", onResize);
    };
  }, [anchor]);

  // фокус — на выбранный пункт (или первый), если в шапке нет поля поиска
  useEffect(() => {
    if (!pos) return;
    const el = ref.current;
    if (!el || el.querySelector("input")) return;
    const target =
      el.querySelector<HTMLElement>('[role="menuitemradio"][aria-checked="true"]') ??
      el.querySelector<HTMLElement>('[role^="menuitem"]:not(:disabled)');
    target?.focus({ preventScroll: true });
  }, [pos !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const list = Array.from(ref.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not(:disabled)') ?? []);
    if (!list.length) return;
    e.preventDefault();
    const i = list.indexOf(document.activeElement as HTMLElement);
    let next = 0;
    if (e.key === "ArrowDown") next = i < 0 ? 0 : (i + 1) % list.length;
    if (e.key === "ArrowUp") next = i <= 0 ? list.length - 1 : i - 1;
    if (e.key === "End") next = list.length - 1;
    list[next].focus();
  };

  const hasChecks = items.some((i) => i.checked !== undefined);

  return createPortal(
    <div
      ref={ref}
      className="ai-menu"
      role="menu"
      aria-label={label}
      style={{
        left: pos?.left ?? -9999,
        top: pos?.top ?? -9999,
        width,
        visibility: pos ? "visible" : "hidden",
      }}
      onKeyDown={onKeyDown}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      {header}
      <div className="ai-menu-list" style={{ maxHeight }}>
        {items.map((it) => (
          <button
            key={it.key}
            type="button"
            role={hasChecks ? "menuitemradio" : "menuitem"}
            aria-checked={hasChecks ? !!it.checked : undefined}
            disabled={it.disabled}
            className={cx("ai-menu-item", it.danger && "danger", it.checked && "checked")}
            onClick={() => {
              it.onSelect();
              onClose();
            }}
          >
            {it.icon && <span className="mi-icon">{it.icon}</span>}
            <span className="mi-text">
              <span className="mi-label">{it.label}</span>
              {it.hint && <span className="mi-hint">{it.hint}</span>}
            </span>
            {it.checked && <Check className="mi-check" />}
          </button>
        ))}
        {items.length === 0 && empty && <div className="ai-menu-empty">{empty}</div>}
      </div>
      {footer}
    </div>,
    document.body,
  );
}
