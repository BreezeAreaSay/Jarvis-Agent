// Мелкие помощники раздела: названия моделей, числа, даты по-русски.

/** «gpt-5.4-mini» → «GPT-5.4 Mini», «openai/gpt-5.4» → «GPT-5.4», «claude-sonnet-4-5» → «Claude Sonnet 4.5». */
export function prettyModel(id: string | undefined | null): string {
  if (!id) return "Модель";
  const base = id.split("/").pop() ?? id;
  const tokens = base.split(/[-_]/).filter(Boolean);
  if (!tokens.length) return id;
  const out: string[] = [];
  tokens.forEach((t, i) => {
    const prev = out[out.length - 1];
    if (i === 0 && /^gpt$/i.test(t)) {
      out.push("GPT");
    } else if (prev === "GPT" && i === 1) {
      out[out.length - 1] = `GPT-${t}`;
    } else if (/^\d+$/.test(t) && prev && /\d$/.test(prev) && !prev.startsWith("GPT")) {
      out[out.length - 1] = `${prev}.${t}`;
    } else if (/^[a-zа-я]/i.test(t) && !/^\d/.test(t) && t.length > 1 && !/^(o\d|r\d)/i.test(t)) {
      out.push(t[0].toUpperCase() + t.slice(1));
    } else {
      out.push(t);
    }
  });
  return out.join(" ");
}

/** Склонение: plural(5, "токен", "токена", "токенов") → «токенов». */
export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("ru-RU");
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн токенов`;
  if (n >= 10_000) return `${(n / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} тыс. токенов`;
  return `${fmtInt(n)} ${plural(n, "токен", "токена", "токенов")}`;
}

export function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (Number.isInteger(v) && v >= 1) return `$${v}`;
  return `$${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}`;
}

const DAY = 86400_000;

export function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Короткое время для списка чатов: «сейчас», «5 мин», «14:20», «вчера», «16 окт.», «16.10.25». */
export function fmtShort(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "сейчас";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин`;
  const d = new Date(ts * 1000);
  const today = new Date();
  if (sameDay(d, today)) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (sameDay(d, addDays(today, -1))) return "вчера";
  if (d.getFullYear() === today.getFullYear()) return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
}

export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/** «сегодня», «завтра», «вчера», «пт, 24 окт.». */
export function fmtDayName(ts: number): string {
  const d = startOfDay(new Date(ts * 1000));
  const today = startOfDay(new Date());
  const diff = Math.round((d.getTime() - today.getTime()) / DAY);
  if (diff === 0) return "сегодня";
  if (diff === 1) return "завтра";
  if (diff === -1) return "вчера";
  const sameYear = d.getFullYear() === today.getFullYear();
  return d.toLocaleDateString("ru-RU", {
    weekday: "short",
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

/** «сегодня в 18:00». */
export function fmtWhen(ts: number): string {
  return `${fmtDayName(ts)} в ${fmtTime(ts)}`;
}

/** «через 2 ч», «через 5 мин», «3 дн. назад». */
export function fmtRelative(ts: number): string {
  const diff = ts - Date.now() / 1000;
  const abs = Math.abs(diff);
  let s: string;
  if (abs < 60) return diff >= 0 ? "сейчас" : "только что";
  if (abs < 3600) s = `${Math.round(abs / 60)} мин`;
  else if (abs < 86400) s = `${Math.round(abs / 3600)} ч`;
  else s = `${Math.round(abs / 86400)} дн.`;
  return diff >= 0 ? `через ${s}` : `${s} назад`;
}

/** Значение для <input type="datetime-local"> в местном времени. */
export function toLocalInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Значение для <input type="date">. */
export function toDateInput(d: Date): string {
  return toLocalInput(d).slice(0, 10);
}

export function fromDateInput(v: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}

export function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Домен ссылки без «www.» для подписи источника. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Ошибка → текст для человека. */
export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
