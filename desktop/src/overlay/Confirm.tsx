// Подтверждения поверх всего: команда или плагин спрашивают разрешения («Очистить корзину? Файлов: 12»).
// Клавиатуры у оверлея нет (окно не берёт фокус) — только мышь. Полоска внизу — сколько осталось до отказа
// по тайм-ауту.
import { ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { Confirmation } from "@/api/types";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { Slot } from "./common";

/** Сколько осталось (0..1) и секунд — по created/timeout ядра. */
function remaining(c: Confirmation): { frac: number; seconds: number } {
  if (!c.timeout || c.timeout <= 0) return { frac: 1, seconds: Infinity };
  const elapsed = Date.now() / 1000 - (c.created || Date.now() / 1000);
  const seconds = Math.max(0, c.timeout - elapsed);
  return { frac: Math.max(0, Math.min(1, seconds / c.timeout)), seconds };
}

/** Тонкая полоска обратного отсчёта: CSS-анимация от текущей доли до нуля. */
export function Countdown({ c }: { c: Confirmation }) {
  const start = useMemo(() => remaining(c), [c]);
  if (!Number.isFinite(start.seconds)) return null;
  return (
    <span className="ov-countdown" aria-hidden>
      <i style={{ "--from": start.frac, animationDuration: `${start.seconds.toFixed(2)}s` } as React.CSSProperties} />
    </span>
  );
}

export function ConfirmButtons({ c }: { c: Confirmation }) {
  const resolve = useApp((s) => s.resolveConfirmation);
  const [busy, setBusy] = useState<"allow" | "deny" | null>(null);
  const act = (allow: boolean) => {
    if (busy) return;
    setBusy(allow ? "allow" : "deny");
    resolve(c.id, allow).catch(() => undefined);
  };
  return (
    <div className="ov-actions">
      <button type="button" className="btn ov-btn-deny" disabled={!!busy} onClick={() => act(false)}>
        Отклонить
      </button>
      <button type="button" className="btn btn-primary" disabled={!!busy} onClick={() => act(true)}>
        Разрешить
      </button>
    </div>
  );
}

/** Внутренний блок «щит + что именно + подробности» (и для агента, и для команд). */
export function ApprovalBox({ c, children }: { c: Confirmation; children?: React.ReactNode }) {
  return (
    <div className="ov-approval">
      <div className="ov-approval-row">
        <span className="ov-shield">
          <ShieldAlert />
        </span>
        <div className="ov-approval-text">
          <div className="ov-approval-title">{c.title}</div>
          {c.detail && <div className="ov-approval-detail">{c.detail}</div>}
        </div>
      </div>
      {children}
    </div>
  );
}

const SOURCE: Record<string, string> = {
  voice: "голосом",
  ui: "из окна Jarvis",
  text: "из окна Jarvis",
  remote: "с пульта",
  telegram: "из Telegram",
  agent: "агент",
};

function ConfirmCard({ c }: { c: Confirmation }) {
  const source = SOURCE[c.source] ?? "";
  return (
    <div className={cx("ov-card ov-confirm ov-anim")} data-interactive role="alertdialog" aria-label={c.title}>
      <div className="ov-confirm-head">
        <span className="ov-confirm-eyebrow">Подтвердите действие</span>
        {source && <span className="ov-confirm-src">{source}</span>}
      </div>
      <ApprovalBox c={c}>
        <ConfirmButtons c={c} />
      </ApprovalBox>
      <Countdown c={c} />
    </div>
  );
}

/** Подтверждения команд и плагинов (агентские — в панели агента). */
export function Confirmations() {
  const all = useApp((s) => s.confirmations);
  const items = useMemo(() => all.filter((c) => c.kind !== "agent"), [all]);
  // ушедшие карточки доигрывают анимацию: помним их ещё немного
  const [shown, setShown] = useState<Confirmation[]>(items);
  useEffect(() => {
    setShown((prev) => {
      const ids = new Set(items.map((c) => c.id));
      const gone = prev.filter((c) => !ids.has(c.id));
      return [...items, ...gone].sort((a, b) => a.created - b.created);
    });
    const t = window.setTimeout(() => setShown(items), 600);
    return () => clearTimeout(t);
  }, [items]);

  // истёкшие по времени убираем сами (ядро пришлёт confirm.resolved, но окно могло его пропустить)
  useEffect(() => {
    if (!items.length) return;
    const timer = window.setInterval(() => {
      const expired = items.filter((c) => c.timeout > 0 && remaining(c).seconds <= 0);
      if (expired.length) {
        const ids = new Set(expired.map((c) => c.id));
        useApp.setState({ confirmations: useApp.getState().confirmations.filter((c) => !ids.has(c.id)) });
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [items]);

  const live = new Set(items.map((c) => c.id));
  return (
    <>
      {shown.map((c) => (
        <Slot key={c.id} show={live.has(c.id)}>
          <ConfirmCard c={c} />
        </Slot>
      ))}
    </>
  );
}
