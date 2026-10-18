// Время: таймер с заготовками 5/15/25 минут (помодоро), секундомер с кругами, мировое время,
// обратный отсчёт до события («12 дней до сессии»).
import { CalendarClock, Flag, Globe2, Moon, Pause, Play, RotateCcw, Sun, Timer, TimerReset, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { cx, notify, Select, TextInput } from "@/ui";
import { WidgetFrame, type WidgetProps } from "../common";
import { askNotifyPermission, CfgButton, chime, clock, desktopNotify, plural, useStored, useTick, WidgetSettingsModal } from "./shared";

// --- таймер ---------------------------------------------------------------------------------------------
interface TimerState {
  duration: number; // секунды
  endsAt: number | null; // мс, если идёт
  left: number; // секунды, если на паузе
  done: boolean;
}

const PRESETS = [5, 15, 25];

export function TimerWidget({ id, w, h, config }: WidgetProps) {
  const base = Math.max(10, Number(config.seconds) || 25 * 60);
  const [st, setSt] = useStored<TimerState>(`jarvis.widget.timer.${id}`, { duration: base, endsAt: null, left: base, done: false });
  const running = st.endsAt !== null;
  useTick(250, running);
  const left = running ? Math.max(0, (st.endsAt! - Date.now()) / 1000) : st.left;
  const title = (config.title as string) || "Таймер";
  const fired = useRef(false);

  useEffect(() => {
    if (!running || left > 0) {
      fired.current = false;
      return;
    }
    if (fired.current) return;
    fired.current = true;
    const late = Date.now() - st.endsAt! > 15_000; // закончился, пока окна не было — без звона
    setSt((s) => ({ ...s, endsAt: null, left: 0, done: true }));
    if (!late) {
      chime();
      desktopNotify(`${title}: время вышло`, `Прошло ${Math.round(st.duration / 60)} мин`);
      notify(`${title}: время вышло`, "ok");
    }
  }, [running, left, st.endsAt, st.duration, title, setSt]);

  const start = () => {
    askNotifyPermission();
    const from = st.done || st.left <= 0 ? st.duration : st.left;
    setSt((s) => ({ ...s, endsAt: Date.now() + from * 1000, done: false }));
  };
  const pause = () => setSt((s) => ({ ...s, endsAt: null, left: Math.max(0, (s.endsAt! - Date.now()) / 1000) }));
  const reset = () => setSt((s) => ({ ...s, endsAt: null, left: s.duration, done: false }));
  const preset = (min: number) => setSt({ duration: min * 60, endsAt: null, left: min * 60, done: false });

  const pct = st.duration > 0 ? 1 - left / st.duration : 0;
  const big = w >= 2 && h >= 2;
  const controls = (
    <div className="wx-timer-ctl">
      <button className={cx("wx-round-btn", running && "on")} onClick={running ? pause : start} aria-label={running ? "Пауза" : "Старт"}>
        {running ? <Pause /> : <Play />}
      </button>
      <button className="icon-btn ghost sm" onClick={reset} aria-label="Сбросить" title="Сбросить">
        <RotateCcw />
      </button>
    </div>
  );
  const presets = (
    <div className="wx-presets">
      {PRESETS.map((m) => (
        <button key={m} className={cx("wx-preset", st.duration === m * 60 && "on")} onClick={() => preset(m)}>
          {m}
        </button>
      ))}
    </div>
  );

  if (big) {
    const R = 46;
    const C = 2 * Math.PI * R;
    return (
      <WidgetFrame icon={<Timer />} title={title} actions={presets}>
        <div className="wx-timer-big">
          <div className={cx("wx-ring", st.done && "done")}>
            <svg viewBox="0 0 100 100" aria-hidden>
              <circle cx="50" cy="50" r={R} className="track" />
              <circle cx="50" cy="50" r={R} className="bar" strokeDasharray={C} strokeDashoffset={C * (1 - pct)} />
            </svg>
            <div className="wx-ring-text">
              <span className="wx-time">{st.done ? "00:00" : clock(left)}</span>
              <span className="sub">{st.done ? "Готово!" : running ? "Идёт" : left < st.duration ? "Пауза" : `${Math.round(st.duration / 60)} мин`}</span>
            </div>
          </div>
          {controls}
        </div>
      </WidgetFrame>
    );
  }
  return (
    <WidgetFrame icon={<Timer />} title={title} actions={presets}>
      <div className="wx-timer">
        <span className={cx("wx-time", st.done && "done")}>{st.done ? "Готово!" : clock(left)}</span>
        <span className="spacer" />
        {controls}
      </div>
      <div className="wbar wx-timer-bar">
        <i style={{ width: `${Math.round(pct * 100)}%` }} />
      </div>
    </WidgetFrame>
  );
}

// --- секундомер -----------------------------------------------------------------------------------------
interface WatchState {
  startedAt: number | null;
  acc: number; // мс до последней паузы
  laps: number[];
}

function watchText(ms: number, tenths = true): string {
  const total = Math.max(0, ms);
  const h = Math.floor(total / 3_600_000);
  const m = Math.floor((total % 3_600_000) / 60_000);
  const s = Math.floor((total % 60_000) / 1000);
  const t = Math.floor((total % 1000) / 100);
  const core = `${h ? `${h}:` : ""}${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return tenths ? `${core},${t}` : core;
}

export function StopwatchWidget({ id, w, h, config }: WidgetProps) {
  const [st, setSt] = useStored<WatchState>(`jarvis.widget.stopwatch.${id}`, { startedAt: null, acc: 0, laps: [] });
  const running = st.startedAt !== null;
  useTick(100, running);
  const elapsed = st.acc + (running ? Date.now() - st.startedAt! : 0);
  const showLaps = (h >= 2 || w >= 2) && st.laps.length > 0;
  return (
    <WidgetFrame icon={<TimerReset />} title={(config.title as string) || "Секундомер"}>
      <div className={cx("wx-watch", showLaps && "with-laps")}>
        <div className="wx-timer">
          <span className="wx-time">{watchText(elapsed)}</span>
          <span className="spacer" />
          <div className="wx-timer-ctl">
            <button
              className={cx("wx-round-btn", running && "on")}
              onClick={() =>
                setSt((s) => (s.startedAt !== null ? { ...s, startedAt: null, acc: s.acc + Date.now() - s.startedAt } : { ...s, startedAt: Date.now() }))
              }
              aria-label={running ? "Пауза" : "Старт"}
            >
              {running ? <Pause /> : <Play />}
            </button>
            {running ? (
              <button
                className="icon-btn ghost sm"
                aria-label="Круг"
                title="Круг"
                onClick={() => setSt((s) => ({ ...s, laps: [elapsed, ...s.laps].slice(0, 30) }))}
              >
                <Flag />
              </button>
            ) : (
              <button className="icon-btn ghost sm" aria-label="Сбросить" title="Сбросить" disabled={!elapsed} onClick={() => setSt({ startedAt: null, acc: 0, laps: [] })}>
                <RotateCcw />
              </button>
            )}
          </div>
        </div>
        {showLaps && (
          <div className="wx-laps">
            {st.laps.map((lap, i) => {
              const prev = st.laps[i + 1] ?? 0;
              return (
                <div key={st.laps.length - i} className="wx-lap">
                  <span className="faint">Круг {st.laps.length - i}</span>
                  <span className="spacer" />
                  <span className="faint">+{watchText(lap - prev)}</span>
                  <span>{watchText(lap)}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </WidgetFrame>
  );
}

// --- мировое время --------------------------------------------------------------------------------------
export const CITIES: Record<string, string> = {
  "Europe/Moscow": "Москва",
  "Europe/Kaliningrad": "Калининград",
  "Europe/Samara": "Самара",
  "Asia/Yekaterinburg": "Екатеринбург",
  "Asia/Omsk": "Омск",
  "Asia/Novosibirsk": "Новосибирск",
  "Asia/Krasnoyarsk": "Красноярск",
  "Asia/Irkutsk": "Иркутск",
  "Asia/Yakutsk": "Якутск",
  "Asia/Vladivostok": "Владивосток",
  "Asia/Magadan": "Магадан",
  "Asia/Kamchatka": "Камчатка",
  "Europe/Minsk": "Минск",
  "Europe/Kyiv": "Киев",
  "Europe/Kiev": "Киев",
  "Asia/Almaty": "Алматы",
  "Asia/Tashkent": "Ташкент",
  "Asia/Tbilisi": "Тбилиси",
  "Asia/Yerevan": "Ереван",
  "Asia/Baku": "Баку",
  "Europe/Istanbul": "Стамбул",
  "Europe/London": "Лондон",
  "Europe/Paris": "Париж",
  "Europe/Berlin": "Берлин",
  "Europe/Rome": "Рим",
  "Europe/Madrid": "Мадрид",
  "Europe/Amsterdam": "Амстердам",
  "Europe/Prague": "Прага",
  "Europe/Warsaw": "Варшава",
  "Europe/Helsinki": "Хельсинки",
  "Asia/Dubai": "Дубай",
  "Asia/Kolkata": "Дели",
  "Asia/Bangkok": "Бангкок",
  "Asia/Singapore": "Сингапур",
  "Asia/Shanghai": "Пекин",
  "Asia/Hong_Kong": "Гонконг",
  "Asia/Seoul": "Сеул",
  "Asia/Tokyo": "Токио",
  "Australia/Sydney": "Сидней",
  "Pacific/Auckland": "Окленд",
  "Africa/Cairo": "Каир",
  "America/New_York": "Нью-Йорк",
  "America/Toronto": "Торонто",
  "America/Chicago": "Чикаго",
  "America/Denver": "Денвер",
  "America/Los_Angeles": "Лос-Анджелес",
  "America/Mexico_City": "Мехико",
  "America/Sao_Paulo": "Сан-Паулу",
  UTC: "UTC",
};

interface Zone {
  tz: string;
  name: string;
}

function normZones(v: unknown): Zone[] {
  if (!Array.isArray(v)) return [];
  return v
    .map((x): Zone | null => {
      if (typeof x === "string") return { tz: x, name: CITIES[x] ?? x.split("/").pop()!.replace(/_/g, " ") };
      if (x && typeof x === "object") {
        const o = x as Record<string, unknown>;
        const tz = String(o.tz ?? o.zone ?? o.timezone ?? "");
        if (!tz) return null;
        return { tz, name: String(o.name ?? o.city ?? CITIES[tz] ?? tz.split("/").pop()!.replace(/_/g, " ")) };
      }
      return null;
    })
    .filter((z): z is Zone => !!z);
}

function zoneNow(tz: string, now: Date): { time: string; hour: number; diffH: number; dayShift: number } | null {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).formatToParts(now);
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
    const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour") % 24, get("minute"));
    const localUtc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes());
    const diffH = Math.round(((asUtc - localUtc) / 3_600_000) * 2) / 2;
    const dayShift = Math.round((Date.UTC(get("year"), get("month") - 1, get("day")) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86_400_000);
    const hour = get("hour") % 24;
    return { time: `${String(hour).padStart(2, "0")}:${String(get("minute")).padStart(2, "0")}`, hour, diffH, dayShift };
  } catch {
    return null;
  }
}

function diffText(d: number): string {
  if (d === 0) return "как у вас";
  const abs = Math.abs(d);
  const n = Number.isInteger(abs) ? String(abs) : abs.toLocaleString("ru-RU");
  return `${d > 0 ? "+" : "−"}${n} ч`;
}

export function WorldClockWidget({ w, h, config, setConfig }: WidgetProps) {
  const zones = normZones(config.zones);
  const list = zones.length ? zones : normZones(["Europe/London", "America/New_York", "Asia/Tokyo"]);
  useTick(15_000);
  const now = new Date();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [add, setAdd] = useState("");
  const rowsFit = Math.max(1, Math.floor((h * 104 + (h - 1) * 12 - 50) / 40));
  const horizontal = h === 1 && w >= 2;
  const cityOptions = useMemo(
    () =>
      Object.entries(CITIES)
        .filter(([tz], i, arr) => arr.findIndex(([, n]) => n === CITIES[tz]) === i)
        .map(([tz, name]) => ({ value: tz, label: name }))
        .sort((a, b) => a.label.localeCompare(b.label, "ru")),
    [],
  );
  return (
    <WidgetFrame
      icon={<Globe2 />}
      title={(config.title as string) || "Мировое время"}
      actions={
        <CfgButton
          onClick={() => {
            setDraft(list.map((z) => z.tz));
            setAdd("");
            setOpen(true);
          }}
        />
      }
    >
      <div className={cx("wx-zones", horizontal && "row-mode")}>
        {list.slice(0, horizontal ? Math.max(1, w + 1) : rowsFit).map((z) => {
          const t = zoneNow(z.tz, now);
          if (!t) return null;
          const day = t.hour >= 7 && t.hour < 20;
          return (
            <div key={z.tz} className="wx-zone">
              <span className={cx("wx-zone-icon", day ? "day" : "night")}>{day ? <Sun /> : <Moon />}</span>
              <span className="grow" style={{ minWidth: 0 }}>
                <span className="wx-zone-name ellipsis">{z.name}</span>
                <span className="wx-zone-diff">
                  {diffText(t.diffH)}
                  {t.dayShift > 0 ? " · завтра" : t.dayShift < 0 ? " · вчера" : ""}
                </span>
              </span>
              <span className="wx-zone-time">{t.time}</span>
            </div>
          );
        })}
      </div>
      <WidgetSettingsModal open={open} title="Мировое время" onClose={() => setOpen(false)} onSave={() => setConfig({ ...config, zones: draft })}>
        <div className="wx-chosen" style={{ marginBottom: 12 }}>
          {draft.map((tz) => (
            <span key={tz} className="chip">
              {CITIES[tz] ?? tz}
              <button aria-label="Убрать" onClick={() => setDraft((d) => d.filter((x) => x !== tz))}>
                <X />
              </button>
            </span>
          ))}
          {!draft.length && <span className="faint small">Городов нет — покажу Лондон, Нью-Йорк и Токио</span>}
        </div>
        <label className="field-label">Добавить город</label>
        <Select
          value={add}
          placeholder="Выберите город"
          options={cityOptions.filter((o) => !draft.includes(o.value))}
          onChange={(v) => {
            setDraft((d) => [...d, v]);
            setAdd("");
          }}
        />
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}

// --- обратный отсчёт ------------------------------------------------------------------------------------
function parseDate(v: unknown): Date | null {
  if (!v) return null;
  const s = String(v);
  const d = /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(`${s}T00:00:00`) : new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function dateText(target: Date, today: Date): string {
  return target.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
    year: target.getFullYear() !== today.getFullYear() ? "numeric" : undefined,
  });
}

export function CountdownWidget({ w, h, config, setConfig }: WidgetProps) {
  useTick(60_000);
  const target = parseDate(config.date);
  const title = String(config.title ?? "").trim();
  const [open, setOpen] = useState(false);
  const [dTitle, setDTitle] = useState("");
  const [dDate, setDDate] = useState("");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = target ? new Date(target) : null;
  start?.setHours(0, 0, 0, 0);
  const days = start ? Math.round((start.getTime() - today.getTime()) / 86_400_000) : 0;
  const label = !title ? "до события" : /^до\s/i.test(title) ? title[0].toLowerCase() + title.slice(1) : title;
  const settings = (
    <CfgButton
      onClick={() => {
        setDTitle(title);
        setDDate(target ? `${target.getFullYear()}-${String(target.getMonth() + 1).padStart(2, "0")}-${String(target.getDate()).padStart(2, "0")}` : "");
        setOpen(true);
      }}
    />
  );
  return (
    <WidgetFrame
      icon={w >= 2 || h >= 2 ? <CalendarClock /> : undefined}
      title={w >= 2 || h >= 2 ? "Обратный отсчёт" : ""}
      actions={w >= 2 || h >= 2 ? settings : undefined}
      className={w >= 2 || h >= 2 ? undefined : "wx-floating"}
    >
      {w < 2 && h < 2 && <div className="wx-float-cfg">{settings}</div>}
      {!target ? (
        <div className="wx-empty">
          <span>Укажите дату события</span>
          <button className="wx-link" onClick={() => setOpen(true)}>
            Выбрать дату
          </button>
        </div>
      ) : (
        <div className={cx("wx-count", h >= 2 && "roomy")}>
          {days > 0 ? (
            <>
              <span className="wx-count-num">{days}</span>
              <span className="wx-count-text">
                <b>{plural(days, "день", "дня", "дней")}</b>
                <span className="ellipsis">
                  {label}
                  {w >= 2 && h < 2 && <span className="faint"> · {dateText(target, today)}</span>}
                </span>
              </span>
            </>
          ) : days === 0 ? (
            <span className="wx-count-text">
              <b className="wx-today">Сегодня!</b>
              <span className="ellipsis">{title || "Ваше событие"}</span>
            </span>
          ) : (
            <span className="wx-count-text">
              <b>{Math.abs(days)} {plural(Math.abs(days), "день", "дня", "дней")} назад</b>
              <span className="ellipsis">{title || "Событие прошло"}</span>
            </span>
          )}
          {h >= 2 && <span className="wx-count-date">{dateText(target, today)}</span>}
        </div>
      )}
      <WidgetSettingsModal
        open={open}
        title="Обратный отсчёт"
        onClose={() => setOpen(false)}
        onSave={() => setConfig({ ...config, title: dTitle.trim(), date: dDate })}
      >
        <label className="field-label">Событие</label>
        <TextInput value={dTitle} onChange={setDTitle} placeholder="До сессии, До отпуска…" />
        <label className="field-label" style={{ marginTop: 12 }}>
          Дата
        </label>
        <input className="input" type="date" value={dDate} onChange={(e) => setDDate(e.target.value)} />
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}

