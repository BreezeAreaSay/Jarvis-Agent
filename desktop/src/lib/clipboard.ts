// «Умный буфер» (плагин clipboard): действия ИИ с текстом — общие для палитры в оверлее и карточки плагина
// на странице «Расширения», — и мелочи показа записей истории.
import {
  Languages,
  Lightbulb,
  type LucideIcon,
  Reply,
  Scissors,
  Smile,
  Sparkles,
  SpellCheck,
  Volume2,
} from "lucide-react";

export interface ClipboardAction {
  id: string;
  title: string;
  icon: LucideIcon;
}

/** Действия ИИ (как в ядре: jarvis/plugins/clipboard.py) и «Прочитать» — голосом, без ИИ. */
export const CLIPBOARD_ACTIONS: ClipboardAction[] = [
  { id: "translate", title: "Перевести", icon: Languages },
  { id: "fix", title: "Исправить", icon: SpellCheck },
  { id: "shorten", title: "Сократить", icon: Scissors },
  { id: "polite", title: "Вежливее", icon: Smile },
  { id: "explain", title: "Объяснить", icon: Lightbulb },
  { id: "reply", title: "Ответить", icon: Reply },
  { id: "read", title: "Прочитать", icon: Volume2 },
];

export function clipboardActionIcon(id: string): LucideIcon {
  return CLIPBOARD_ACTIONS.find((a) => a.id === id)?.icon ?? Sparkles;
}

/** Запись одной строкой: без переносов и лишних пробелов, длинная — с «…». */
export function oneLine(text: string, max = 240): string {
  const t = text.replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t;
}

function norm(s: string): string {
  return s.toLowerCase().replaceAll("ё", "е");
}

/** Подходит ли запись под поиск: все слова запроса, без регистра, «ё» = «е». */
export function matchesQuery(text: string, query: string): boolean {
  const words = norm(query).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const t = norm(text);
  return words.every((w) => t.includes(w));
}

/** «1 знак», «64 знака», «1 200 знаков». */
export function charsLabel(n: number): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  const word = a > 10 && a < 20 ? "знаков" : b === 1 ? "знак" : b > 1 && b < 5 ? "знака" : "знаков";
  return `${n.toLocaleString("ru-RU")} ${word}`;
}
