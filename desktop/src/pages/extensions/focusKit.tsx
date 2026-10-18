// «Фокус»: общее для живой панели на странице «Расширения», виджета стола и оверлея — фаза и её цвет,
// кольцо таймера, отсчёт секунд между событиями ядра (plugin.focus.state приходит при изменениях и раз
// в минуту, секунды считаем сами от left_s) и столбики минут за 7 дней.
import "@/styles/focus.css";
import { type CSSProperties, type ReactNode, useEffect, useState } from "react";
import type { FocusDay, FocusState, FocusStatus } from "@/api/types";
import { cx } from "@/ui";
import { type Plugin, plural } from "./pluginKit";
import { usePlugins } from "./pluginStore";

export type FocusTone = "focus" | "break" | "long" | "paused" | "idle";

export function isRunning(s: FocusState | null | undefined): boolean {
  return !!s && (s.phase === "focus" || s.phase === "break");
}

/** Цвет фазы: фокус — изумрудный, перерыв — голубой, длинный — сиреневый, пауза — янтарная. */
export function focusTone(s: FocusState | null | undefined): FocusTone {
  if (!s) return "idle";
  if (s.phase === "paused") return "paused";
  if (s.phase === "break") return s.long_break ? "long" : "break";
  return s.phase === "focus" ? "focus" : "idle";
}

export function focusLabel(s: FocusState | null | undefined): string {
  switch (s?.phase) {
    case "focus":
      return "Фокус";
    case "break":
      return s.long_break ? "Длинный перерыв" : "Перерыв";
    case "paused":
      return s.reason === "idle" ? "Пауза · вы отошли" : "Пауза";
    default:
      return s?.waiting ? "Перерыв окончен" : "Готов";
  }
}

/** Секунды → «18:42» или «1:05:00» (с округлением вверх: на старте — ровно «25:00»). */
export function focusClock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds - 0.05));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${String(m).padStart(2, "0")}:${ss}`;
}

/** Минуты коротко: «45 мин», «1 ч 15 мин». */
export function fmtMinutes(min: number): string {
  const m = Math.max(0, Math.round(min));
  const h = Math.floor(m / 60);
  const rest = m % 60;
  if (!h) return `${m} мин`;
  return rest ? `${h} ч ${rest} мин` : `${h} ч`;
}

export function sessionsText(n: number): string {
  return `${n} ${plural(n, "сессия", "сессии", "сессий")}`;
}

export function daysText(n: number): string {
  return `${n} ${plural(n, "день", "дня", "дней")}`;
}

/** Доля оставшегося времени для кольца (0..1); без таймера — полное кольцо. */
export function leftFrac(s: FocusState | null | undefined, left: number): number {
  if (!s || s.phase === "idle" || !s.total_s) return 1;
  return Math.max(0, Math.min(1, left / s.total_s));
}

/** Сколько осталось сейчас: left_s минус время с прихода состояния (если таймер идёт). */
export function useFocusLeft(state: FocusState | null | undefined, receivedAt: number): number {
  const running = isRunning(state);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(t);
  }, [running]);
  if (!state) return 0;
  if (!running) return Math.max(0, state.left_s);
  return Math.max(0, state.left_s - (Date.now() - receivedAt) / 1000);
}

/** Состояние для панели и виджета: свежее из события plugin.focus.state, иначе — из статуса плагина
 *  (статус обновляется с plugins.changed; событие, пришедшее раньше статуса, уже устарело — скажем, ядро
 *  перезапустилось, пока окно было открыто). */
export function useFocusState(plugin?: Plugin | null): { state: FocusStatus | null; at: number } {
  const live = usePlugins((s) => s.live.focus?.state);
  const status = plugin?.status as (Partial<FocusStatus> & { running?: boolean }) | undefined;
  const [statusAt, setStatusAt] = useState(() => Date.now());
  useEffect(() => setStatusAt(Date.now()), [status]);
  if (!status || typeof status.phase !== "string") return { state: null, at: statusAt };
  const on = !!plugin?.enabled && status.running !== false;
  if (on && live && live.ts >= statusAt) {
    return { state: { ...(status as FocusStatus), ...(live.data as Partial<FocusState>) }, at: live.ts };
  }
  return { state: status as FocusStatus, at: statusAt };
}

/** Кольцо таймера: дуга — доля оставшегося времени; прошедшее «съедается» по часовой стрелке от 12 часов. */
export function FocusRing({
  size,
  frac,
  tone,
  stroke = 7,
  className,
  children,
}: {
  size: number;
  frac: number;
  tone: FocusTone;
  stroke?: number;
  className?: string;
  children?: ReactNode;
}) {
  const r = 50 - stroke / 2 - 1.5;
  const c = 2 * Math.PI * r;
  const f = Math.max(0, Math.min(1, frac));
  return (
    <span className={cx("fx-ring", `fxt-${tone}`, className)} style={{ "--fx-size": `${size}px` } as CSSProperties}>
      <svg viewBox="0 0 100 100" aria-hidden>
        <circle className="fx-ring-track" cx="50" cy="50" r={r} strokeWidth={stroke} />
        {f > 0.002 && (
          <circle
            className="fx-ring-bar"
            cx="50"
            cy="50"
            r={r}
            strokeWidth={stroke}
            strokeDasharray={`${c * f} ${c}`}
            strokeDashoffset={-c * (1 - f)}
          />
        )}
      </svg>
      {children && <span className="fx-ring-in">{children}</span>}
    </span>
  );
}

// --- неделя -------------------------------------------------------------------------------------------
const WEEKDAY = new Intl.DateTimeFormat("ru-RU", { weekday: "short" });
const LONG_DAY = new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long" });

function dayDate(iso: string): Date {
  return new Date(`${iso}T12:00:00`);
}

function capital(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Пустая неделя (ядро ещё не ответило): 7 дней до сегодня. */
function emptyWeek(): FocusDay[] {
  const now = new Date();
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(now);
    d.setDate(now.getDate() - (6 - i));
    return { date: isoDay(d), min: 0, sessions: 0 };
  });
}

/** Столбики минут фокуса за 7 дней: сегодня — ярче и с подписью, остальное — в подсказке (compact — в
 *  системной подсказке: виджет обрезает всё, что выходит за его край). */
export function WeekBars({ days, className, compact }: { days?: FocusDay[] | null; className?: string; compact?: boolean }) {
  const list = days && days.length ? days : emptyWeek();
  const max = Math.max(30, ...list.map((d) => d.min));
  return (
    <div className={cx("fx-week", className)} role="list" aria-label="Минуты фокуса за неделю">
      {list.map((d, i) => {
        const today = i === list.length - 1;
        const pct = d.min > 0 ? Math.max(5, (d.min / max) * 100) : 0;
        const when = capital(LONG_DAY.format(dayDate(d.date)));
        const detail = d.sessions ? ` · ${sessionsText(d.sessions)}` : "";
        return (
          <div
            key={d.date}
            role="listitem"
            tabIndex={0}
            className={cx("fx-week-col", today && "today")}
            aria-label={`${when}: ${fmtMinutes(d.min)}${detail}`}
            title={compact ? `${when}: ${fmtMinutes(d.min)}${detail}` : undefined}
          >
            <span className="fx-week-plot">
              <span className={cx("fx-week-bar", !d.min && "zero")} style={{ height: `${pct}%` }}>
                {today && d.min > 0 && <span className="fx-week-val">{d.min}</span>}
              </span>
            </span>
            <span className="fx-week-day">{WEEKDAY.format(dayDate(d.date))}</span>
            {!compact && (
              <span className="fx-week-tip" aria-hidden>
                <b>{fmtMinutes(d.min)}</b>
                {detail}
                <span>{when}</span>
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
