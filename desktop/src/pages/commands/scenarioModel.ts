// Сценарии: подписи триггеров («Будни 8:00»), значки с градиентами, сводка действий, «вчера в 8:00 ✓»,
// заготовки новых триггеров и проверка черновика перед сохранением. Формат — docs/CONTRACTS.md §6.
import {
  AppWindow,
  BatteryCharging,
  BatteryLow,
  BellRing,
  BookOpen,
  Briefcase,
  CalendarClock,
  Clock3,
  Coffee,
  DoorOpen,
  Eye,
  Gamepad2,
  Hand,
  Heart,
  House,
  type LucideIcon,
  MessageSquareQuote,
  Monitor,
  Moon,
  Music,
  Power,
  Repeat,
  Rocket,
  Sparkles,
  Sunrise,
  Sunset,
  SquareX,
  UserRoundCheck,
  Zap,
} from "lucide-react";
import type {
  ActionType,
  AppEntry,
  CommandAction,
  Scenario,
  ScenarioConditions,
  ScenarioTrigger,
  ScenarioTriggerType,
} from "@/api/types";
import { actionLine, isRequired, plural } from "./model";

// --- триггеры ----------------------------------------------------------------------------------------
export interface TriggerMeta {
  title: string;
  hint: string;
  icon: LucideIcon;
  /** слоты, которые триггер передаёт действиям */
  slots: string[];
}

export const TRIGGERS: Record<ScenarioTriggerType, TriggerMeta> = {
  time: { title: "По расписанию", hint: "В заданное время по дням недели", icon: Clock3, slots: [] },
  phrase: { title: "Фраза", hint: "Скажите или напишите её Jarvis", icon: MessageSquareQuote, slots: [] },
  app_open: { title: "Открыта программа", hint: "Когда запускается программа", icon: AppWindow, slots: ["app"] },
  app_close: { title: "Закрыта программа", hint: "Когда программу закрыли", icon: SquareX, slots: ["app"] },
  interval: { title: "Каждые N минут", hint: "Повторять через промежуток", icon: Repeat, slots: ["number"] },
  idle_return: {
    title: "Возвращение за компьютер",
    hint: "Вы вернулись после перерыва",
    icon: UserRoundCheck,
    slots: ["number"],
  },
  calendar_event: {
    title: "Перед событием",
    hint: "За несколько минут до события календаря",
    icon: CalendarClock,
    slots: ["text", "number"],
  },
  battery_low: { title: "Низкий заряд", hint: "Аккумулятор садится", icon: BatteryLow, slots: ["number"] },
  jarvis_start: { title: "Запуск Jarvis", hint: "Когда Jarvis включается", icon: Power, slots: [] },
};

export const TRIGGER_ORDER: ScenarioTriggerType[] = [
  "time",
  "phrase",
  "app_open",
  "app_close",
  "interval",
  "idle_return",
  "calendar_event",
  "battery_low",
  "jarvis_start",
];

export const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const WORKDAYS = [0, 1, 2, 3, 4];

export function weekdaysLabel(days: number[] | undefined): string {
  const d = [...new Set(days ?? [])].sort((a, b) => a - b);
  if (!d.length || d.length === 7) return "Каждый день";
  if (d.join() === WORKDAYS.join()) return "Будни";
  if (d.join() === "5,6") return "Выходные";
  return d.map((i) => WEEKDAYS[i]).join(", ");
}

/** «08:00» → «8:00». */
export function clockLabel(at: string): string {
  const m = /^(\d{1,2}):(\d{2})$/.exec(at.trim());
  return m ? `${Number(m[1])}:${m[2]}` : at;
}

/** Подпись триггера, как в ядре: «Будни 8:00», «Фраза: доброе утро», «Открыт Steam». */
export function triggerLabel(t: ScenarioTrigger): string {
  switch (t.type) {
    case "time":
      return `${weekdaysLabel(t.weekdays)} ${clockLabel(t.at)}`;
    case "interval":
      return (t.minutes === 1 ? "Каждую минуту" : `Каждые ${t.minutes} мин`) + (t.only_active ? " активности" : "");
    case "phrase": {
      const extra = t.phrases.length - 1;
      return `Фраза: ${t.phrases[0] ?? "—"}${extra > 0 ? ` +${extra}` : ""}`;
    }
    case "jarvis_start":
      return "При запуске Jarvis";
    case "app_open":
      return `Открыт ${t.app || "…"}`;
    case "app_close":
      return `Закрыт ${t.app || "…"}`;
    case "idle_return":
      return `Возврат после ${t.after_min} мин`;
    case "battery_low":
      return `Заряд ниже ${t.percent}%`;
    case "calendar_event":
      return t.before_min === 0 ? "В начале события" : `За ${t.before_min} мин до события`;
  }
}

/** Новый триггер с разумными значениями. */
export function newTrigger(type: ScenarioTriggerType): ScenarioTrigger {
  switch (type) {
    case "time":
      return { type, at: "09:00", weekdays: [...WORKDAYS] };
    case "interval":
      return { type, minutes: 30, only_active: true };
    case "phrase":
      return { type, phrases: [] };
    case "app_open":
    case "app_close":
      return { type, app: "" };
    case "idle_return":
      return { type, after_min: 15 };
    case "battery_low":
      return { type, percent: 20 };
    case "calendar_event":
      return { type, before_min: 10 };
    case "jarvis_start":
      return { type };
  }
}

/** Что не так с триггером (для подсветки) или "". */
export function triggerError(t: ScenarioTrigger): string {
  const n = (v: number, lo: number, hi: number) => Number.isInteger(v) && v >= lo && v <= hi;
  switch (t.type) {
    case "time":
      return /^\d{1,2}:\d{2}$/.test(t.at) ? "" : "Укажите время";
    case "interval":
      return n(t.minutes, 1, 1440) ? "" : "Интервал — от 1 до 1440 минут";
    case "phrase":
      return t.phrases.some((p) => p.trim()) ? "" : "Добавьте фразу";
    case "app_open":
    case "app_close":
      return t.app.trim() ? "" : "Выберите программу";
    case "idle_return":
      return n(t.after_min, 1, 1440) ? "" : "Отсутствие — от 1 до 1440 минут";
    case "battery_low":
      return n(t.percent, 1, 99) ? "" : "Заряд — от 1 до 99%";
    case "calendar_event":
      return n(t.before_min, 0, 1440) ? "" : "От 0 до 1440 минут";
    default:
      return "";
  }
}

/** Слоты, доступные действиям при таких триггерах: «{number}», «{app}»… — строкой для предупреждений редактора. */
export function availableSlots(triggers: ScenarioTrigger[]): string {
  const out = new Set<string>();
  for (const t of triggers) {
    for (const s of TRIGGERS[t.type].slots) out.add(`{${s}}`);
    if (t.type === "phrase") for (const p of t.phrases) for (const m of p.matchAll(/\{(\w+)\}/g)) out.add(`{${m[1]}}`);
  }
  return [...out].join(" ");
}

// --- условия -----------------------------------------------------------------------------------------
export function conditionsActive(c: ScenarioConditions | undefined): boolean {
  return !!c && (!!c.weekdays?.length || !!c.time_from || !!c.time_to);
}

/** «Будни, 9:00–18:00» / «с 22:00» / «" — без ограничений. */
export function conditionsLabel(c: ScenarioConditions | undefined): string {
  if (!conditionsActive(c)) return "";
  const parts: string[] = [];
  if (c?.weekdays?.length) parts.push(weekdaysLabel(c.weekdays));
  const from = c?.time_from ? clockLabel(c.time_from) : "";
  const to = c?.time_to ? clockLabel(c.time_to) : "";
  if (from && to) parts.push(`${from}–${to}`);
  else if (from) parts.push(`с ${from}`);
  else if (to) parts.push(`до ${to}`);
  return parts.join(", ");
}

// --- значки ------------------------------------------------------------------------------------------
export interface ScenarioIconMeta {
  icon: LucideIcon;
  label: string;
  from: string;
  to: string;
}

export const SCENARIO_ICONS: Record<string, ScenarioIconMeta> = {
  sparkles: { icon: Sparkles, label: "Волшебство", from: "#6d8cff", to: "#9b6bff" },
  sunrise: { icon: Sunrise, label: "Утро", from: "#ffb347", to: "#ff6f61" },
  sunset: { icon: Sunset, label: "Вечер", from: "#ff8a5c", to: "#b04ad8" },
  moon: { icon: Moon, label: "Ночь", from: "#5b6cff", to: "#2b2f8f" },
  door: { icon: DoorOpen, label: "Ухожу", from: "#4fd1c5", to: "#2b7a9b" },
  gamepad: { icon: Gamepad2, label: "Игры", from: "#a855f7", to: "#ec4899" },
  eye: { icon: Eye, label: "Глаза", from: "#34d399", to: "#0e9f9a" },
  wave: { icon: Hand, label: "Привет", from: "#fbbf24", to: "#f97316" },
  coffee: { icon: Coffee, label: "Перерыв", from: "#c08457", to: "#7c4a2d" },
  bolt: { icon: Zap, label: "Энергия", from: "#facc15", to: "#f59e0b" },
  briefcase: { icon: Briefcase, label: "Работа", from: "#60a5fa", to: "#3b5bdb" },
  battery: { icon: BatteryCharging, label: "Батарея", from: "#4ade80", to: "#15803d" },
  calendar: { icon: CalendarClock, label: "Календарь", from: "#f472b6", to: "#db2777" },
  bell: { icon: BellRing, label: "Напоминание", from: "#fb7185", to: "#e11d48" },
  music: { icon: Music, label: "Музыка", from: "#22d3ee", to: "#6366f1" },
  home: { icon: House, label: "Дом", from: "#38bdf8", to: "#0369a1" },
  rocket: { icon: Rocket, label: "Старт", from: "#818cf8", to: "#c026d3" },
  heart: { icon: Heart, label: "Забота", from: "#fb7185", to: "#be185d" },
  book: { icon: BookOpen, label: "Учёба", from: "#a3e635", to: "#4d7c0f" },
  monitor: { icon: Monitor, label: "Компьютер", from: "#94a3b8", to: "#475569" },
};

export const ICON_KEYS = Object.keys(SCENARIO_ICONS);

// --- действия и запуски ------------------------------------------------------------------------------
/** «Сводка дня», «Выключить звук +1», «Скажет: Отдохните…». */
export function actionsSummary(
  actions: CommandAction[],
  say: string,
  byType: Map<string, ActionType>,
  apps: AppEntry[],
): { text: string; more: number } {
  if (!actions.length) return { text: say ? `Скажет: «${say}»` : "Без действий", more: 0 };
  const first = actions[0];
  return { text: actionLine(first, byType.get(first.type), apps), more: actions.length - 1 };
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** «сегодня в 8:00», «вчера в 18:30», «12 окт в 9:00». */
export function whenText(ts: number, now = Date.now()): string {
  const d = new Date(ts * 1000);
  const today = new Date(now);
  const time = `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (sameDay(d, today)) {
    const ago = now / 1000 - ts;
    if (ago >= 0 && ago < 60) return "только что";
    return `сегодня в ${time}`;
  }
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (sameDay(d, y)) return `вчера в ${time}`;
  return `${d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }).replace(".", "")} в ${time}`;
}

export const W_SCENARIOS: [string, string, string] = ["сценарий", "сценария", "сценариев"];
export const W_RUNS: [string, string, string] = ["запуск", "запуска", "запусков"];

export function runsText(n: number): string {
  return `${n.toLocaleString("ru-RU")} ${plural(n, W_RUNS)}`;
}

// --- черновик редактора --------------------------------------------------------------------------------
export interface DraftErrors {
  name: boolean;
  triggers: boolean;
  triggerErrors: string[];
  work: boolean;
  values: boolean;
}

export function draftErrors(
  d: { name: string; triggers: ScenarioTrigger[]; actions: CommandAction[]; say: string },
  byType: Map<string, ActionType>,
): DraftErrors {
  const triggerErrors = d.triggers.map(triggerError);
  return {
    name: !d.name.trim(),
    triggers: d.triggers.length === 0 || triggerErrors.some(Boolean),
    triggerErrors,
    work: d.actions.length === 0 && !d.say.trim(),
    values: d.actions.some((a) => isRequired(byType.get(a.type)) && !a.value.trim()),
  };
}

/** Сценарий для правки: последний запуск для подписи в шапке. */
export function lastRunLine(s: Scenario): string {
  if (!s.last_run) return "Ещё не запускался";
  return `${s.last_ok ? "Выполнен" : "Не удался"} ${whenText(s.last_run)}`;
}
