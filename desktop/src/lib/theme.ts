// Тема: акцентный цвет, прозрачность и размытие панелей, анимации (settings.interface).
import type { Settings } from "@/api/types";

export const ACCENTS = ["#3B82F6", "#6D5DF6", "#8B3CF6", "#D946EF", "#22C55E", "#F97316", "#F43F5E", "#F2F4F8"];

function hexToRgb(hex: string): [number, number, number] {
  let h = hex.replace("#", "").trim();
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const n = Number.parseInt(h.slice(0, 6), 16);
  if (Number.isNaN(n)) return [59, 130, 246];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(rgb: [number, number, number], target: number, amount: number): [number, number, number] {
  return rgb.map((c) => Math.round(c + (target - c) * amount)) as [number, number, number];
}

function toHex([r, g, b]: [number, number, number]): string {
  return "#" + [r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("");
}

export function luminance([r, g, b]: [number, number, number]): number {
  const f = (c: number) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

export function accentRgb(hex: string): [number, number, number] {
  return hexToRgb(hex);
}

export function applyTheme(ui: Settings["interface"] | undefined) {
  if (!ui) return;
  const root = document.documentElement;
  const rgb = hexToRgb(ui.accent);
  const light = luminance(rgb) > 0.6;
  root.style.setProperty("--accent", ui.accent);
  root.style.setProperty("--accent-rgb", rgb.join(", "));
  root.style.setProperty("--accent-hover", toHex(mix(rgb, light ? 0 : 255, 0.14)));
  root.style.setProperty("--accent-press", toHex(mix(rgb, 0, 0.12)));
  root.style.setProperty("--accent-soft", `rgba(${rgb.join(", ")}, 0.16)`);
  root.style.setProperty("--accent-softer", `rgba(${rgb.join(", ")}, 0.08)`);
  root.style.setProperty("--accent-text", toHex(light ? rgb : mix(rgb, 255, 0.42)));
  root.style.setProperty("--on-accent", light ? "#12141a" : "#ffffff");
  // «Прозрачность» 0..100 → непрозрачность панелей 0.92..0.30; «Размытие» 0..100 → 0..40 px
  const alpha = 0.92 - (Math.max(0, Math.min(100, ui.transparency)) / 100) * 0.62;
  root.style.setProperty("--glass-alpha", alpha.toFixed(3));
  root.style.setProperty("--glass-blur", `${Math.round((Math.max(0, Math.min(100, ui.blur)) / 100) * 40)}px`);
  root.dataset.anim = ui.animations ? "on" : "off";
  root.style.setProperty("zoom", String((ui.ui_scale || 100) / 100));
}
