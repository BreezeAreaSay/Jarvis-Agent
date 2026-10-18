// Живая панель плагина на карточке: что он делает прямо сейчас (по статусу и событиям plugin.<id>.*).
// Диктовка — поле с текстом и «Слушаю…», субтитры — кадр с подписью, конспект — таймер записи и итоги,
// чтение — подсвеченная фраза и прогресс, умный дом — устройства, питомец — живая медуза и т. д.
import {
  BellRing,
  Check,
  Laptop,
  Lightbulb,
  LockKeyhole,
  Mic,
  Pause,
  Play,
  ShieldCheck,
  Square,
  UserRound,
  Volume2,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { AppIcon } from "@/components/AppIcon";
import { cx, notifyError, Toggle } from "@/ui";
import { type JellyMood, PetCanvas } from "@/widgets/extra/PixelJelly";
import { canToggle, deviceState, useSmartDevices } from "@/widgets/extra/smarthome";
import { ClipboardLive } from "./ClipboardLive";
import { FileSearchLive } from "./FileSearchLive";
import { FocusLive } from "./FocusLive";
import type { Plugin } from "./pluginKit";
import { usePlugins } from "./pluginStore";

type Dict = Record<string, unknown>;

function useLive(id: string) {
  return usePlugins((s) => s.live[id]);
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v === null || v === undefined ? "" : String(v);
}

function numOr(v: unknown, d: number): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : d;
}

export function mmss(total: number): string {
  const s = Math.max(0, Math.floor(total));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0");
  return h ? `${h}:${mm}:${String(sec).padStart(2, "0")}` : `${mm}:${String(sec).padStart(2, "0")}`;
}

/** Анимированные полоски «звука». */
export function Bars({ n = 6, className }: { n?: number; className?: string }) {
  return (
    <span className={cx("lv-bars", className)} aria-hidden>
      {Array.from({ length: n }, (_, i) => (
        <i key={i} style={{ animationDelay: `${(i * 137) % 600}ms` }} />
      ))}
    </span>
  );
}

function Placeholder({ children }: { children: ReactNode }) {
  return <span className="lv-placeholder">{children}</span>;
}

// --- диктовка ----------------------------------------------------------------------------------------
function DictationLive({ plugin }: { plugin: Plugin }) {
  const live = useLive(plugin.id);
  const ev = (live?.state?.data ?? {}) as Dict;
  const state = str(ev.state || plugin.status?.state || "idle");
  const text = str(ev.text || plugin.status?.text || plugin.status?.last_text || "");
  const hotkey = str(plugin.config?.hotkey || plugin.status?.hotkey || "");
  const active = ev.active ?? plugin.status?.active;
  const listening = active === true || ["listening", "recording", "active", "speaking"].includes(state);
  const busy = ["processing", "thinking"].includes(state);
  return (
    <div className="lv lv-dictation">
      <div className={cx("lv-field", listening && "live")}>
        {text ? (
          <span className="lv-text">{text}</span>
        ) : (
          <Placeholder>
            {hotkey ? `Нажмите ${hotkey} и говорите — ` : "Говорите — "}текст появится там, где стоит курсор
          </Placeholder>
        )}
        {listening && <span className="lv-caret" />}
      </div>
      <div className={cx("lv-pill", listening && "on")}>
        <Mic />
        {listening ? "Слушаю…" : busy ? "Расставляю знаки…" : plugin.enabled ? (hotkey ? `Готов · ${hotkey}` : "Готов") : "Выключена"}
        {listening && <Bars n={6} />}
      </div>
    </div>
  );
}

// --- субтитры ----------------------------------------------------------------------------------------
/** Кадр «видео» с подписью: ▶ запускает субтитры (плагин включён — ещё не значит, что они идут), ■ — останавливает. */
function SubtitlesLive({ plugin, onAction }: { plugin: Plugin; onAction: (id: string) => void }) {
  const live = useLive(plugin.id);
  const ev = (live?.text?.data ?? {}) as Dict;
  const last = (plugin.status?.last ?? {}) as Dict;
  const state = live?.state && (!live.text || live.state.ts >= live.text.ts) ? (live.state.data as Dict) : null;
  const active = typeof state?.active === "boolean" ? state.active : !!plugin.status?.active;
  const noTranslate = plugin.config?.target_lang === "none";
  const text = str(ev.text || last.text || plugin.status?.text || "");
  const translation = noTranslate ? "" : str(ev.translation || last.translation || plugin.status?.translation || "");
  const fresh = live?.text ? Date.now() - live.text.ts < 15000 : false;
  const placeholder = !plugin.enabled
    ? "Включите плагин переключателем справа вверху"
    : active
      ? "Слушаю звук компьютера — подпись появится здесь и внизу экрана"
      : "Нажмите ▶ — и Jarvis начнёт подписывать то, что звучит в колонках";
  return (
    <div className={cx("lv lv-subtitles", active && "active")}>
      <div className="lv-video">
        {active && (
          <span className="lv-onair">
            <i /> Субтитры идут
          </span>
        )}
        <button
          className={cx("lv-play", active && "on")}
          disabled={!plugin.enabled}
          onClick={() => onAction(active ? "stop" : "start")}
          aria-label={active ? "Остановить субтитры" : "Запустить субтитры"}
          title={active ? "Остановить субтитры" : "Запустить субтитры"}
        >
          {active ? <Square /> : <Play />}
        </button>
        <div className={cx("lv-caption", !(text || translation) && "empty", fresh && "fresh")}>
          {text || translation ? (
            <>
              {text && translation && translation !== text && <div className="orig">{text}</div>}
              <div className="tr">{translation || text}</div>
            </>
          ) : (
            <div className="tr faint">{placeholder}</div>
          )}
        </div>
      </div>
    </div>
  );
}

// --- конспект ----------------------------------------------------------------------------------------
interface SummaryResult {
  title?: string;
  text?: string;
  summary?: string;
  decisions?: string[];
  tasks?: string[];
}

function SummaryLive({ plugin }: { plugin: Plugin }) {
  const live = useLive(plugin.id);
  const ev = live?.state;
  const data = (ev?.data ?? {}) as Dict;
  const state = str(data.state || plugin.status?.state || "idle");
  const baseSeconds = numOr(data.seconds ?? plugin.status?.seconds, 0);
  const recording = ["recording", "listening", "active"].includes(state);
  const processing = ["processing", "summarizing", "thinking"].includes(state);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!recording) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [recording]);
  const seconds = recording && ev ? baseSeconds + (Date.now() - ev.ts) / 1000 : baseSeconds;
  const lastRaw = (data.last ?? data.result ?? plugin.status?.last ?? plugin.status?.result) as SummaryResult | string | undefined;
  const last: SummaryResult | null = typeof lastRaw === "string" ? { text: lastRaw } : lastRaw && typeof lastRaw === "object" ? lastRaw : null;
  const rows = [
    ...(last?.decisions ?? []).map((t) => ({ kind: "Решения", t, cls: "dec" })),
    ...(last?.tasks ?? []).map((t) => ({ kind: "Задачи", t, cls: "task" })),
  ];
  return (
    <div className="lv lv-summary">
      <div className="lv-box">
        <div className="row" style={{ gap: 10 }}>
          <span className="lv-box-title">{last?.title || "Конспект созвона"}</span>
          <span className="spacer" />
          {recording && (
            <>
              <Bars n={12} className="red" />
              <span className="lv-timer">{mmss(seconds)}</span>
            </>
          )}
          {processing && (
            <span className="row faint small" style={{ gap: 6 }}>
              <span className="spinner" /> Составляю конспект…
            </span>
          )}
        </div>
        {rows.length ? (
          <div className="lv-sum-rows">
            {rows.slice(0, 5).map((r, i) => (
              <div key={i} className={cx("lv-sum-row", r.cls)}>
                <span className="lv-check">
                  <Check strokeWidth={3} />
                </span>
                <span className="lv-sum-kind">{r.kind}</span>
                <span className="ellipsis">{r.t}</span>
              </div>
            ))}
          </div>
        ) : last?.text || last?.summary ? (
          <p className="lv-sum-text">{last.text || last.summary}</p>
        ) : (
          <Placeholder>
            {recording
              ? "Записываю звук созвона — после остановки появятся решения и задачи"
              : "Скажите «сделай конспект созвона» — Jarvis запишет звук и выделит решения и задачи"}
          </Placeholder>
        )}
      </div>
    </div>
  );
}

// --- чтение вслух ------------------------------------------------------------------------------------
function ReaderLive({ plugin, onAction }: { plugin: Plugin; onAction: (id: string) => void }) {
  const live = useLive(plugin.id);
  const ev = (live?.progress?.data ?? {}) as Dict;
  const src = { ...(plugin.status ?? {}), ...ev } as Dict;
  const state = str(src.state || "idle");
  const index = numOr(src.index, 0);
  const total = numOr(src.total, 0);
  const sentence = str(src.sentence || "");
  const prev = str(src.prev || src.previous || "");
  const next = str(src.next || "");
  const playing = ["playing", "reading", "speaking"].includes(state);
  const paused = state === "paused";
  const speed = numOr(src.speed ?? plugin.config?.speed, 1);
  const title = str(src.title || "");
  const bars = useMemo(() => Array.from({ length: 34 }, (_, i) => 0.35 + 0.65 * Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.6))), []);
  const progress = total > 0 ? Math.min(1, (index + (playing ? 0.5 : 0)) / total) : 0;
  const has = (id: string) => plugin.actions?.some((a) => a.id === id);
  const toggleAction = playing ? (has("pause") ? "pause" : has("stop") ? "stop" : "") : paused && has("resume") ? "resume" : "";
  return (
    <div className="lv lv-reader">
      <div className="lv-box lv-reader-text">
        {title && <div className="lv-reader-title ellipsis">{title}</div>}
        {sentence ? (
          <>
            {prev && <div className="faded">{prev}</div>}
            <div className="hl">{sentence}</div>
            {next && <div className="faded">{next}</div>}
          </>
        ) : (
          <Placeholder>Скажите «прочитай вслух» или выделите текст — Jarvis прочитает его, подсвечивая фразу</Placeholder>
        )}
      </div>
      <div className={cx("lv-player", (playing || paused) && "on")}>
        <button
          className="lv-pp"
          disabled={!toggleAction}
          onClick={() => toggleAction && onAction(toggleAction)}
          aria-label={playing ? "Пауза" : "Продолжить"}
        >
          {playing ? <Pause /> : <Play />}
        </button>
        <span className="lv-wave">
          {bars.map((v, i) => (
            <i key={i} className={i / bars.length < progress ? "done" : ""} style={{ height: `${Math.round(v * 100)}%` }} />
          ))}
        </span>
        <span className="lv-speed">{speed.toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}×</span>
      </div>
      {total > 0 && (
        <div className="faint tiny" style={{ marginTop: -2 }}>
          Фраза {Math.min(index + 1, total)} из {total}
        </div>
      )}
    </div>
  );
}

// --- умный дом ---------------------------------------------------------------------------------------
function SmartHomeDevices() {
  const { devices, error, toggle } = useSmartDevices(20000);
  const lit = devices.some((d) => d.kind === "light" && d.state?.on);
  const shown = devices.filter((d) => d.kind !== "sensor").slice(0, 6);
  return (
    <>
      <div className={cx("lv-bulb", lit && "lit")}>
        <Lightbulb />
      </div>
      {shown.length ? (
        <div className="lv-chips">
          {shown.map((d) => (
            <span key={d.id} className={cx("lv-chip", d.state?.on && "on")}>
              <span className="ellipsis">{d.name}</span>
              {canToggle(d) && d.kind !== "climate" ? (
                <Toggle checked={!!d.state?.on} onChange={() => toggle(d)} label={d.name} />
              ) : (
                <span className="faint">· {deviceState(d)}</span>
              )}
            </span>
          ))}
        </div>
      ) : (
        <Placeholder>{error || "Устройств пока нет — проверьте адрес и токен в настройках ниже"}</Placeholder>
      )}
    </>
  );
}

function SmartHomeLive({ plugin }: { plugin: Plugin }) {
  const hint = str(plugin.status?.hint || "");
  return (
    <div className="lv lv-smart">
      {plugin.enabled && plugin.status?.running !== false ? (
        <SmartHomeDevices />
      ) : (
        <>
          <div className="lv-bulb">
            <Lightbulb />
          </div>
          <Placeholder>{hint || "Включите плагин — свет, розетки и климат появятся здесь"}</Placeholder>
        </>
      )}
    </div>
  );
}

// --- блокировка --------------------------------------------------------------------------------------
function AutolockLive({ plugin }: { plugin: Plugin }) {
  const live = useLive(plugin.id);
  const ev = Object.values(live ?? {}).sort((a, b) => b.ts - a.ts)[0]?.data ?? {};
  const s = { ...(plugin.status ?? {}), ...ev } as Dict;
  const [, tick] = useState(0);
  const running = plugin.enabled && s.running !== false;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [running]);
  const now = Date.now() / 1000;
  const pausedUntil = numOr(s.paused_until, 0);
  const paused = pausedUntil > now;
  const present = s.present === true ? true : s.present === false || s.away === true || s.state === "away" ? false : null;
  const awayFor = numOr(s.away_s, NaN);
  const limit = numOr(plugin.config?.away_seconds ?? plugin.config?.delay_s, NaN);
  const left = numOr(s.seconds_left ?? s.lock_in, Number.isFinite(awayFor) && Number.isFinite(limit) ? limit - awayFor : NaN);
  const mode = str(s.mode || plugin.config?.mode || "");
  let text: string;
  let tone: "ok" | "warn" | "" = "";
  if (!plugin.enabled) text = "Включите — и Jarvis заблокирует компьютер, когда вы отойдёте";
  else if (!running) text = "Запускаю слежение…";
  else if (paused)
    text = `Не блокирую до ${new Date(pausedUntil * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
  else if (s.armed === false) text = "Слежение на паузе";
  else if (present === false) {
    tone = "warn";
    text = Number.isFinite(left) && left > 0 ? `Вас не видно — блокирую через ${Math.ceil(left)} с` : "Вас не видно — блокирую";
  } else if (present === true) {
    tone = "ok";
    text = mode === "idle" ? "Вы за компьютером — клавиатура и мышь в деле" : "Вы за компьютером — камера вас видит";
  } else text = mode === "idle" ? "Слежу за клавиатурой и мышью" : "Слежу через камеру";
  return (
    <div className="lv lv-center">
      <div className={cx("lv-round", tone)}>{present === false ? <LockKeyhole /> : present === true ? <UserRound /> : <ShieldCheck />}</div>
      <div className="lv-caption-text">{text}</div>
      {running && str(s.camera) && mode !== "idle" && (
        <span className="chip mini">Камера: {s.camera === "on" ? "включена" : s.camera === "off" ? "выключена" : str(s.camera)}</span>
      )}
    </div>
  );
}

// --- второй компьютер --------------------------------------------------------------------------------
function SecondPcLive({ plugin }: { plugin: Plugin }) {
  const cfg = plugin.config ?? {};
  const computers = (Array.isArray(cfg.pcs) ? cfg.pcs : Array.isArray(cfg.computers) ? cfg.computers : []) as Dict[];
  const st = plugin.status ?? {};
  const statusList = (Array.isArray(st.pcs) ? st.pcs : Array.isArray(st.computers) ? st.computers : []) as Dict[];
  return (
    <div className="lv lv-center">
      {computers.length ? (
        <div className="lv-pcs">
          {computers.map((c, i) => {
            const info = statusList.find((x) => (c.id && x.id === c.id) || x.name === c.name) ?? statusList[i];
            const online = info ? info.online === true || info.status === "online" : undefined;
            const err = info ? str(info.error) : "";
            return (
              <div key={str(c.id) || i} className="lv-pc">
                <Laptop />
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="ellipsis" style={{ fontWeight: 650 }}>{str(c.name) || "Компьютер"}</div>
                  <div className={cx("tiny ellipsis", err ? "lv-err" : "faint")}>{err || str(c.url || c.address || c.host || "")}</div>
                </div>
                {plugin.enabled && (
                  <span className={cx("status-dot", online === true && "ok", online === false && "err")} title={online ? "В сети" : online === false ? "Не отвечает" : ""} />
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <>
          <div className="lv-round">
            <Laptop />
          </div>
          <div className="lv-caption-text">Добавьте компьютер ниже — и командуйте им голосом: «на ноутбуке включи музыку»</div>
        </>
      )}
    </div>
  );
}

// --- озвучка уведомлений -----------------------------------------------------------------------------
function NotifyLive({ plugin }: { plugin: Plugin }) {
  const live = useLive(plugin.id);
  const evLast = Object.values(live ?? {}).sort((a, b) => b.ts - a.ts)[0]?.data as Dict | undefined;
  const last = (evLast?.text || evLast?.title ? evLast : plugin.status?.last) as Dict | undefined;
  return (
    <div className="lv lv-center">
      {last && (last.text || last.title) ? (
        <div className="lv-notif">
          <AppIcon name={str(last.app || "bell").toLowerCase()} size={36} />
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="row" style={{ gap: 6 }}>
              <b className="ellipsis">{str(last.title || last.app)}</b>
              {last.app ? <span className="faint tiny">· {str(last.app)}</span> : null}
            </div>
            <div className="muted small ellipsis">{str(last.text)}</div>
          </div>
          <Volume2 className="lv-notif-speak" />
        </div>
      ) : (
        <>
          <div className="lv-round">
            <BellRing />
          </div>
          <div className="lv-caption-text">Здесь появится последнее уведомление, которое Jarvis прочитал вслух</div>
        </>
      )}
    </div>
  );
}

// --- питомец -----------------------------------------------------------------------------------------
const PET_ACTIONS: Record<string, string> = {
  dance: "Танцует",
  sleep: "Спит",
  eat: "Ест",
  feed: "Ест",
  happy: "Радуется",
  play: "Играет",
  music: "Слушает музыку",
  walk: "Гуляет",
  idle: "Отдыхает",
};

function PetLive({ plugin }: { plugin: Plugin }) {
  const live = useLive(plugin.id);
  const ev = live?.action;
  const action = str((ev?.data as Dict | undefined)?.action || plugin.status?.action || "idle");
  const recent = ev ? Date.now() - ev.ts < 8000 : false;
  const [poke, setPoke] = useState(0);
  useEffect(() => {
    if (!poke) return;
    const t = setTimeout(() => setPoke(0), 1800);
    return () => clearTimeout(t);
  }, [poke]);
  const music = plugin.status?.playing === true && plugin.config?.dance_to_music !== false;
  const mood: JellyMood = poke
    ? "happy"
    : (recent && (action === "dance" || action === "music")) || (music && action !== "sleep")
      ? "dance"
      : action === "sleep"
        ? "sleep"
        : recent && (action === "eat" || action === "feed")
          ? "eat"
          : "idle";
  const name = str(plugin.config?.name || "Люкси");
  return (
    <div className="lv lv-pet">
      <PetCanvas size={132} mood={mood} onClick={() => setPoke(Date.now())} />
      <span className="lv-petname">{name}</span>
      <span className="faint tiny">{poke ? "Мур!" : music && action === "idle" ? "Танцует под музыку" : PET_ACTIONS[action] ?? action}</span>
    </div>
  );
}

// --- общее -------------------------------------------------------------------------------------------
export function PluginLive({ plugin, onAction }: { plugin: Plugin; onAction: (id: string) => void }) {
  switch (plugin.id) {
    case "dictation":
      return <DictationLive plugin={plugin} />;
    case "subtitles":
      return <SubtitlesLive plugin={plugin} onAction={onAction} />;
    case "summary":
      return <SummaryLive plugin={plugin} />;
    case "reader":
      return <ReaderLive plugin={plugin} onAction={onAction} />;
    case "smart_home":
      return <SmartHomeLive plugin={plugin} />;
    case "autolock":
      return <AutolockLive plugin={plugin} />;
    case "second_pc":
      return <SecondPcLive plugin={plugin} />;
    case "notify_voice":
      return <NotifyLive plugin={plugin} />;
    case "pet":
      return <PetLive plugin={plugin} />;
    case "focus":
      return <FocusLive plugin={plugin} />;
    case "clipboard":
      return <ClipboardLive plugin={plugin} />;
    case "file_search":
      return <FileSearchLive plugin={plugin} />;
    default:
      return null;
  }
}

/** Числа из статуса, понятные человеку: «Устройств: 6». */
const STATUS_LABELS: Record<string, string> = {
  devices: "Устройств",
  online: "В сети",
  count: "Сегодня",
  read: "Прочитано",
  segments: "Фраз",
  recycle_count: "В Корзине",
  read_today: "Прочитано сегодня",
  words: "Слов",
  language: "Язык",
  model: "Модель",
  queue: "В очереди",
  computers: "Компьютеров",
};

export function StatusFacts({ plugin }: { plugin: Plugin }) {
  const facts = Object.entries(plugin.status ?? {})
    .filter(([k, v]) => STATUS_LABELS[k] && (typeof v === "number" || (typeof v === "string" && v)))
    .slice(0, 4);
  if (!facts.length || !plugin.enabled) return null;
  return (
    <div className="pl-facts">
      {facts.map(([k, v]) => (
        <span key={k} className="chip">
          {STATUS_LABELS[k]}: <b style={{ color: "var(--text)" }}>{typeof v === "number" ? v.toLocaleString("ru-RU") : String(v)}</b>
        </span>
      ))}
    </div>
  );
}

export function runPluginAction(plugin: Plugin, id: string) {
  return usePlugins
    .getState()
    .action(plugin.id, id)
    .catch((e) => {
      notifyError(e);
      throw e;
    });
}

