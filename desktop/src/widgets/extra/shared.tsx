// Общее для дополнительных виджетов: кнопка «Настроить» в шапке и окно настроек, размер тела виджета,
// локальное состояние (таймер, секундомер), время «10:42 / вчера / пн / 12 окт», звонок и уведомление.
import { Settings2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Modal } from "@/ui";

/** Маленькая кнопка в шапке виджета (видна при наведении, если hover). */
export function HeadButton({
  icon,
  label,
  onClick,
  hover,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  hover?: boolean;
}) {
  return (
    <button className={`wx-head-btn${hover ? " wx-hover" : ""}`} onClick={onClick} aria-label={label} title={label}>
      {icon}
    </button>
  );
}

export function CfgButton({ onClick }: { onClick: () => void }) {
  return <HeadButton icon={<Settings2 />} label="Настроить виджет" onClick={onClick} hover />;
}

/** Окно настроек виджета с «Отмена / Сохранить». */
export function WidgetSettingsModal({
  open,
  title,
  onClose,
  onSave,
  children,
  width = 460,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  onSave: () => void;
  children: ReactNode;
  width?: number;
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      width={width}
      footer={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button
            variant="primary"
            onClick={() => {
              onSave();
              onClose();
            }}
          >
            Сохранить
          </Button>
        </>
      }
    >
      {children}
    </Modal>
  );
}

/** Размер элемента (ResizeObserver). */
export function useBox<T extends HTMLElement>(): [React.RefObject<T | null>, { width: number; height: number }] {
  const ref = useRef<T>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setBox((b) => (Math.abs(b.width - r.width) < 0.5 && Math.abs(b.height - r.height) < 0.5 ? b : { width: r.width, height: r.height }));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, box];
}

/** Состояние в localStorage (только удобство одного окна; без хранилища — просто в памяти). */
export function useStored<T>(key: string, initial: T): [T, (v: T | ((p: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return initial;
      const parsed = JSON.parse(raw);
      return initial && typeof initial === "object" && !Array.isArray(initial) ? ({ ...initial, ...parsed } as T) : (parsed as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T | ((p: T) => T)) => {
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
        try {
          localStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* хранилище недоступно — живём в памяти */
        }
        return next;
      });
    },
    [key],
  );
  return [value, set];
}

/** Тик раз в ms, пока active. */
export function useTick(ms: number, active = true): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setN((x) => x + 1), ms);
    return () => clearInterval(t);
  }, [ms, active]);
  return n;
}

/** «10:42» сегодня, «вчера», «пн» на этой неделе, иначе «12 окт». */
export function shortWhen(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "вчера";
  const days = (now.getTime() - d.getTime()) / 86400000;
  if (days > 0 && days < 6.5) return d.toLocaleDateString("ru-RU", { weekday: "short" });
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }).replace(".", "");
}

export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

export function toList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map((x) => String(x).trim()).filter(Boolean);
  if (typeof v === "string") return v.split(/[,;]/).map((x) => x.trim()).filter(Boolean);
  return [];
}

/** Короткий звонок (WebAudio) — конец таймера. */
export function chime() {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const t0 = ctx.currentTime + 0.02;
    [880, 1175, 1568, 1175, 1568].forEach((f, i) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      const t = t0 + i * 0.16;
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.22, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.45);
      o.connect(g).connect(ctx.destination);
      o.start(t);
      o.stop(t + 0.5);
    });
    setTimeout(() => ctx.close().catch(() => undefined), 1600);
  } catch {
    /* звука нет — не страшно */
  }
}

/** Системное уведомление (если разрешено). */
export function desktopNotify(title: string, body: string) {
  try {
    if (!("Notification" in window)) return;
    if (Notification.permission === "granted") new Notification(title, { body, silent: true });
    else if (Notification.permission === "default")
      Notification.requestPermission()
        .then((p) => p === "granted" && new Notification(title, { body, silent: true }))
        .catch(() => undefined);
  } catch {
    /* уведомления недоступны */
  }
}

/** Попросить разрешение на уведомления заранее (по нажатию «Старт»). */
export function askNotifyPermission() {
  try {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => undefined);
  } catch {
    /* нет уведомлений */
  }
}

export function clock(totalSec: number): string {
  const s = Math.max(0, Math.ceil(totalSec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
