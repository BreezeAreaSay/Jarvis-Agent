// Общее для виджетов: рамка, загрузка данных, форматирование.
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api, qs } from "@/api/client";
import { onEvent } from "@/api/events";

export interface WidgetProps {
  id: string;
  w: number;
  h: number;
  config: Record<string, unknown>;
  editing: boolean;
  setConfig: (config: Record<string, unknown>) => void;
}

export function WidgetFrame({
  icon,
  title,
  actions,
  children,
  className,
  pad = true,
}: {
  icon?: ReactNode;
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  pad?: boolean;
}) {
  return (
    <div className={`widget ${pad ? "" : "nopad"} ${className ?? ""}`}>
      {(title || actions) && (
        <div className="widget-head">
          {icon}
          <span className="ellipsis">{title}</span>
          <span className="spacer" />
          {actions}
        </div>
      )}
      <div className="widget-body">{children}</div>
    </div>
  );
}

/** Данные виджета с ядра: GET /api/widgets/{kind}, обновление по таймеру и по событиям. */
export function useWidgetData<T>(
  kind: string,
  params: Record<string, string | number | undefined> = {},
  options: { refreshMs?: number; events?: string[] } = {},
): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const key = JSON.stringify(params);
  const tick = useRef(0);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    const my = ++tick.current;
    const load = async () => {
      try {
        const res = await api.get<T & { error?: string }>(`/api/widgets/${kind}${qs(params)}`);
        if (!alive || my !== tick.current) return;
        if (res && typeof res === "object" && "error" in res && res.error) {
          setError(String(res.error));
        } else {
          setError("");
        }
        setData(res);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    load();
    const timer = options.refreshMs ? setInterval(load, options.refreshMs) : undefined;
    const offs = (options.events ?? []).map((ev) => onEvent(ev, () => load()));
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
      for (const off of offs) off();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, key, options.refreshMs, nonce]);

  return { data, error, loading, reload: () => setNonce((n) => n + 1) };
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtDelta(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const s = Math.abs(v).toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${s}`;
}

export function Delta({ value, digits = 2, suffix = "" }: { value: number | null | undefined; digits?: number; suffix?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  const cls = value > 0 ? "up" : value < 0 ? "down" : "flat";
  return (
    <span className={`delta ${cls}`}>
      {fmtDelta(value, digits)}
      {suffix}
    </span>
  );
}

export function Bar({ value, color }: { value: number; color?: string }) {
  return (
    <div className="wbar">
      <i style={{ width: `${Math.max(0, Math.min(100, value))}%`, background: color }} />
    </div>
  );
}
