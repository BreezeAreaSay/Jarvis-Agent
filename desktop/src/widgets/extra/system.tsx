// Компьютер: громкость (ползунок и «без звука»), сеть (скорость и график), батарея, активность
// (сколько команд выполнено сегодня — по истории).
import {
  Activity,
  ArrowDown,
  ArrowUp,
  BatteryCharging,
  BatteryFull,
  BatteryLow,
  BatteryMedium,
  BatteryWarning,
  Minus,
  Plug,
  Plus,
  Volume1,
  Volume2,
  VolumeX,
  Wifi,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { HistoryItem } from "@/api/types";
import { cx, notifyError, Slider } from "@/ui";
import { useWidgetData, WidgetFrame, type WidgetProps } from "../common";
import { HeadButton, plural } from "./shared";

// --- громкость ------------------------------------------------------------------------------------------
interface VolumeState {
  volume: number;
  muted: boolean;
  error?: string;
}

async function mediaCall(action: string, body: Record<string, unknown> = {}): Promise<VolumeState | null> {
  try {
    const res = await api.post<Partial<VolumeState>>(`/api/media/${action}`, body);
    return typeof res?.volume === "number" ? { volume: res.volume, muted: !!res.muted } : null;
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
      const text =
        action === "volume" ? `громкость на ${body.level}` : action === "mute" ? "выключи звук" : action === "volume_up" ? "громче" : "тише";
      await api.post("/api/dispatch", { text, source: "ui" });
      return null;
    }
    throw e;
  }
}

export function VolumeWidget({ w, config }: WidgetProps) {
  const { data, error, reload } = useWidgetData<VolumeState>("volume", {}, { refreshMs: 6000 });
  const [local, setLocal] = useState<VolumeState | null>(null);
  useEffect(() => setLocal(null), [data]);
  const v = local ?? (data && typeof data.volume === "number" ? data : null);
  const muted = !!v?.muted;
  const level = v?.volume ?? 0;

  const apply = async (action: string, body: Record<string, unknown> = {}, optimistic?: Partial<VolumeState>) => {
    if (optimistic && v) setLocal({ ...v, ...optimistic });
    try {
      const res = await mediaCall(action, body);
      if (res) setLocal(res);
      else reload();
    } catch (e) {
      setLocal(null);
      notifyError(e);
    }
  };

  const Icon = muted || level === 0 ? VolumeX : level < 45 ? Volume1 : Volume2;
  return (
    <WidgetFrame
      icon={<Icon />}
      title={(config.title as string) || "Громкость"}
      actions={
        <HeadButton
          icon={muted ? <VolumeX /> : <Volume2 />}
          label={muted ? "Включить звук" : "Выключить звук"}
          onClick={() => apply("mute", {}, { muted: !muted })}
        />
      }
    >
      {!v ? (
        <div className="sub">{error || data?.error || "Загружаю…"}</div>
      ) : (
        <div className={cx("wx-volume", muted && "muted")}>
          {w >= 2 && (
            <button className="icon-btn ghost sm" aria-label="Тише" onClick={() => apply("volume_down", { step: 10 }, { volume: Math.max(0, level - 10) })}>
              <Minus />
            </button>
          )}
          <div className="grow">
            <Slider
              value={level}
              onCommit={(x) => apply("volume", { level: Math.round(x) }, { volume: Math.round(x), muted: false })}
              format={(x) => (muted ? "выкл" : `${Math.round(x)}%`)}
            />
          </div>
          {w >= 2 && (
            <button className="icon-btn ghost sm" aria-label="Громче" onClick={() => apply("volume_up", { step: 10 }, { volume: Math.min(100, level + 10), muted: false })}>
              <Plus />
            </button>
          )}
        </div>
      )}
    </WidgetFrame>
  );
}

// --- сеть и батарея ------------------------------------------------------------------------------------
interface Stats {
  net_down_kbps?: number;
  net_up_kbps?: number;
  battery?: number | null;
  charging?: boolean | null;
  error?: string;
}

export function speed(kbps: number | undefined): [string, string] {
  const v = Math.max(0, kbps ?? 0);
  if (v >= 1000) return [(v / 1000).toLocaleString("ru-RU", { maximumFractionDigits: v >= 100_000 ? 0 : 1 }), "Мбит/с"];
  return [Math.round(v).toLocaleString("ru-RU"), "Кбит/с"];
}

function Spark({ values, max, className }: { values: number[]; max: number; className?: string }) {
  if (values.length < 2) return null;
  const W = 100;
  const H = 30;
  const step = W / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(H - (v / max) * (H - 3) - 1.5).toFixed(1)}`);
  return (
    <svg className={cx("wx-spark", className)} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <polyline points={`0,${H} ${pts.join(" ")} ${W},${H}`} className="area" />
      <polyline points={pts.join(" ")} className="line" />
    </svg>
  );
}

export function NetworkWidget({ w, h, config }: WidgetProps) {
  const { data, error } = useWidgetData<Stats>("system", {}, { refreshMs: 2000 });
  const [hist, setHist] = useState<{ d: number[]; u: number[] }>({ d: [], u: [] });
  const last = useRef<unknown>(null);
  useEffect(() => {
    if (!data || data === last.current || typeof data.net_down_kbps !== "number") return;
    last.current = data;
    setHist((x) => ({ d: [...x.d, data.net_down_kbps ?? 0].slice(-40), u: [...x.u, data.net_up_kbps ?? 0].slice(-40) }));
  }, [data]);
  const [dv, du] = speed(data?.net_down_kbps);
  const [uv, uu] = speed(data?.net_up_kbps);
  const max = Math.max(1, ...hist.d, ...hist.u) * 1.15;
  return (
    <WidgetFrame icon={<Wifi />} title={(config.title as string) || "Сеть"}>
      {!data || typeof data.net_down_kbps !== "number" ? (
        <div className="sub">{error || data?.error || "Измеряю…"}</div>
      ) : (
        <div className={cx("wx-net", (w >= 2 || h >= 2) && "roomy")}>
          <div className="wx-net-nums">
            <span className="wx-net-v down">
              <ArrowDown />
              <b>{dv}</b> <small>{du}</small>
            </span>
            <span className="wx-net-v up">
              <ArrowUp />
              <b>{uv}</b> <small>{uu}</small>
            </span>
          </div>
          <div className="wx-net-chart">
            <Spark values={hist.d} max={max} className="down" />
            <Spark values={hist.u} max={max} className="up" />
          </div>
        </div>
      )}
    </WidgetFrame>
  );
}

export function BatteryWidget({ config }: WidgetProps) {
  const { data, error } = useWidgetData<Stats>("system", {}, { refreshMs: 15000 });
  const b = data?.battery;
  const has = typeof b === "number";
  const charging = !!data?.charging;
  const Icon = !has ? Plug : charging ? BatteryCharging : b! >= 80 ? BatteryFull : b! >= 35 ? BatteryMedium : b! >= 15 ? BatteryLow : BatteryWarning;
  const level = has ? Math.round(b!) : 0;
  const tone = !has ? "" : charging ? "charge" : level < 15 ? "low" : level < 35 ? "mid" : "ok";
  return (
    <WidgetFrame icon={<Icon />} title={(config.title as string) || "Батарея"}>
      {!data ? (
        <div className="sub">{error || "Загружаю…"}</div>
      ) : !has ? (
        <div className="wx-battery none">
          <span className="big" style={{ fontSize: 20 }}>Батареи нет</span>
          <span className="sub">Компьютер работает от сети</span>
        </div>
      ) : (
        <div className={cx("wx-battery", tone)}>
          <div className="row" style={{ gap: 10, alignItems: "baseline" }}>
            <span className="big">{level}%</span>
            <span className="sub">{charging ? "Заряжается" : level < 15 ? "Пора зарядить" : "От батареи"}</span>
          </div>
          <div className="wx-batt">
            <i style={{ width: `${Math.max(3, level)}%` }} />
          </div>
        </div>
      )}
    </WidgetFrame>
  );
}

// --- активность -----------------------------------------------------------------------------------------
function startOfDay(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

export function ActivityWidget({ w, h, config }: WidgetProps) {
  const [items, setItems] = useState<HistoryItem[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<{ items: HistoryItem[] }>("/api/history?limit=300")
        .then((r) => alive && (setItems(r.items ?? []), setError("")))
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    load();
    const offs = [
      onEvent<{ item: HistoryItem }>("history.added", (e) => setItems((l) => (l ? [e.data.item, ...l].slice(0, 400) : l))),
      onEvent<{ item: HistoryItem }>("history.updated", (e) => setItems((l) => l?.map((x) => (x.id === e.data.item.id ? e.data.item : x)) ?? l)),
      onEvent("history.cleared", () => setItems([])),
    ];
    const midnight = setInterval(load, 30 * 60_000);
    return () => {
      alive = false;
      clearInterval(midnight);
      offs.forEach((off) => off());
    };
  }, []);

  const today = useMemo(() => {
    const from = startOfDay();
    return (items ?? []).filter((x) => x.ts >= from && x.kind !== "system" && x.kind !== "confirm");
  }, [items]);
  const ok = today.filter((x) => x.status === "ok").length;
  const hours = useMemo(() => {
    const arr = Array.from({ length: 24 }, () => 0);
    for (const x of today) arr[new Date(x.ts * 1000).getHours()]++;
    return arr;
  }, [today]);
  const nowH = new Date().getHours();
  const max = Math.max(1, ...hours);
  const top = useMemo(() => {
    const m = new Map<string, number>();
    for (const x of today) {
      const k = x.title || x.text;
      if (k) m.set(k, (m.get(k) ?? 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
  }, [today]);

  return (
    <WidgetFrame icon={<Activity />} title={(config.title as string) || "Активность"}>
      {!items ? (
        <div className="sub">{error || "Считаю…"}</div>
      ) : (
        <div className={cx("wx-activity", h >= 2 && "tall")}>
          <div className="wx-act-main">
            <div className="wx-act-num">
              <span className="big">{today.length}</span>
              <span className="sub">
                {plural(today.length, "команда", "команды", "команд")} сегодня
                {today.length > 0 && w >= 2 ? ` · ${Math.round((ok / today.length) * 100)}% успешно` : ""}
              </span>
            </div>
            <div className="wx-hours" title="По часам">
              {hours.map((n, i) => (
                <i key={i} className={cx(i === nowH && "now", n > 0 && "has")} style={{ height: `${Math.max(8, (n / max) * 100)}%` }} />
              ))}
            </div>
          </div>
          {h >= 2 && top.length > 0 && (
            <div className="wx-act-top">
              {top.map(([k, n]) => (
                <div key={k} className="row" style={{ gap: 8 }}>
                  <span className="grow ellipsis">{k}</span>
                  <span className="faint tiny">×{n}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </WidgetFrame>
  );
}
