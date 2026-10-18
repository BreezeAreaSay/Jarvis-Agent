// Всплывающие слои раздела: меню у кнопки (поверх прокручиваемых списков) и боковая панель.
import { X } from "lucide-react";
import { type CSSProperties, type ReactNode, type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cx, IconButton } from "@/ui";

type Pos = { top?: number; bottom?: number; left: number; width: number; maxHeight: number };

/**
 * Меню, привязанное к элементу: рисуется в body с position: fixed, поэтому не обрезается списком
 * с прокруткой. Закрывается по Esc и щелчку мимо; переворачивается вверх, если внизу мало места.
 */
export function Popover({
  anchor,
  open,
  onClose,
  children,
  className,
  width,
  minWidth = 0,
  align = "start",
  maxHeight = 360,
  restoreFocus = true,
}: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  /** ширина меню; без неё — по ширине элемента */
  width?: number;
  minWidth?: number;
  align?: "start" | "end";
  maxHeight?: number;
  restoreFocus?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<Pos | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    const place = () => {
      const el = anchor.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const w = Math.min(vw - 16, Math.max(width ?? r.width, minWidth));
      let left = align === "end" ? r.right - w : r.left;
      left = Math.max(8, Math.min(left, vw - w - 8));
      const below = vh - r.bottom - 14;
      const above = r.top - 14;
      const want = Math.min(ref.current?.scrollHeight ?? maxHeight, maxHeight);
      if (below >= want || below >= above) {
        setPos({ top: r.bottom + 6, left, width: w, maxHeight: Math.max(120, Math.min(maxHeight, below)) });
      } else {
        setPos({ bottom: vh - r.top + 6, left, width: w, maxHeight: Math.max(120, Math.min(maxHeight, above)) });
      }
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, anchor, width, minWidth, align, maxHeight]);

  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || anchor.current?.contains(t)) return;
      closeRef.current();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      closeRef.current();
      if (restoreFocus) anchor.current?.focus();
    };
    document.addEventListener("mousedown", down, true);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("mousedown", down, true);
      document.removeEventListener("keydown", key, true);
    };
  }, [open, anchor, restoreFocus]);

  if (!open) return null;
  // пока меню измеряется — прозрачное за краем экрана, но не visibility: hidden: autoFocus внутри должен сработать
  const style: CSSProperties = pos
    ? { top: pos.top, bottom: pos.bottom, left: pos.left, width: pos.width, maxHeight: pos.maxHeight }
    : { top: 0, left: -9999, width: width ?? minWidth ?? undefined, opacity: 0, pointerEvents: "none" };
  return createPortal(
    <div ref={ref} className={cx("cmd-pop", className)} style={style}>
      {children}
    </div>,
    document.body,
  );
}

/** Боковая панель справа (подробности пака). */
export function Drawer({
  open,
  onClose,
  children,
  label,
  width = 460,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  label: string;
  width?: number;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const key = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      prev?.focus?.();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="cmd-drawer-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={panel}
        className="cmd-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        style={{ width: `min(${width}px, calc(100vw - 80px))` }}
      >
        <IconButton className="cmd-drawer-close" icon={<X />} label="Закрыть" variant="ghost" size="sm" onClick={onClose} />
        {children}
      </div>
    </div>,
    document.body,
  );
}

/** Окно подтверждения «Удалить?» / «Выйти без сохранения?». */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmText,
  cancelText = "Отмена",
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmText: string;
  cancelText?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => confirmRef.current?.focus(), 30);
    const key = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    document.addEventListener("keydown", key);
    return () => {
      clearTimeout(t);
      document.removeEventListener("keydown", key);
      prev?.focus?.();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal cmd-confirm" role="alertdialog" aria-modal="true" aria-label={title}>
        <div className="cmd-confirm-body">
          <h3>{title}</h3>
          {children && <div className="muted small">{children}</div>}
        </div>
        <div className="cmd-confirm-foot">
          <button type="button" className="btn" onClick={onClose}>
            {cancelText}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={cx("btn", danger ? "btn-danger" : "btn-primary")}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy && <span className="spinner" />}
            {confirmText}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
