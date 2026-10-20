// «Системные операции» (#/system): нагрузка (процессор, видеокарта, память — кольца с температурой, график,
// диск, сеть), погода, микшер громкости по программам, «Сейчас играет» с кнопками, самые загруженные программы
// и «Инструменты» — таймеры, скриншот, заметки одним нажатием. Голосом: «Джарвис, открой системные операции».
// Данные: виджеты ядра (system, weather, media) и /api/system/* (датчики и микшер — docs/CONTRACTS.md §5).
import {
  Activity,
  AlarmClock,
  ArrowDown,
  ArrowUp,
  Camera,
  Cpu,
  Gauge,
  HardDrive,
  ListTodo,
  Minimize2,
  MoonStar,
  Music2,
  NotebookPen,
  Pause,
  Play,
  SkipBack,
  SkipForward,
  SlidersVertical,
  Sparkles,
  Thermometer,
  Timer,
  Volume1,
  Volume2,
  VolumeX,
  Wrench,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { MediaInfo } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { LogoMark } from "@/components/Orb";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { cx, notify, notifyError, Slider } from "@/ui";
import { useWidgetData } from "@/widgets/common";
import { WeatherIcon } from "@/widgets/core";
import { speed } from "@/widgets/extra/system";
import { heat, type Sensors, type SysStats, usePoll } from "./data";
import "@/styles/system.css";

// --- данные --------------------------------------------------------------------------------------------
interface Mixer {
  master: { volume: number; muted: boolean };
  items: { app: string; title: string; level: number; muted: boolean; active: boolean }[];
}

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

function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} д ${h} ч`;
  if (h) return `${h} ч ${m} мин`;
  return `${m} мин`;
}

function signed(t: number): string {
  const r = Math.round(t);
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r)}°`;
}

/** «chrome.exe» → «Chrome», «Code.exe» → «Code». */
function procName(name: string): string {
  const base = name.replace(/\.exe$/i, "");
  return base.charAt(0).toUpperCase() + base.slice(1);
}

// --- общие части ------------------------------------------------------------------------------------------
function Panel({
  icon,
  title,
  aside,
  className,
  children,
}: {
  icon: ReactNode;
  title: string;
  aside?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={cx("sys-card", className)}>
      <header className="sys-card-head">
        <span className="sys-card-icon">{icon}</span>
        <h2>{title}</h2>
        <span className="spacer" />
        {aside}
      </header>
      {children}
    </section>
  );
}

/** Кольцо загрузки: процент в центре, подпись и температура под ним. */
export function GaugeRing({
  value,
  label,
  sub,
  temp,
  size = 116,
}: {
  value: number | null;
  label: string;
  sub?: string;
  temp?: number | null;
  size?: number;
}) {
  const r = 46;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(100, value ?? 0));
  const level = v >= 90 ? "hot" : v >= 70 ? "warm" : "";
  return (
    <div className={cx("sys-gauge", level)} style={{ width: size }}>
      <svg viewBox="0 0 120 120" width={size} height={size} aria-hidden>
        <defs>
          <linearGradient id={`g-${label}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--gauge-a)" />
            <stop offset="100%" stopColor="var(--gauge-b)" />
          </linearGradient>
        </defs>
        <circle cx="60" cy="60" r={r} className="sys-gauge-track" />
        <circle
          cx="60"
          cy="60"
          r={r}
          className="sys-gauge-value"
          stroke={`url(#g-${label})`}
          strokeDasharray={`${(c * v) / 100} ${c}`}
          transform="rotate(-90 60 60)"
        />
      </svg>
      <div className="sys-gauge-center">
        <span className="sys-gauge-num">{value === null ? "—" : Math.round(v)}</span>
        {value !== null && <span className="sys-gauge-pct">%</span>}
      </div>
      <div className="sys-gauge-label">{label}</div>
      {sub && <div className="sys-gauge-sub ellipsis">{sub}</div>}
      {temp !== null && temp !== undefined && (
        <div className={cx("sys-temp", heat(temp))}>
          <Thermometer />
          {Math.round(temp)}°C
        </div>
      )}
    </div>
  );
}

/** График нагрузки за последние минуты (процессор и видеокарта). */
function History({ cpu, gpu }: { cpu: number[]; gpu: number[] }) {
  if (cpu.length < 2) return <div className="sys-history empty" />;
  const W = 300;
  const H = 64;
  const line = (values: number[]) => {
    const step = W / (Math.max(values.length, 2) - 1);
    return values.map((v, i) => `${(i * step).toFixed(1)},${(H - (v / 100) * (H - 4) - 2).toFixed(1)}`).join(" ");
  };
  return (
    <svg className="sys-history" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id="sys-hist-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgba(var(--accent-rgb), 0.35)" />
          <stop offset="100%" stopColor="rgba(var(--accent-rgb), 0)" />
        </linearGradient>
      </defs>
      {[25, 50, 75].map((y) => (
        <line key={y} x1="0" x2={W} y1={H - (y / 100) * H} y2={H - (y / 100) * H} className="grid" />
      ))}
      <polygon points={`0,${H} ${line(cpu)} ${W},${H}`} fill="url(#sys-hist-fill)" />
      {gpu.length > 1 && <polyline points={line(gpu)} className="gpu" />}
      <polyline points={line(cpu)} className="cpu" />
    </svg>
  );
}

// --- нагрузка -------------------------------------------------------------------------------------------
function LoadCard({ stats, sensors }: { stats: SysStats | null; sensors: Sensors | null }) {
  const hist = useRef<{ cpu: number[]; gpu: number[] }>({ cpu: [], gpu: [] });
  const [, bump] = useState(0);
  useEffect(() => {
    if (!stats || stats.error) return;
    const h = hist.current;
    h.cpu = [...h.cpu, stats.cpu].slice(-60);
    if (stats.gpu !== null && stats.gpu !== undefined) h.gpu = [...h.gpu, stats.gpu].slice(-60);
    bump((n) => n + 1);
  }, [stats]);

  const gpu = stats?.gpu ?? sensors?.gpu_load ?? null;
  const diskFree = stats?.extra?.disk_free_gb;
  const [down, downUnit] = speed(stats?.net_down_kbps);
  const [up, upUnit] = speed(stats?.net_up_kbps);
  return (
    <Panel
      icon={<Gauge />}
      title="Нагрузка"
      className="sys-load"
      aside={stats && !stats.error ? <span className="sys-live">в реальном времени</span> : undefined}
    >
      {!stats || stats.error ? (
        <div className="sys-note">{stats?.error || "Загружаю…"}</div>
      ) : (
        <>
          <div className="sys-gauges">
            <GaugeRing value={stats.cpu} label="Процессор" temp={sensors?.cpu_temp} sub={stats.extra?.cpu_count ? `${stats.extra.cpu_count} потоков` : undefined} />
            <GaugeRing value={gpu} label="Видеокарта" temp={sensors?.gpu_temp} sub={sensors?.gpu_name?.replace(/^NVIDIA\s+/i, "") || undefined} />
            <GaugeRing
              value={stats.ram}
              label="Память"
              sub={stats.ram_total_gb ? `${stats.ram_used_gb.toFixed(1)} из ${Math.round(stats.ram_total_gb)} ГБ` : undefined}
            />
          </div>
          <div className="sys-history-box">
            <div className="sys-history-legend">
              <span className="cpu">Процессор</span>
              {hist.current.gpu.length > 1 && <span className="gpu">Видеокарта</span>}
              <span className="spacer" />
              <span className="faint">последние 2 минуты</span>
            </div>
            <History cpu={hist.current.cpu} gpu={hist.current.gpu} />
          </div>
          <div className="sys-facts">
            <div className="sys-fact">
              <HardDrive />
              <span className="grow">
                <span className="sys-fact-row">
                  <span>Диск</span>
                  <b>{Math.round(stats.disk)}%</b>
                </span>
                <span className="sys-bar">
                  <i style={{ width: `${Math.min(100, stats.disk)}%` }} className={cx(stats.disk > 90 && "hot")} />
                </span>
                {diskFree !== undefined && <span className="sys-fact-sub">свободно {Math.round(diskFree)} ГБ</span>}
              </span>
            </div>
            <div className="sys-fact">
              <Activity />
              <span className="grow">
                <span className="sys-fact-row">
                  <span>Сеть</span>
                </span>
                <span className="sys-net">
                  <span>
                    <ArrowDown /> {down} <small>{downUnit}</small>
                  </span>
                  <span>
                    <ArrowUp /> {up} <small>{upUnit}</small>
                  </span>
                </span>
              </span>
            </div>
            <div className="sys-fact">
              <Timer />
              <span className="grow">
                <span className="sys-fact-row">
                  <span>Без перезагрузки</span>
                </span>
                <span className="sys-fact-big">{fmtUptime(stats.uptime_s)}</span>
              </span>
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}

// --- погода ---------------------------------------------------------------------------------------------
function WeatherCard() {
  const { data, error } = useWidgetData<Weather>("weather", {}, { refreshMs: 10 * 60_000 });
  const day = data?.is_day !== 0 && data?.is_day !== false;
  return (
    <Panel icon={<WeatherIcon icon="partly" size={16} />} title="Погода" className="sys-weather">
      {!data || error || data.error ? (
        <div className="sys-note">{error || data?.error || "Загружаю…"}</div>
      ) : (
        <>
          <div className="sys-weather-now">
            <WeatherIcon icon={data.icon} size={54} day={day} />
            <div>
              <div className="sys-weather-temp">{signed(data.temp)}C</div>
              <div className="sys-weather-sub">
                {data.city} · {data.condition}
              </div>
              <div className="sys-weather-feels">ощущается как {signed(data.feels)}</div>
            </div>
          </div>
          {data.daily && (
            <div className="sys-forecast">
              {data.daily.slice(0, 5).map((d, i) => (
                <div key={d.date} className="sys-forecast-day">
                  <span className="faint">{i === 0 ? "Сегодня" : new Date(d.date).toLocaleDateString("ru-RU", { weekday: "short" })}</span>
                  <WeatherIcon icon={d.icon} size={22} />
                  <span>
                    <b>{Math.round(d.max)}°</b> <span className="faint">{Math.round(d.min)}°</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

// --- микшер ----------------------------------------------------------------------------------------------
function MixerIcon({ app, title }: { app: string; title: string }) {
  if (title === "Jarvis") return <LogoMark size={30} />;
  if (app === "system") return <AppIcon name="volume" size={30} radius={9} />;
  return <AppIcon name={app} size={30} radius={9} />;
}

function MixerCard() {
  const { data, error, reload } = usePoll<Mixer>("/api/system/mixer", 4000);
  const [local, setLocal] = useState<Mixer | null>(null);
  const before = useRef<Record<string, number>>({});
  useEffect(() => setLocal(null), [data]);
  const mixer = local ?? data;

  const setApp = async (app: string, level: number) => {
    if (mixer) setLocal({ ...mixer, items: mixer.items.map((i) => (i.app === app ? { ...i, level, muted: false } : i)) });
    try {
      setLocal(await api.post<Mixer>("/api/system/mixer", { app, level }));
    } catch (e) {
      setLocal(null);
      notifyError(e);
      reload();
    }
  };

  const master = async (action: string, body: Record<string, unknown> = {}) => {
    try {
      const r = await api.post<{ volume?: number; muted?: boolean }>(`/api/media/${action}`, body);
      if (mixer && typeof r?.volume === "number") setLocal({ ...mixer, master: { volume: r.volume, muted: !!r.muted } });
      else reload();
    } catch (e) {
      notifyError(e);
    }
  };

  const m = mixer?.master;
  const MasterIcon = !m || m.muted || m.volume === 0 ? VolumeX : m.volume < 45 ? Volume1 : Volume2;
  return (
    <Panel icon={<SlidersVertical />} title="Микшер громкости" className="sys-mixer">
      {!mixer ? (
        <div className="sys-note">{error || "Загружаю…"}</div>
      ) : (
        <div className="sys-mixer-list">
          <div className="sys-mix-row master">
            <span className="sys-mix-icon master">
              <MasterIcon />
            </span>
            <span className="sys-mix-name">
              Общая громкость
              <small>{m!.muted ? "звук выключен" : "все программы"}</small>
            </span>
            <div className="sys-mix-slider">
              <Slider value={m!.muted ? 0 : m!.volume} onCommit={(v) => master("volume", { level: Math.round(v) })} />
            </div>
            <button
              type="button"
              className={cx("sys-mute", m!.muted && "on")}
              aria-label={m!.muted ? "Включить звук" : "Выключить звук"}
              title={m!.muted ? "Включить звук" : "Выключить звук"}
              onClick={() => master(m!.muted ? "unmute" : "mute")}
            >
              {m!.muted ? <VolumeX /> : <Volume2 />}
            </button>
          </div>
          {mixer.items.length === 0 && <div className="sys-note">Сейчас ни одна программа не издаёт звук</div>}
          {mixer.items.map((it) => {
            const muted = it.muted || it.level === 0;
            return (
              <div key={it.app} className={cx("sys-mix-row", it.active && "active")}>
                <MixerIcon app={it.app} title={it.title} />
                <span className="sys-mix-name">
                  <span className="ellipsis">{it.title}</span>
                  <small>{it.active ? "звучит сейчас" : muted ? "без звука" : "не звучит"}</small>
                </span>
                <div className="sys-mix-slider">
                  <Slider value={muted ? 0 : it.level} onCommit={(v) => setApp(it.app, Math.round(v))} />
                </div>
                <button
                  type="button"
                  className={cx("sys-mute", muted && "on")}
                  aria-label={muted ? `Включить звук: ${it.title}` : `Выключить звук: ${it.title}`}
                  title={muted ? "Включить звук" : "Без звука"}
                  onClick={() => {
                    if (muted) setApp(it.app, before.current[it.app] || 60);
                    else {
                      before.current[it.app] = it.level;
                      setApp(it.app, 0);
                    }
                  }}
                >
                  {muted ? <VolumeX /> : <Volume2 />}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// --- сейчас играет ---------------------------------------------------------------------------------------
function fmtTime(s: number): string {
  const v = Math.max(0, Math.floor(s));
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, "0")}`;
}

function MediaCard() {
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
    <Panel icon={<Music2 />} title="Сейчас играет" className="sys-media" aside={has && media?.app ? <span className="chip">{media.app}</span> : undefined}>
      <div className="sys-media-body">
        <div className={cx("sys-media-art", playing && "playing")}>
          {media?.thumbnail ? <img src={media.thumbnail} alt="" /> : <Music2 />}
        </div>
        <div className="sys-media-info">
          <div className="sys-media-title ellipsis">{has ? media!.title : "Ничего не играет"}</div>
          <div className="sys-media-artist ellipsis">{has ? media!.artist || media!.album || "" : "Скажите «Джарвис, включи музыку»"}</div>
          {has && media!.duration_s > 0 && (
            <div className="sys-media-progress">
              <span>{fmtTime(pos)}</span>
              <span className="sys-bar">
                <i style={{ width: `${Math.min(100, (pos / media!.duration_s) * 100)}%` }} />
              </span>
              <span>{fmtTime(media!.duration_s)}</span>
            </div>
          )}
          <div className="sys-media-controls">
            <button type="button" className="icon-btn ghost" onClick={() => act("prev")} aria-label="Предыдущий трек">
              <SkipBack />
            </button>
            <button type="button" className="play-btn" onClick={() => act("play_pause")} aria-label={playing ? "Пауза" : "Играть"}>
              {playing ? <Pause /> : <Play />}
            </button>
            <button type="button" className="icon-btn ghost" onClick={() => act("next")} aria-label="Следующий трек">
              <SkipForward />
            </button>
          </div>
        </div>
      </div>
    </Panel>
  );
}

// --- программы ------------------------------------------------------------------------------------------
function TopCard({ sensors }: { sensors: Sensors | null }) {
  const top = sensors?.top ?? [];
  const max = Math.max(5, ...top.map((t) => t.cpu));
  return (
    <Panel icon={<Cpu />} title="Самые активные программы" className="sys-top">
      {!sensors ? (
        <div className="sys-note">Загружаю…</div>
      ) : !top.length ? (
        <div className="sys-note">Список программ пока пуст</div>
      ) : (
        <div className="sys-top-list">
          {top.map((p) => (
            <div key={p.name} className="sys-top-row">
              <AppIcon name={p.name} size={28} radius={8} />
              <span className="sys-top-name ellipsis">{procName(p.name)}</span>
              <span className="sys-top-bar">
                <i style={{ width: `${Math.min(100, (p.cpu / max) * 100)}%` }} />
              </span>
              <span className="sys-top-cpu">{p.cpu.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%</span>
              <span className="sys-top-ram">
                {p.ram_mb >= 1024 ? `${(p.ram_mb / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ГБ` : `${p.ram_mb} МБ`}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

// --- инструменты -----------------------------------------------------------------------------------------
const TOOLS: { title: string; hint: string; icon: ReactNode; text?: string; route?: string[] }[] = [
  { title: "Таймер 5 мин", hint: "поставь таймер на 5 минут", icon: <AlarmClock />, text: "поставь таймер на 5 минут" },
  { title: "Помидор 25 мин", hint: "25 минут работы без отвлечений", icon: <Timer />, text: "поставь таймер на 25 минут" },
  { title: "Скриншот", hint: "снимок экрана", icon: <Camera />, text: "сделай скриншот" },
  { title: "Свернуть всё", hint: "показать рабочий стол", icon: <Minimize2 />, text: "сверни все окна" },
  { title: "Тихий режим", hint: "не беспокоить", icon: <MoonStar />, text: "включи тихий режим" },
  { title: "Заметки", hint: "заметки и задачи", icon: <NotebookPen />, route: ["ai", "notes", "notes"] },
  { title: "Задачи", hint: "список дел", icon: <ListTodo />, route: ["ai", "notes", "tasks"] },
  { title: "Спросить Jarvis", hint: "чат с ИИ", icon: <Sparkles />, route: ["ai"] },
];

function ToolsCard() {
  const [busy, setBusy] = useState("");
  const run = async (t: (typeof TOOLS)[number]) => {
    if (t.route) {
      navigate(...t.route);
      return;
    }
    setBusy(t.title);
    try {
      // команда идёт тем же путём, что и голосом: /api/widgets/quick/run → диспетчер
      const r = await api.post<{ kind?: string; result?: { ok: boolean; title: string; message: string } }>(
        "/api/widgets/quick/run",
        { text: t.text },
      );
      if (r?.result) notify(r.result.ok ? r.result.title || t.title : r.result.message || `Не получилось: ${t.title}`, r.result.ok ? "ok" : "error");
      else if (r?.kind === "unknown") notify(`Команда не нашлась: «${t.text}»`, "error");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy("");
    }
  };
  return (
    <Panel icon={<Wrench />} title="Инструменты" className="sys-tools">
      <div className="sys-tools-grid">
        {TOOLS.map((t) => (
          <button key={t.title} type="button" className={cx("sys-tool", busy === t.title && "busy")} onClick={() => run(t)} title={t.hint}>
            <span className="sys-tool-icon">{busy === t.title ? <span className="spinner" /> : t.icon}</span>
            <span className="sys-tool-title">{t.title}</span>
          </button>
        ))}
      </div>
    </Panel>
  );
}

// --- страница --------------------------------------------------------------------------------------------
export default function SystemPage() {
  const { data: stats } = useWidgetData<SysStats>("system", {}, { refreshMs: 2000 });
  const { data: sensors } = usePoll<Sensors>("/api/system/sensors", 3000);
  const hot = useMemo(() => [sensors?.cpu_temp, sensors?.gpu_temp].some((t) => heat(t) === "hot"), [sensors]);
  return (
    <div className="page sys-page">
      <PageHeader
        title="Системные операции"
        actions={
          <span className={cx("sys-status", hot && "hot")}>
            <span className="sys-status-dot" />
            {hot ? "Компьютер перегревается" : "Система в норме"}
          </span>
        }
      />
      <div className="page-body">
        <div className="sys-grid">
          <LoadCard stats={stats} sensors={sensors} />
          <WeatherCard />
          <MediaCard />
          <MixerCard />
          <TopCard sensors={sensors} />
          <ToolsCard />
        </div>
      </div>
    </div>
  );
}
