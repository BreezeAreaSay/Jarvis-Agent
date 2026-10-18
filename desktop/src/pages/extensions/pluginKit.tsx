// Общее для плагинов: значки (в духе lucide, как на плитках Luxify), глянцевые плитки, цвета, запасные
// описания девяти плагинов (если ядро не ответило), подписи состояний. Используется страницей «Расширения»
// и виджетом «Плагины».
import { BellRing, Puzzle } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import type { PluginInfo } from "@/api/types";
import { cx } from "@/ui";
import { JellyIcon } from "@/widgets/extra/PixelJelly";

/** Плагин, как его отдаёт ядро (+ поля, которых нет в общем типе). */
export type Plugin = PluginInfo & { examples?: string[] };

export const PLUGIN_ORDER = [
  "smart_home",
  "dictation",
  "subtitles",
  "summary",
  "reader",
  "autolock",
  "second_pc",
  "notify_voice",
  "pet",
  "focus",
  "clipboard",
  "file_search",
];

const FALLBACK: Record<string, { name: string; description: string; icon: string; color: [string, string] }> = {
  smart_home: { name: "Умный дом", description: "Свет, розетки и климат — голосом", icon: "home", color: ["#FDBA74", "#F97316"] },
  dictation: { name: "Умная диктовка", description: "Говорите — текст появляется там, где курсор", icon: "mic", color: ["#A78BFA", "#7C3AED"] },
  subtitles: { name: "Живые субтитры", description: "Текст того, что звучит, — с переводом", icon: "subtitles", color: ["#38BDF8", "#0284C7"] },
  summary: { name: "Конспект видео и созвонов", description: "О чём говорили, решения и задачи", icon: "file-text", color: ["#F87171", "#DC2626"] },
  reader: { name: "Чтение вслух", description: "Статьи и книги голосом ассистента", icon: "book-open", color: ["#F472B6", "#DB2777"] },
  autolock: { name: "Блокировка, когда отходишь", description: "Компьютер блокируется, если вы отошли", icon: "lock", color: ["#2DD4BF", "#0D9488"] },
  second_pc: { name: "Второй компьютер", description: "Команды и файлы на другом ПК", icon: "monitor-smartphone", color: ["#818CF8", "#4F46E5"] },
  notify_voice: { name: "Озвучка уведомлений", description: "Читает вслух важные уведомления", icon: "bell", color: ["#FCD34D", "#F59E0B"] },
  pet: { name: "Питомец", description: "Пиксельный друг на рабочем столе", icon: "jellyfish", color: ["#E879F9", "#A21CAF"] },
  focus: { name: "Фокус", description: "Помодоро, тишина и защита от отвлечений", icon: "focus", color: ["#6EE7B7", "#059669"] },
  clipboard: { name: "Умный буфер", description: "История буфера и ИИ для выделенного текста", icon: "clipboard", color: ["#BEF264", "#65A30D"] },
  file_search: { name: "Поиск по ПК", description: "Файлы и папки — по имени, типу и дате", icon: "file-search", color: ["#93C5FD", "#2563EB"] },
};

/** Девять плагинов-заглушек, пока ядро не ответило (плитки видны, переключатели — нет). */
export function fallbackPlugins(): Plugin[] {
  return PLUGIN_ORDER.map((id) => ({
    id,
    ...FALLBACK[id],
    enabled: false,
    config: {},
    schema: [],
    status: {},
    actions: [],
  }));
}

export function sortPlugins(items: Plugin[]): Plugin[] {
  const rank = (id: string) => {
    const i = PLUGIN_ORDER.indexOf(id);
    return i < 0 ? 99 : i;
  };
  return [...items].sort((a, b) => rank(a.id) - rank(b.id));
}

export function pluginColors(p: Pick<Plugin, "id" | "color">): [string, string] {
  const c = p.color;
  if (Array.isArray(c) && c.length >= 2 && c[0] && c[1]) return [c[0], c[1]];
  if (Array.isArray(c) && c.length === 1 && c[0]) return [c[0], c[0]];
  if (typeof c === "string" && c) return [c, c];
  return FALLBACK[p.id]?.color ?? ["#64748B", "#475569"];
}

export function colorVars(p: Pick<Plugin, "id" | "color">): CSSProperties {
  const [c1, c2] = pluginColors(p);
  return { "--c1": c1, "--c2": c2 } as CSSProperties;
}

// --- значки ----------------------------------------------------------------------------------------
function Glyph({ children, stroke = 1.9 }: { children: ReactNode; stroke?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {children}
    </svg>
  );
}

const Spark = ({ x, y, r }: { x: number; y: number; r: number }) => (
  <path
    d={`M${x} ${y - r}l${r * 0.32} ${r * 0.68} ${r * 0.68} ${r * 0.32}-${r * 0.68} ${r * 0.32}-${r * 0.32} ${r * 0.68}-${r * 0.32}-${r * 0.68}-${r * 0.68}-${r * 0.32} ${r * 0.68}-${r * 0.32}z`}
    fill="currentColor"
    stroke="none"
  />
);

const GLYPHS: Record<string, () => ReactNode> = {
  home: () => (
    <Glyph>
      <path d="M3.4 10.4 12 3.6l8.6 6.8" />
      <path d="M5.6 9v10.2a1.6 1.6 0 0 0 1.6 1.6h9.6a1.6 1.6 0 0 0 1.6-1.6V9" />
      <path d="M12 9.4a3 3 0 0 0-1.85 5.36v1.44h3.7v-1.44A3 3 0 0 0 12 9.4Z" />
      <path d="M10.7 18.4h2.6" />
    </Glyph>
  ),
  mic: () => (
    <Glyph>
      <rect x="8.2" y="2.8" width="7" height="11.6" rx="3.5" />
      <path d="M10.6 7h2.2M10.6 10h2.2" />
      <path d="M5.2 11.4a6.5 6.5 0 0 0 13 0" />
      <path d="M11.7 17.9V21M9 21h5.4" />
      <Spark x={19.6} y={4.2} r={2.1} />
      <Spark x={4.4} y={4.6} r={1.3} />
    </Glyph>
  ),
  subtitles: () => (
    <Glyph>
      <rect x="2.6" y="3.4" width="18.8" height="13.6" rx="2.6" />
      <path d="M10.4 6.6v4.6l3.8-2.3Z" fill="currentColor" />
      <path d="M6.4 14h6.2M14.8 14h2.8" />
      <path d="M12 17v3.6M8.6 20.6h6.8" />
    </Glyph>
  ),
  summary: () => (
    <Glyph>
      <path d="M14 2.6H7.2a2 2 0 0 0-2 2v14.8a2 2 0 0 0 2 2h9.6a2 2 0 0 0 2-2V7.4Z" />
      <path d="M14 2.6v4.8h4.8" />
      <path d="M8.4 5.8v2.2M10.2 5v3.8M12 6.2v1.4" />
      <path d="m8.3 12.6 1.3 1.3 2.3-2.4M13.6 12.8h2.6" />
      <path d="m8.3 16.6 1.3 1.3 2.3-2.4M13.6 16.8h2.6" />
    </Glyph>
  ),
  reader: () => (
    <Glyph>
      <path d="M1.8 6.2h4.6a2.6 2.6 0 0 1 2.6 2.6v10.4a2 2 0 0 0-2-2H1.8Z" />
      <path d="M16.2 6.2h-4.6A2.6 2.6 0 0 0 9 8.8v10.4a2 2 0 0 1 2-2h5.2Z" />
      <path d="M3.9 9.8h2.4M3.9 12.8h2.4M11.7 9.8h2.4M11.7 12.8h2.4" />
      <path d="M18.6 9.4a3.4 3.4 0 0 1 0 5.2" />
      <path d="M20.8 7.4a6.4 6.4 0 0 1 0 9.2" />
    </Glyph>
  ),
  lock: () => (
    <Glyph>
      <rect x="2.8" y="10.4" width="11.4" height="10" rx="2.2" />
      <path d="M5.4 10.4V7.8a3.1 3.1 0 0 1 6.2 0v2.6" />
      <path d="M8.5 14.3v2.2" />
      <rect x="16.6" y="10.6" width="4.8" height="8.4" rx="1.3" />
      <path d="M17.2 7.4a3.6 3.6 0 0 1 3.6 0" />
      <path d="M16 4.9a6.2 6.2 0 0 1 6 0" />
    </Glyph>
  ),
  second_pc: () => (
    <Glyph>
      <rect x="2.4" y="3" width="11.2" height="8" rx="1.6" />
      <path d="M8 11v2.4M5.8 13.4h4.4" />
      <rect x="10.4" y="11.2" width="11.2" height="7.6" rx="1.6" />
      <path d="M16 18.8v2.4M13.8 21.2h4.4" />
      <path d="M16.4 3.4h2.4a1.6 1.6 0 0 1 1.6 1.6v2.6" />
      <path d="m18.8 6.2 1.6 1.6L22 6.2" />
      <path d="M7.6 20.6H5.2a1.6 1.6 0 0 1-1.6-1.6v-2.6" />
      <path d="M2 17.8l1.6-1.6 1.6 1.6" />
    </Glyph>
  ),
  bell: () => <BellRing strokeWidth={1.9} />,
  focus: () => (
    <Glyph>
      <circle cx="10.6" cy="13.4" r="8.2" />
      <circle cx="10.6" cy="13.4" r="4.4" />
      <circle cx="10.6" cy="13.4" r="1.25" fill="currentColor" stroke="none" />
      <path d="m10.6 13.4 8.2-8.2" />
      <path d="M18.8 1.8v3.4h3.4" />
    </Glyph>
  ),
  clipboard: () => (
    <Glyph>
      <rect x="3.6" y="4.6" width="12.8" height="16.6" rx="2.4" />
      <rect x="7.2" y="2.8" width="5.6" height="3.6" rx="1.2" />
      <path d="M7 11h6M7 14.2h6M7 17.4h3.6" />
      <path d="M19.4 9.4v9.4a3.4 3.4 0 0 1-3.4 3.4H8.4" />
      <Spark x={19.6} y={4.4} r={2.2} />
    </Glyph>
  ),
  file_search: () => (
    <Glyph>
      <path d="M2.8 17V6.6a1.8 1.8 0 0 1 1.8-1.8h3.9l2 2.2h7.2a1.8 1.8 0 0 1 1.8 1.8v1.6" />
      <path d="M2.8 17a1.8 1.8 0 0 0 1.8 1.8h5.6" />
      <circle cx="15.6" cy="15.2" r="3.9" />
      <path d="m18.5 18.1 2.9 2.9" />
      <path d="M13.9 13.8a2 2 0 0 1 1.4-.6" />
    </Glyph>
  ),
};

const ICON_ALIASES: Record<string, string> = {
  smart_home: "home",
  house: "home",
  "house-plug": "home",
  dictation: "mic",
  "mic-vocal": "mic",
  captions: "subtitles",
  "monitor-play": "subtitles",
  "file-text": "summary",
  notes: "summary",
  "book-open": "reader",
  book: "reader",
  "book-audio": "reader",
  autolock: "lock",
  "lock-keyhole": "lock",
  "monitor-smartphone": "second_pc",
  monitor: "second_pc",
  computer: "second_pc",
  pc: "second_pc",
  laptop: "second_pc",
  "bell-ring": "bell",
  notification: "bell",
  notify_voice: "bell",
  notifications: "bell",
  pet: "jellyfish",
  fish: "jellyfish",
  paw: "jellyfish",
  target: "focus",
  "clipboard-list": "clipboard",
  "clipboard-paste": "clipboard",
  "file-search": "file_search",
  "folder-search": "file_search",
};

function glyphKey(p: Pick<Plugin, "id" | "icon">): string {
  for (const key of [p.icon, p.id]) {
    if (!key) continue;
    const k = ICON_ALIASES[key] ?? key;
    if (k === "jellyfish" || GLYPHS[k]) return k;
  }
  return FALLBACK[p.id]?.icon ? (ICON_ALIASES[FALLBACK[p.id].icon] ?? FALLBACK[p.id].icon) : "";
}

export function PluginGlyph({ plugin }: { plugin: Pick<Plugin, "id" | "icon"> }) {
  const key = glyphKey(plugin);
  if (key === "jellyfish") return <JellyIcon />;
  const make = GLYPHS[key];
  return <>{make ? make() : <Puzzle strokeWidth={1.9} />}</>;
}

/** Глянцевая плитка плагина с градиентом его цвета. */
export function PluginTile({
  plugin,
  size = 72,
  className,
  dim,
}: {
  plugin: Pick<Plugin, "id" | "icon" | "color">;
  size?: number;
  className?: string;
  dim?: boolean;
}) {
  const pet = glyphKey(plugin) === "jellyfish";
  return (
    <span
      className={cx("ptile", pet && "pet", dim && "dim", className)}
      style={{ ...colorVars(plugin), "--size": `${size}px` } as CSSProperties}
      aria-hidden
    >
      <PluginGlyph plugin={plugin} />
    </span>
  );
}

// --- состояние ---------------------------------------------------------------------------------------
export type PluginHealth = "off" | "starting" | "ready" | "on" | "error";

/** ready — плагин включён, но его сеанс не идёт (субтитры не запущены, диктовка ждёт клавишу). */
export function pluginHealth(p: Plugin): PluginHealth {
  if (!p.enabled) return "off";
  if (p.status?.error) return "error";
  if (p.status && "running" in p.status && !p.status.running) return "starting";
  if (p.status && "active" in p.status && !p.status.active) return "ready";
  return "on";
}

export const HEALTH_TEXT: Record<PluginHealth, string> = {
  off: "Выключен",
  starting: "Запускается…",
  ready: "Готов к запуску",
  on: "Работает",
  error: "Ошибка",
};

export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}
