// Умный дом: устройства из GET /api/widgets/smarthome, переключение через плагин smart_home.
// Виджет «Умный дом» и живая панель плагина на странице «Расширения».
import { AirVent, Blinds, CircleDot, Gauge, Home, Lightbulb, Plug, RefreshCw, Thermometer, Tv } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { navigate } from "@/lib/router";
import { cx, notifyError, Toggle } from "@/ui";
import { useWidgetData, WidgetFrame, type WidgetProps } from "../common";

export interface SmartDevice {
  id: string;
  name: string;
  room: string;
  kind: "light" | "switch" | "climate" | "cover" | "media" | "sensor" | "other";
  state: {
    on?: boolean | null;
    available?: boolean;
    brightness?: number;
    temperature?: number;
    current?: number;
    position?: number;
    value?: string | number;
    unit?: string;
    title?: string;
    volume?: number;
    mode?: string;
  };
  provider: string;
  caps: string[];
}

interface SmartData {
  devices?: SmartDevice[];
  items?: SmartDevice[];
  error?: string;
  updated?: number;
}

/** Действие плагина «Умный дом» (POST /api/plugins/smart_home/action; запасной путь — маршрут виджета). */
export async function smartAction(action: string, args: Record<string, unknown>): Promise<Record<string, unknown>> {
  try {
    return await api.post(`/api/plugins/smart_home/action`, { action, args });
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
      return api.post(`/api/widgets/smarthome/${action}`, args);
    }
    throw e;
  }
}

export function canToggle(d: SmartDevice): boolean {
  return d.state?.available !== false && d.caps.some((c) => c === "on_off" || c === "open" || c === "activate");
}

export function DeviceIcon({ device }: { device: SmartDevice }) {
  switch (device.kind) {
    case "light":
      return <Lightbulb />;
    case "switch":
      return <Plug />;
    case "climate":
      return device.caps.includes("temperature") ? <Thermometer /> : <AirVent />;
    case "cover":
      return <Blinds />;
    case "media":
      return <Tv />;
    case "sensor":
      return <Gauge />;
    default:
      return <CircleDot />;
  }
}

function num(v: unknown, digits = 0): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  return n.toLocaleString("ru-RU", { maximumFractionDigits: digits });
}

export function deviceState(d: SmartDevice): string {
  const s = d.state ?? {};
  if (s.available === false) return "Недоступно";
  switch (d.kind) {
    case "light":
      return s.on ? (typeof s.brightness === "number" ? `${Math.round(s.brightness)}%` : "Вкл") : "Выкл";
    case "climate":
      if (typeof s.temperature === "number") return `${num(s.temperature, 1)}°` + (s.on === false ? " · выкл" : "");
      return s.on ? "Вкл" : "Выкл";
    case "cover":
      if (typeof s.position === "number") return s.position > 0 ? `Открыто ${Math.round(s.position)}%` : "Закрыто";
      return s.on ? "Открыто" : "Закрыто";
    case "media":
      return s.title || (s.on ? "Вкл" : "Выкл");
    case "sensor":
      return `${num(s.value, 1)}${s.unit ? (String(s.unit).startsWith("°") ? "" : " ") + s.unit : ""}`;
    default:
      return s.on === null || s.on === undefined ? "" : s.on ? "Вкл" : "Выкл";
  }
}

/** Список устройств с ядра + оптимистичное переключение. */
export function useSmartDevices(refreshMs = 15000) {
  const { data, error, loading, reload } = useWidgetData<SmartData>("smarthome", {}, { refreshMs });
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  useEffect(() => setOverrides({}), [data]);
  const raw = data?.devices ?? data?.items ?? [];
  const devices = raw.map((d) =>
    d.id in overrides ? { ...d, state: { ...d.state, on: overrides[d.id] } } : d,
  );
  const toggle = async (d: SmartDevice) => {
    const next = !d.state?.on;
    setOverrides((o) => ({ ...o, [d.id]: next }));
    try {
      await smartAction("toggle", { id: d.id });
      setTimeout(reload, 1200);
    } catch (e) {
      setOverrides((o) => {
        const rest = { ...o };
        delete rest[d.id];
        return rest;
      });
      notifyError(e);
    }
  };
  return { devices, error: data?.error || error, loading, reload, toggle };
}

// --- виджет ------------------------------------------------------------------------------------------
export function SmartHomeWidget({ w, h, config }: WidgetProps) {
  const { devices, error, loading, reload, toggle } = useSmartDevices();
  const title = (config.title as string) || "Умный дом";
  const cols = w >= 3 ? 3 : w >= 2 ? 2 : 1;
  const shown = devices.filter((d) => d.kind !== "sensor" || h >= 2 || w >= 2);
  return (
    <WidgetFrame
      icon={<Home />}
      title={title}
      actions={
        <button className="wx-head-btn" onClick={reload} aria-label="Обновить" title="Обновить">
          <RefreshCw />
        </button>
      }
    >
      {error && !devices.length ? (
        <div className="wx-empty">
          <span>{error}</span>
          <button className="wx-link" onClick={() => navigate("extensions", "plugins", "smart_home")}>
            Настроить плагин
          </button>
        </div>
      ) : !devices.length ? (
        <div className="wx-empty">{loading ? "Загружаю устройства…" : "Устройств пока нет"}</div>
      ) : (
        <div className="wx-devices" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
          {shown.map((d) => {
            const on = !!d.state?.on;
            const off = d.state?.available === false;
            return (
              <div key={d.id} className={cx("wx-device", on && "on", off && "na", d.kind)}>
                <span className="wx-device-icon">
                  <DeviceIcon device={d} />
                </span>
                <span className="grow" style={{ minWidth: 0 }}>
                  <span className="wx-device-name ellipsis">{d.name}</span>
                  <span className="wx-device-sub ellipsis">
                    {[d.room, deviceState(d)].filter(Boolean).join(" · ")}
                  </span>
                </span>
                {canToggle(d) && <Toggle checked={on} onChange={() => toggle(d)} label={d.name} />}
              </div>
            );
          })}
        </div>
      )}
    </WidgetFrame>
  );
}
