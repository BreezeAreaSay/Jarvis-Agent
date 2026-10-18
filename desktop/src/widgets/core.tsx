// Основные виджеты главной: аватар, панель управления, история, «Сейчас играет», погода, задачи,
// курсы валют, часы, система, календарь.
import {
  Calendar as CalendarIcon,
  Check,
  CircleAlert,
  Cloud,
  CloudDrizzle,
  CloudFog,
  CloudLightning,
  CloudRain,
  CloudSnow,
  CloudSun,
  Coins,
  Cpu,
  History,
  ListChecks,
  Moon,
  Music2,
  Pause,
  Play,
  Plus,
  SkipBack,
  SkipForward,
  SlidersHorizontal,
  Sun,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { EventItem, HistoryItem, MediaInfo, TaskItem } from "@/api/types";
import { Orb } from "@/components/Orb";
import { useApp } from "@/store/app";
import { cx, notifyError, Toggle } from "@/ui";
import { Bar, Delta, fmtNum, useWidgetData, WidgetFrame, type WidgetProps } from "./common";

// --- аватар -------------------------------------------------------------------------------------------
const STATE_TEXT: Record<string, string> = {
  off: "Микрофон выключен",
  loading: "Готовлю голосовые модели…",
  idle: "Скажите «Джарвис»",
  listening: "Слушаю…",
  processing: "Думаю…",
  speaking: "Говорю…",
  error: "Голос недоступен",
};

export function AvatarWidget({ h }: WidgetProps) {
  const state = useApp((s) => s.voiceState);
  const reason = useApp((s) => s.voiceReason);
  const wake = useApp((s) => s.settings?.profile.assistant_name ?? "Джарвис");
  const text = state === "idle" ? `Скажите «${wake}»` : reason || STATE_TEXT[state] || "";
  return (
    <div className="widget bare" style={{ position: "relative", padding: 0 }}>
      <Orb
        scale={h >= 3 ? 0.44 : 0.4}
        onClick={() => api.post("/api/voice/listen").catch(notifyError)}
        title="Нажмите, чтобы сказать команду"
      />
      <div className="avatar-status">{text}</div>
    </div>
  );
}

// --- панель управления ---------------------------------------------------------------------------------
export function ControlWidget() {
  const s = useApp((st) => st.settings);
  const patch = useApp((st) => st.patchSettings);
  if (!s) return null;
  const rows: { label: string; on: boolean; set: (v: boolean) => void }[] = [
    { label: "Микрофон", on: s.voice.enabled, set: (v) => patch({ voice: { enabled: v } }) },
    { label: "Режим префикса", on: s.voice.prefix_mode, set: (v) => patch({ voice: { prefix_mode: v } }) },
    { label: "Тихий режим", on: s.voice.silent_mode, set: (v) => patch({ voice: { silent_mode: v } }) },
    { label: "Анимация аватара", on: s.avatar.animated, set: (v) => patch({ avatar: { animated: v } }) },
    { label: "Аватар на рабочем столе", on: s.avatar.desktop, set: (v) => patch({ avatar: { desktop: v } }) },
  ];
  return (
    <WidgetFrame icon={<SlidersHorizontal />} title="Панель управления">
      <div className="col" style={{ gap: 0, overflow: "auto" }}>
        {rows.map((r) => (
          <div key={r.label} className={cx("ctl-row", r.on && "on")}>
            <span className="ellipsis">{r.label}</span>
            <Toggle checked={r.on} onChange={(v) => r.set(v)} label={r.label} />
          </div>
        ))}
      </div>
    </WidgetFrame>
  );
}

// --- история команд --------------------------------------------------------------------------------------
function statusText(h: HistoryItem): { text: string; ok: boolean | null } {
  if (h.status === "pending") return { text: "Выполняется…", ok: null };
  if (h.status === "ok") return { text: h.kind === "ai" ? "Ответ Jarvis AI" : "Выполнено", ok: true };
  if (h.status === "cancelled") return { text: "Отменено", ok: false };
  return { text: h.detail || "Ошибка", ok: false };
}

export function HistoryWidget() {
  const history = useApp((s) => s.history);
  return (
    <WidgetFrame icon={<History />} title="История команд">
      {history.length === 0 ? (
        <div className="empty" style={{ padding: 8 }}>
          Скажите «Джарвис, сверни все окна» — команда появится здесь
        </div>
      ) : (
        <div className="wlist" style={{ margin: 0, gap: 4 }}>
          {history.slice(0, 12).map((h) => {
            const st = statusText(h);
            return (
              <div key={h.id} className="hist-item">
                <div className="ellipsis" style={{ fontSize: 13 }}>
                  {h.text}
                </div>
                <div className="status">
                  {st.text}
                  {st.ok === true && <Check style={{ color: "var(--ok)" }} />}
                  {st.ok === false && <CircleAlert style={{ color: "var(--err)" }} />}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </WidgetFrame>
  );
}

// --- сейчас играет ---------------------------------------------------------------------------------------
export function MediaWidget({ h }: WidgetProps) {
  const { data } = useWidgetData<MediaInfo & { error?: string }>("media", {}, { refreshMs: 5000 });
  const [live, setLive] = useState<MediaInfo | null>(null);
  useEffect(() => onEvent<MediaInfo>("media.changed", (e) => setLive(e.data)), []);
  const media = live ?? data;
  const [pos, setPos] = useState(0);
  useEffect(() => {
    if (!media) return;
    setPos(media.position_s);
    if (media.status !== "playing") return;
    const t = setInterval(() => setPos((p) => Math.min(media.duration_s || p + 1, p + 1)), 1000);
    return () => clearInterval(t);
  }, [media?.title, media?.status, media?.position_s, media?.duration_s]);
  const act = (a: string) => api.post(`/api/media/${a}`).catch(notifyError);
  const has = !!media?.title;
  const playing = media?.status === "playing";
  return (
    <WidgetFrame icon={<Music2 />} title="Сейчас играет">
      <div className="row" style={{ gap: 12, flex: 1 }}>
        <div className="media-art">{media?.thumbnail ? <img src={media.thumbnail} alt="" /> : <Music2 />}</div>
        <div className="grow col" style={{ gap: 5 }}>
          <div className="ellipsis" style={{ fontWeight: 700, fontSize: 15 }}>
            {has ? media!.title : "Ничего не играет"}
          </div>
          <div className="ellipsis sub">{has ? media!.artist || media!.app : "Скажите «включи музыку»"}</div>
          {has && media!.duration_s > 0 && <Bar value={(pos / media!.duration_s) * 100} color="#fff" />}
        </div>
        {h >= 1 && (
          <div className="row" style={{ gap: 4 }}>
            <button className="icon-btn ghost sm" onClick={() => act("prev")} aria-label="Назад">
              <SkipBack />
            </button>
            <button className="play-btn" onClick={() => act("play_pause")} aria-label={playing ? "Пауза" : "Играть"}>
              {playing ? <Pause /> : <Play />}
            </button>
            <button className="icon-btn ghost sm" onClick={() => act("next")} aria-label="Дальше">
              <SkipForward />
            </button>
          </div>
        )}
      </div>
    </WidgetFrame>
  );
}

// --- погода --------------------------------------------------------------------------------------------
interface Weather {
  city: string;
  temp: number;
  feels: number;
  condition: string;
  icon: string;
  is_day?: boolean | number;
  daily?: { date: string; min: number; max: number; icon: string; condition: string }[];
  error?: string;
}

export function WeatherIcon({ icon, size = 30, day = true }: { icon?: string; size?: number; day?: boolean }) {
  const style = { width: size, height: size };
  switch (icon) {
    case "sun":
      return day ? <Sun style={{ ...style, color: "#FBBF24" }} /> : <Moon style={{ ...style, color: "#C7D2FE" }} />;
    case "partly":
      return <CloudSun style={{ ...style, color: "#FCD34D" }} />;
    case "cloud":
      return <Cloud style={{ ...style, color: "#CBD5E1" }} />;
    case "rain":
      return <CloudRain style={{ ...style, color: "#93C5FD" }} />;
    case "drizzle":
      return <CloudDrizzle style={{ ...style, color: "#93C5FD" }} />;
    case "snow":
      return <CloudSnow style={{ ...style, color: "#E0F2FE" }} />;
    case "storm":
      return <CloudLightning style={{ ...style, color: "#FDE68A" }} />;
    case "fog":
      return <CloudFog style={{ ...style, color: "#CBD5E1" }} />;
    default:
      return <CloudSun style={{ ...style, color: "#FCD34D" }} />;
  }
}

function signed(t: number): string {
  const r = Math.round(t);
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r)}°`;
}

export function WeatherWidget({ w, h, config }: WidgetProps) {
  const city = (config.city as string) || undefined;
  const { data, error } = useWidgetData<Weather>("weather", { city }, { refreshMs: 10 * 60_000 });
  return (
    <WidgetFrame icon={<Sun />} title="Погода">
      {!data || error ? (
        <div className="sub">{error || "Загружаю…"}</div>
      ) : (
        <div className="col" style={{ gap: 6, flex: 1 }}>
          <div className="row" style={{ gap: 10 }}>
            <WeatherIcon icon={data.icon} day={data.is_day !== 0 && data.is_day !== false} />
            <div className="big">{signed(data.temp)}</div>
          </div>
          <div className="sub ellipsis">
            {data.city} · {data.condition}
          </div>
          {(w >= 2 || h >= 2) && data.daily && (
            <div className="row" style={{ gap: 6, marginTop: "auto", justifyContent: "space-between" }}>
              {data.daily.slice(0, w >= 2 ? 5 : 3).map((d) => (
                <div key={d.date} className="col" style={{ gap: 2, alignItems: "center", fontSize: 11.5 }}>
                  <span className="faint">
                    {new Date(d.date).toLocaleDateString("ru-RU", { weekday: "short" })}
                  </span>
                  <WeatherIcon icon={d.icon} size={18} />
                  <span>{Math.round(d.max)}°</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </WidgetFrame>
  );
}

// --- задачи ----------------------------------------------------------------------------------------------
export function TasksWidget() {
  const [items, setItems] = useState<TaskItem[] | null>(null);
  const [adding, setAdding] = useState("");
  const load = () =>
    api
      .get<{ items: TaskItem[] }>("/api/tasks")
      .then((r) => setItems(r.items))
      .catch(() => setItems([]));
  useEffect(() => {
    load();
    return onEvent("tasks.changed", () => load());
  }, []);
  const toggle = async (t: TaskItem) => {
    setItems((list) => list?.map((x) => (x.id === t.id ? { ...x, done: !x.done } : x)) ?? null);
    await api.patch(`/api/tasks/${t.id}`, { done: !t.done }).catch(notifyError);
  };
  const add = async () => {
    const title = adding.trim();
    if (!title) return;
    setAdding("");
    await api.post("/api/tasks", { title }).catch(notifyError);
    load();
  };
  const sorted = useMemo(
    () => [...(items ?? [])].sort((a, b) => Number(!!a.done) - Number(!!b.done) || a.position - b.position),
    [items],
  );
  return (
    <WidgetFrame icon={<ListChecks />} title="Задачи">
      <div className="wlist">
        {sorted.map((t) => (
          <button key={t.id} className={cx("witem wtask check", !!t.done && "done on")} onClick={() => toggle(t)}>
            <span className="check-box">
              <Check strokeWidth={3} />
            </span>
            <span className="title ellipsis">{t.title}</span>
          </button>
        ))}
        {items && items.length === 0 && <div className="sub" style={{ padding: "4px 6px" }}>Задач нет</div>}
      </div>
      <div className="row" style={{ marginTop: "auto", paddingTop: 6, gap: 6 }}>
        <Plus style={{ width: 14, height: 14, color: "var(--text-3)" }} />
        <input
          className="grow"
          style={{ background: "transparent", border: 0, outline: "none", fontSize: 13 }}
          placeholder="Новая задача"
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
      </div>
    </WidgetFrame>
  );
}

// --- курсы валют -----------------------------------------------------------------------------------------
interface Rates {
  items?: { code: string; value: number; delta: number }[];
  error?: string;
}

export function RatesWidget({ h }: WidgetProps) {
  const { data, error } = useWidgetData<Rates>("rates", {}, { refreshMs: 30 * 60_000 });
  const items = data?.items ?? [];
  return (
    <WidgetFrame icon={<Coins />} title="Курсы валют">
      {error && !items.length ? (
        <div className="sub">{error}</div>
      ) : (
        <div className="col" style={{ gap: 2 }}>
          {items.slice(0, Math.max(2, h * 2 + 1)).map((r) => (
            <div key={r.code} className="wrate">
              <span className="code">{r.code}</span>
              <span className="val">{fmtNum(r.value)}</span>
              <Delta value={r.delta} />
            </div>
          ))}
        </div>
      )}
    </WidgetFrame>
  );
}

// --- часы -----------------------------------------------------------------------------------------------
export function ClockWidget({ w }: WidgetProps) {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="widget center" style={{ flexDirection: "column", gap: 4 }}>
      <div className="big" style={{ fontSize: w >= 2 ? 44 : 34, fontVariantNumeric: "tabular-nums" }}>
        {now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
      </div>
      <div className="sub">{now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</div>
    </div>
  );
}

// --- система --------------------------------------------------------------------------------------------
interface Stats {
  cpu: number;
  ram: number;
  gpu: number | null;
  disk: number;
  error?: string;
}

export function SystemWidget() {
  const { data, error } = useWidgetData<Stats>("system", {}, { refreshMs: 2500 });
  const rows = data
    ? [
        { label: "Процессор", v: data.cpu },
        ...(data.gpu !== null && data.gpu !== undefined ? [{ label: "Видеокарта", v: data.gpu }] : []),
        { label: "Память", v: data.ram },
      ]
    : [];
  return (
    <WidgetFrame icon={<Cpu />} title="Система">
      {error && !data ? (
        <div className="sub">{error}</div>
      ) : (
        <div className="col" style={{ gap: 9 }}>
          {rows.map((r) => (
            <div key={r.label} className="col" style={{ gap: 4 }}>
              <div className="row" style={{ fontSize: 12.5 }}>
                <span className="muted">{r.label}</span>
                <span className="spacer" />
                <span style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round(r.v)}%</span>
              </div>
              <Bar value={r.v} />
            </div>
          ))}
        </div>
      )}
    </WidgetFrame>
  );
}

// --- календарь ------------------------------------------------------------------------------------------
export function CalendarWidget() {
  const [events, setEvents] = useState<EventItem[]>([]);
  const today = new Date();
  const [month] = useState(new Date(today.getFullYear(), today.getMonth(), 1));
  useEffect(() => {
    const start = month.getTime() / 1000;
    const end = new Date(month.getFullYear(), month.getMonth() + 1, 1).getTime() / 1000;
    const load = () =>
      api
        .get<{ items: EventItem[] }>(`/api/events?start=${start}&end=${end}`)
        .then((r) => setEvents(r.items))
        .catch(() => setEvents([]));
    load();
    return onEvent("events.changed", () => load());
  }, [month]);
  const days: { d: Date; other: boolean }[] = [];
  const first = (month.getDay() + 6) % 7;
  for (let i = -first; i < 42 - first; i++) {
    const d = new Date(month.getFullYear(), month.getMonth(), 1 + i);
    days.push({ d, other: d.getMonth() !== month.getMonth() });
    if (i >= 27 && d.getDay() === 0 && d.getMonth() !== month.getMonth()) break;
  }
  const marked = new Set(events.map((e) => new Date(e.start * 1000).toDateString()));
  return (
    <WidgetFrame
      icon={<CalendarIcon />}
      title={month.toLocaleDateString("ru-RU", { month: "long", year: "numeric" })}
    >
      <div className="cal">
        {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((d) => (
          <span key={d} className="dow">
            {d}
          </span>
        ))}
        {days.map(({ d, other }) => (
          <span
            key={d.toISOString()}
            className={cx(
              "day",
              other && "other",
              d.toDateString() === today.toDateString() && "today",
              d.toDateString() !== today.toDateString() && marked.has(d.toDateString()) && "event",
            )}
          >
            {d.getDate()}
          </span>
        ))}
      </div>
    </WidgetFrame>
  );
}
