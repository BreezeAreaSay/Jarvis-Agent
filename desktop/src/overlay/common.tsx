// Общие помощники оверлея: появление/исчезновение с анимацией, границы рабочей области, полноэкранный режим,
// кадровый цикл, действия плагинов, демо-режим.
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Settings } from "@/api/types";
import { bridge, isElectron } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { emitOverlay, useOverlayEvent } from "./bus";

// --- демо-режим ---------------------------------------------------------------------------------------
/** Сцена демо-режима: ?demo=1 (или #/overlay/demo/<сцена>) — для показа в браузере и снимков экрана. */
export const DEMO: string | null = (() => {
  const q = new URLSearchParams(location.search).get("demo");
  if (q !== null) return q === "" || q === "1" || q === "true" ? "all" : q;
  const parts = location.hash.replace(/^#\/?/, "").split(/[/?]/);
  if (parts[1] === "demo") return parts[2] || "all";
  return null;
})();

// --- настройки --------------------------------------------------------------------------------------------
export function useSettings(): Settings | null {
  return useApp((s) => s.settings);
}

export function useAnimations(): boolean {
  return useApp((s) => s.settings?.interface.animations ?? true);
}

/** Масштаб интерфейса (settings.interface.ui_scale): theme.ts ставит его как CSS zoom корня. */
export function useUiZoom(): number {
  return useApp((s) => (s.settings?.interface.ui_scale ?? 100) / 100);
}

// --- появление и исчезновение ---------------------------------------------------------------------------
/** Держит элемент на экране, пока играет анимация ухода. */
export function usePresence(show: boolean, exitMs = 300): { mounted: boolean; leaving: boolean } {
  const anim = useAnimations();
  const [kept, setKept] = useState(show);
  useEffect(() => {
    if (show) {
      setKept(true);
      return;
    }
    const t = window.setTimeout(() => setKept(false), anim ? exitMs : 0);
    return () => clearTimeout(t);
  }, [show, anim, exitMs]);
  return { mounted: show || kept, leaving: !show && kept };
}

/** Ячейка стопки: плавно раздвигает соседей при появлении и смыкает при уходе. */
export function Slot({ show, children, exitMs = 340 }: { show: boolean; children: ReactNode; exitMs?: number }) {
  const { mounted, leaving } = usePresence(show, exitMs);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!mounted || leaving) {
      setOpen(false);
      return;
    }
    let raf = requestAnimationFrame(() => {
      raf = requestAnimationFrame(() => setOpen(true));
    });
    return () => cancelAnimationFrame(raf);
  }, [mounted, leaving]);
  if (!mounted) return null;
  return (
    <div className={cx("ov-slot", open && "open", leaving && "leaving")}>
      <div className="ov-slot-in">
        <div className="ov-slot-pad">{children}</div>
      </div>
    </div>
  );
}

/** Простое появление/исчезновение без раздвигания соседей. */
export function Fade({ show, children, exitMs = 300 }: { show: boolean; children: (leaving: boolean) => ReactNode; exitMs?: number }) {
  const { mounted, leaving } = usePresence(show, exitMs);
  return mounted ? <>{children(leaving)}</> : null;
}

// --- рабочая область --------------------------------------------------------------------------------------
export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
  scale: number;
}

function fallbackBounds(): Bounds {
  return {
    x: isElectron ? window.screenX : 0,
    y: isElectron ? window.screenY : 0,
    width: window.innerWidth,
    height: window.innerHeight,
    scale: window.devicePixelRatio || 1,
  };
}

// Границы приходят от Electron один раз при загрузке и при смене дисплеев. Храним их на уровне модуля:
// компонент, появившийся позже (питомец включили потом), получит последние известные, а не пропустит их.
let boundsFromMain: Bounds | null = null;
const boundsListeners = new Set<(b: Bounds) => void>();
let boundsBound = false;

function bindBounds() {
  if (boundsBound) return;
  boundsBound = true;
  bridge.on("overlay:bounds", (b) => {
    const v = b as Partial<Bounds> | null;
    if (!v || typeof v.width !== "number") return;
    boundsFromMain = {
      x: Number(v.x) || 0,
      y: Number(v.y) || 0,
      width: v.width,
      height: Number(v.height) || window.innerHeight,
      scale: Number(v.scale) || window.devicePixelRatio || 1,
    };
    for (const fn of boundsListeners) fn(boundsFromMain);
  });
  window.addEventListener("resize", () => {
    if (!boundsFromMain) for (const fn of boundsListeners) fn(fallbackBounds());
  });
  bridge.send("overlay:ready"); // границы могли уйти до подписки — попросить ещё раз
}

/** Рабочая область оверлея в DIP экрана и масштаб дисплея (overlay:bounds из Electron). */
export function useBounds(): Bounds {
  bindBounds();
  const [bounds, setBounds] = useState<Bounds>(() => boundsFromMain ?? fallbackBounds());
  useEffect(() => {
    boundsListeners.add(setBounds);
    if (boundsFromMain) setBounds(boundsFromMain);
    return () => {
      boundsListeners.delete(setBounds);
    };
  }, []);
  return bounds;
}

// --- полноэкранные игры и видео -----------------------------------------------------------------------------
/** Прятать оверлей (кроме подтверждений): активное окно — игра или видео во весь экран. */
export function useFullscreenHide(): boolean {
  const [fullscreen, setFullscreen] = useState(false);
  const hideSetting = useApp((s) => s.settings?.avatar.hide_fullscreen ?? true);
  useOverlayEvent<{ fullscreen: boolean }>("fullscreen.changed", (d) => setFullscreen(!!d.fullscreen));
  return fullscreen && hideSetting;
}

// --- кадровый цикл ------------------------------------------------------------------------------------------
/** requestAnimationFrame, пока active; колбэк получает dt (с) и время (мс). */
export function useRaf(cb: (dt: number, now: number) => void, active: boolean) {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    if (!active) return;
    let raf = 0;
    let last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      ref.current(dt, now);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [active]);
}

// --- действия ----------------------------------------------------------------------------------------------
/** Уведомление самого оверлея (ошибка кнопки и т. п.) — через тот же стек тостов. */
export function overlayToast(title: string, message = "", status: "ok" | "error" | "info" = "error", icon = "error") {
  emitOverlay("toast", { title, message, status, icon, ts: Date.now() / 1000 });
}

export async function pluginAction<T = Record<string, unknown>>(
  plugin: string,
  action: string,
  args: Record<string, unknown> = {},
): Promise<T | null> {
  if (DEMO) return null;
  try {
    return await api.post<T>(`/api/plugins/${plugin}/action`, { action, args });
  } catch (e) {
    overlayToast("Не получилось", e instanceof Error ? e.message : String(e), "error", "plugin");
    return null;
  }
}

// --- форматирование ---------------------------------------------------------------------------------------
export function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${String(m).padStart(2, "0")}:${ss}`;
}

/** Хвост длинного текста по границе слова: «…и город ещё спал». */
export function tail(text: string, max: number): string {
  const t = text.trim();
  if (t.length <= max) return t;
  const cut = t.slice(t.length - max);
  const space = cut.indexOf(" ");
  return "…" + (space > 0 && space < 24 ? cut.slice(space + 1) : cut);
}
