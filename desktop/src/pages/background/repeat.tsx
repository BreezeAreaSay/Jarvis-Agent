// Повторы фоновых задач в интерфейсе: выбор расписания (каждый день, по будням, раз в неделю, каждые N часов),
// «когда» словами («завтра в 21:00»). Ядро — Task.repeat (docs/CONTRACTS.md §8).
import type { TaskRepeat } from "@/store/background";
import { Select } from "@/ui";

type Mode = "none" | "daily" | "workdays" | "weekend" | "weekly" | "custom" | "hours";

const WORKDAYS = [0, 1, 2, 3, 4];
const WEEKEND = [5, 6];
const DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];
const SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const MODES: { value: Mode; label: string }[] = [
  { value: "none", label: "Не повторять" },
  { value: "daily", label: "Каждый день" },
  { value: "workdays", label: "По будням" },
  { value: "weekend", label: "По выходным" },
  { value: "weekly", label: "Раз в неделю" },
  { value: "hours", label: "Каждые N часов" },
];

function same(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function modeOf(repeat: TaskRepeat): Mode {
  if (!("kind" in repeat)) return "none";
  if (repeat.kind === "hours") return "hours";
  const days = [...repeat.weekdays].sort();
  if (!days.length) return "daily";
  if (same(days, WORKDAYS)) return "workdays";
  if (same(days, WEEKEND)) return "weekend";
  return days.length === 1 ? "weekly" : "custom";
}

/** Выбор расписания: что повторять, во сколько, в какой день, раз в сколько часов. */
export function RepeatPicker({ value, onChange }: { value: TaskRepeat; onChange: (r: TaskRepeat) => void }) {
  const mode = modeOf(value);
  // свой набор дней (задан моделью или в API: «по Пн, Ср, Пт») — свой пункт, чтобы смена времени его не сбрасывала
  const custom = "kind" in value && value.kind === "daily" && mode === "custom" ? [...value.weekdays].sort() : [];
  const modes = custom.length ? [...MODES.slice(0, 5), { value: "custom" as Mode, label: `По ${custom.map((d) => SHORT[d]).join(", ")}` }, MODES[5]] : MODES;
  const at = "kind" in value && value.kind === "daily" ? value.at : "09:00";
  const day = "kind" in value && value.kind === "daily" && value.weekdays.length === 1 ? value.weekdays[0] : new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
  const hours = "kind" in value && value.kind === "hours" ? value.hours : 3;
  const set = (next: Mode, patch: { at?: string; day?: number; hours?: number } = {}) => {
    const time = patch.at ?? at;
    if (next === "none") onChange({});
    else if (next === "hours") onChange({ kind: "hours", hours: Math.max(1, Math.min(168, Math.round(patch.hours ?? hours) || 1)) });
    else {
      const weekdays =
        next === "daily" ? [] : next === "workdays" ? WORKDAYS : next === "weekend" ? WEEKEND : next === "custom" ? custom : [patch.day ?? day];
      onChange({ kind: "daily", at: time, weekdays });
    }
  };
  return (
    <div className="bg-repeat">
      <Select<Mode> value={mode} options={modes} onChange={(v) => set(v)} width={190} />
      {mode === "weekly" && (
        <Select<string>
          value={String(day)}
          options={DAYS.map((label, i) => ({ value: String(i), label }))}
          onChange={(v) => set("weekly", { day: Number(v) })}
          width={160}
        />
      )}
      {mode !== "none" && mode !== "hours" && (
        <label className="bg-repeat-field">
          в
          <input className="input" type="time" value={at} onChange={(e) => e.target.value && set(mode, { at: e.target.value })} />
        </label>
      )}
      {mode === "hours" && (
        <label className="bg-repeat-field">
          каждые
          <input
            className="input"
            type="number"
            min={1}
            max={168}
            value={hours}
            onChange={(e) => set("hours", { hours: Number(e.target.value) })}
          />
          ч
        </label>
      )}
    </div>
  );
}

/** «сегодня в 21:00», «завтра в 9:00», «3 ноября в 10:00». */
export function fmtWhen(ts: number): string {
  if (!ts) return "";
  const moment = new Date(ts * 1000);
  const now = new Date();
  const time = moment.toLocaleTimeString("ru-RU", { hour: "numeric", minute: "2-digit" });
  const dayOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((dayOf(moment) - dayOf(now)) / 86_400_000);
  if (diff === 0) return `сегодня в ${time}`;
  if (diff === 1) return `завтра в ${time}`;
  if (diff === -1) return `вчера в ${time}`;
  return `${moment.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })} в ${time}`;
}
