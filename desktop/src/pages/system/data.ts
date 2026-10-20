// Данные «Системных операций», общие для страницы и мини-панели в чате: нагрузка (виджет system), датчики
// (/api/system/sensors), опрос по таймеру и цвет температуры.
import { useEffect, useState } from "react";
import { api } from "@/api/client";

export interface SysStats {
  cpu: number;
  ram: number;
  ram_used_gb: number;
  ram_total_gb: number;
  gpu: number | null;
  disk: number;
  net_down_kbps: number;
  net_up_kbps: number;
  uptime_s: number;
  extra?: { disk_free_gb?: number; disk_total_gb?: number; cpu_count?: number };
  error?: string;
}

export interface Sensors {
  cpu_temp: number | null;
  gpu_temp: number | null;
  gpu_name: string;
  gpu_load?: number | null;
  top: { name: string; cpu: number; ram_mb: number }[];
}

/** Опрос ручки ядра по таймеру (только пока компонент на экране). */
export function usePoll<T>(path: string, ms: number): { data: T | null; error: string; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<T>(path)
        .then((r) => {
          if (!alive) return;
          setData(r);
          setError("");
        })
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    load();
    const t = window.setInterval(load, ms);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [path, ms, nonce]);
  return { data, error, reload: () => setNonce((n) => n + 1) };
}

/** Цвет по температуре: до 65° — спокойно, до 85° — тепло, выше — горячо. */
export function heat(t: number | null | undefined): "ok" | "warm" | "hot" | "" {
  if (t === null || t === undefined) return "";
  return t < 65 ? "ok" : t < 85 ? "warm" : "hot";
}
