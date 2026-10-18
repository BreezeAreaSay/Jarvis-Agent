// Уведомления под строкой голоса: «[Spotify] Включить музыку — Выполнено ✓». Не больше трёх, исчезают сами
// (3,5 с, ошибки — 6 с; под указателем — ждут). Напоминания (reminder.fired) — с колокольчиком и «Ок», 10 с.
import { Check, Info, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReminderItem, ToastEvent } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { cx } from "@/ui";
import { useOverlayEvent } from "./bus";
import { DEMO, Slot } from "./common";

interface Toast {
  id: number;
  title: string;
  message: string;
  status: "ok" | "error" | "info";
  icon: string;
  reminder: boolean;
  ttl: number;
  leaving: boolean;
}

const MAX = 3;
const LEAVE_MS = 360;
let seq = 0;

export function StatusRing({ status }: { status: "ok" | "error" | "info" }) {
  return (
    <span className={cx("ov-ring", `is-${status}`)} aria-label={status === "ok" ? "Выполнено" : status === "error" ? "Ошибка" : "Сведения"}>
      {status === "ok" ? <Check strokeWidth={2.6} /> : status === "error" ? <X strokeWidth={2.6} /> : <Info strokeWidth={2.4} />}
    </span>
  );
}

function ToastCard({ toast, onClose }: { toast: Toast; onClose: (id: number) => void }) {
  const left = useRef(toast.ttl);
  const started = useRef(0);
  const timer = useRef(0);
  const [hover, setHover] = useState(false);

  useEffect(() => {
    if (hover || toast.leaving) return;
    started.current = performance.now();
    timer.current = window.setTimeout(() => onClose(toast.id), Math.max(400, left.current));
    return () => {
      clearTimeout(timer.current);
      left.current -= performance.now() - started.current;
    };
  }, [hover, toast.leaving, toast.id, onClose]);

  return (
    <div
      className={cx("ov-card ov-toast ov-anim", `is-${toast.status}`, toast.reminder && "is-reminder")}
      data-interactive
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <AppIcon name={toast.reminder ? "bell" : toast.icon || (toast.status === "error" ? "error" : "command")} size={46} radius={13} />
      <div className="ov-toast-text">
        <div className="ov-toast-title">{toast.title}</div>
        {toast.message && <div className="ov-toast-msg">{toast.message}</div>}
      </div>
      {toast.reminder ? (
        <button type="button" className="btn btn-primary btn-sm ov-toast-ok" onClick={() => onClose(toast.id)}>
          Ок
        </button>
      ) : (
        <StatusRing status={toast.status} />
      )}
      <button type="button" className="ov-toast-close" aria-label="Закрыть" onClick={() => onClose(toast.id)}>
        <X />
      </button>
    </div>
  );
}

export function Toasts({ hidden }: { hidden: boolean }) {
  const [items, setItems] = useState<Toast[]>([]);
  const recent = useRef(new Map<string, number>());

  const close = useRef((id: number) => {
    setItems((list) => list.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    window.setTimeout(() => setItems((list) => list.filter((t) => t.id !== id)), LEAVE_MS + 40);
  }).current;

  const push = (t: Omit<Toast, "id" | "leaving">) => {
    // одинаковое уведомление дважды подряд (команда + итог) — показываем один раз
    const key = `${t.title}|${t.message}|${t.status}`;
    const now = Date.now();
    if (now - (recent.current.get(key) ?? 0) < 1500) return;
    recent.current.set(key, now);
    const toast: Toast = { ...t, id: ++seq, leaving: false };
    setItems((list) => {
      const live = list.filter((x) => !x.leaving);
      const extra = live.length + 1 - MAX;
      if (extra > 0) {
        const drop = new Set(live.slice(0, extra).map((x) => x.id));
        for (const id of drop) window.setTimeout(() => close(id), 0);
      }
      return [...list, toast];
    });
  };

  useOverlayEvent<ToastEvent>("toast", (d) => {
    if (!d || (!d.title && !d.message)) return;
    const status = d.status === "error" || d.status === "info" ? d.status : "ok";
    push({
      title: d.title || (status === "error" ? "Ошибка" : "Готово"),
      message: d.message ?? "",
      status,
      icon: d.icon || "",
      reminder: false,
      ttl: DEMO ? 600000 : status === "error" ? 6000 : 3500,
    });
  });

  useOverlayEvent<{ item: ReminderItem }>("reminder.fired", (d) => {
    const text = d?.item?.text?.trim();
    push({ title: "Напоминание", message: text || "Пора!", status: "info", icon: "bell", reminder: true, ttl: DEMO ? 600000 : 10000 });
  });

  // над игрой во весь экран обычные уведомления не копим, напоминания ждут выхода из полного экрана
  useEffect(() => {
    if (hidden) setItems((list) => list.filter((t) => t.reminder && !t.leaving));
  }, [hidden]);

  return (
    <>
      {items.map((t) => (
        <Slot key={t.id} show={!t.leaving && !hidden}>
          <ToastCard toast={t} onClose={close} />
        </Slot>
      ))}
    </>
  );
}
