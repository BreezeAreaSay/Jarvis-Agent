// Базовые компоненты интерфейса (стили — src/styles/components.css).
import { Check as CheckIcon, ChevronDown, X } from "lucide-react";
import {
  type ButtonHTMLAttributes,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// --- кнопки -------------------------------------------------------------------------------------------
type BtnVariant = "default" | "primary" | "soft" | "ghost" | "danger";

export function Button({
  variant = "default",
  size,
  block,
  icon,
  loading,
  children,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: BtnVariant;
  size?: "sm" | "lg";
  block?: boolean;
  icon?: ReactNode;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      className={cx(
        "btn",
        variant !== "default" && `btn-${variant}`,
        size && `btn-${size}`,
        block && "btn-block",
        className,
      )}
      disabled={rest.disabled || loading}
      {...rest}
    >
      {loading ? <span className="spinner" /> : icon}
      {children}
    </button>
  );
}

export function IconButton({
  icon,
  label,
  variant,
  size,
  active,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  icon: ReactNode;
  label: string;
  variant?: "ghost" | "primary";
  size?: "sm";
  active?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx("icon-btn", variant, size, active && "active", className)}
      {...rest}
    >
      {icon}
    </button>
  );
}

// --- переключатели и поля -----------------------------------------------------------------------------
export function Toggle({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={cx("toggle", checked && "on")}
      onClick={() => onChange(!checked)}
    />
  );
}

export function Slider({
  value,
  min = 0,
  max = 100,
  step = 1,
  onChange,
  onCommit,
  format = (v) => `${Math.round(v)}%`,
  showValue = true,
}: {
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange?: (v: number) => void;
  onCommit?: (v: number) => void;
  format?: (v: number) => string;
  showValue?: boolean;
}) {
  const [local, setLocal] = useState(value);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) setLocal(value);
  }, [value]);
  const pct = ((local - min) / (max - min)) * 100;
  const commit = () => {
    dragging.current = false;
    onCommit?.(local);
  };
  return (
    <div className="slider-row">
      <input
        type="range"
        className="slider"
        min={min}
        max={max}
        step={step}
        value={local}
        style={{ "--val": `${pct}%` } as React.CSSProperties}
        onPointerDown={() => (dragging.current = true)}
        onChange={(e) => {
          const v = Number(e.target.value);
          setLocal(v);
          onChange?.(v);
        }}
        onPointerUp={commit}
        onKeyUp={commit}
      />
      {showValue && <span className="value">{format(local)}</span>}
    </div>
  );
}

export function Check({
  checked,
  onChange,
  children,
  chip,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  children?: ReactNode;
  chip?: boolean;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      className={cx("check", checked && "on", chip && "check-chip")}
      onClick={() => onChange(!checked)}
    >
      <span className="check-box">
        <CheckIcon strokeWidth={3} />
      </span>
      {children}
    </button>
  );
}

export function TextInput({
  value,
  onChange,
  onCommit,
  placeholder,
  icon,
  type = "text",
  className,
  autoFocus,
  onEnter,
}: {
  value: string;
  onChange: (v: string) => void;
  onCommit?: (v: string) => void;
  placeholder?: string;
  icon?: ReactNode;
  type?: string;
  className?: string;
  autoFocus?: boolean;
  onEnter?: () => void;
}) {
  const input = (
    <input
      className={cx("input", className)}
      type={type}
      value={value}
      placeholder={placeholder}
      autoFocus={autoFocus}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
      onBlur={() => onCommit?.(value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          onCommit?.(value);
          onEnter?.();
        }
      }}
    />
  );
  if (!icon) return input;
  return (
    <div className="input-wrap">
      {icon}
      {input}
    </div>
  );
}

export interface Option<T extends string> {
  value: T;
  label: string;
  hint?: string;
  icon?: ReactNode;
  disabled?: boolean;
}

export function Select<T extends string>({
  value,
  options,
  onChange,
  placeholder = "Выберите…",
  width,
  up,
}: {
  value: T;
  options: Option<T>[];
  onChange: (v: T) => void;
  placeholder?: string;
  width?: number | string;
  up?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const current = options.find((o) => o.value === value);
  return (
    <div className="select" ref={ref} style={{ width }}>
      <button type="button" className={cx("select-trigger", open && "open")} onClick={() => setOpen(!open)}>
        {current?.icon}
        <span className="value">{current?.label ?? <span className="faint">{placeholder}</span>}</span>
        <ChevronDown />
      </button>
      {open && (
        <div className={cx("select-menu", up && "up")} role="listbox">
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === value}
              disabled={o.disabled}
              className={cx("select-option", o.value === value && "selected")}
              style={o.disabled ? { opacity: 0.45 } : undefined}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
            >
              {o.icon}
              <span className="ellipsis">{o.label}</span>
              {o.hint && <span className="hint">{o.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Tabs<T extends string>({
  value,
  items,
  onChange,
  plain,
}: {
  value: T;
  items: { value: T; label: ReactNode; icon?: ReactNode }[];
  onChange: (v: T) => void;
  plain?: boolean;
}) {
  return (
    <div className={cx("tabs", plain && "plain")} role="tablist">
      {items.map((it) => (
        <button
          key={it.value}
          type="button"
          role="tab"
          aria-selected={value === it.value}
          className={cx("tab", value === it.value && "active")}
          onClick={() => onChange(it.value)}
        >
          {it.icon}
          {it.label}
        </button>
      ))}
    </div>
  );
}

// --- настройки ----------------------------------------------------------------------------------------
export function Setting({
  name,
  desc,
  children,
  wide,
}: {
  name: ReactNode;
  desc?: ReactNode;
  children?: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="setting">
      <div className="label">
        <div className="name">{name}</div>
        {desc && <div className="desc">{desc}</div>}
      </div>
      <div className={cx("control", wide && "wide")}>{children}</div>
    </div>
  );
}

export function Card({
  title,
  icon,
  sub,
  actions,
  children,
  className,
  style,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <section className={cx("card", className)} style={style}>
      {(title || actions) && (
        <div className="row" style={{ marginBottom: sub ? 2 : 12 }}>
          {title && (
            <h3 className="card-title" style={{ margin: 0 }}>
              {icon}
              {title}
            </h3>
          )}
          <span className="spacer" />
          {actions}
        </div>
      )}
      {sub && <p className="card-sub" style={{ marginTop: 0 }}>{sub}</p>}
      {children}
    </section>
  );
}

export function Empty({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <div>{children}</div>
    </div>
  );
}

export function Dots() {
  return (
    <span className="dots" aria-hidden>
      <i />
      <i />
      <i />
    </span>
  );
}

// --- модальное окно ----------------------------------------------------------------------------------
export function Modal({
  open,
  title,
  icon,
  onClose,
  children,
  footer,
  width,
}: {
  open: boolean;
  title?: ReactNode;
  icon?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={width ? { width: `min(${width}px, 100%)` } : undefined} role="dialog">
        {title && (
          <div className="modal-head">
            {icon}
            <h3>{title}</h3>
            <span className="spacer" />
            <IconButton icon={<X />} label="Закрыть" variant="ghost" size="sm" onClick={onClose} />
          </div>
        )}
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

// --- всплывающая подсказка об ошибке/успехе внутри окна -------------------------------------------------
type Notice = { id: number; text: string; kind: "ok" | "error" | "info" };
let noticeId = 0;
const noticeListeners = new Set<(n: Notice) => void>();

export function notify(text: string, kind: Notice["kind"] = "info") {
  const n = { id: ++noticeId, text, kind };
  for (const l of noticeListeners) l(n);
}

export function notifyError(e: unknown) {
  notify(e instanceof Error ? e.message : String(e), "error");
}

export function Notices() {
  const [items, setItems] = useState<Notice[]>([]);
  useEffect(() => {
    const add = (n: Notice) => {
      setItems((list) => [...list.slice(-3), n]);
      setTimeout(() => setItems((list) => list.filter((x) => x.id !== n.id)), n.kind === "error" ? 6000 : 3500);
    };
    noticeListeners.add(add);
    return () => {
      noticeListeners.delete(add);
    };
  }, []);
  return (
    <div style={{ position: "fixed", right: 20, bottom: 20, zIndex: 200, display: "flex", flexDirection: "column", gap: 8 }}>
      {items.map((n) => (
        <div
          key={n.id}
          className={cx("chip", n.kind === "error" ? "err" : n.kind === "ok" ? "ok" : "accent", "pop-in")}
          style={{ height: "auto", padding: "9px 14px", fontSize: 13, maxWidth: 380, whiteSpace: "normal", borderRadius: 12 }}
        >
          {n.text}
        </div>
      ))}
    </div>
  );
}

// --- мелочи -------------------------------------------------------------------------------------------
export function useDebounced<T extends (...args: any[]) => void>(fn: T, ms: number): T {
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const latest = useRef(fn);
  latest.current = fn;
  return useCallback(
    ((...args: unknown[]) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => latest.current(...args), ms);
    }) as T,
    [ms],
  );
}

export function useElementWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => setWidth(entries[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function timeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString())
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}
