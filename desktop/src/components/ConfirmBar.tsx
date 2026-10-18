// Подтверждения в главном окне: пока окно Jarvis в фокусе, запросы («Очистить корзину? Файлов: 4») появляются
// здесь, с клавиатурой (Esc — отклонить), а оверлей их прячет. В Jarvis AI свои карточки агента — там агентские
// запросы не дублируем.
import { ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { Confirmation } from "@/api/types";
import { useRoute } from "@/lib/router";
import { useApp } from "@/store/app";
import { Button } from "@/ui";
import "@/styles/confirm-bar.css";

const SOURCE: Record<string, string> = {
  voice: "голосом",
  ui: "из окна Jarvis",
  text: "из окна Jarvis",
  remote: "с пульта",
  telegram: "из Telegram",
  agent: "агент",
};

function secondsLeft(c: Confirmation): number {
  if (!c.timeout || c.timeout <= 0) return Infinity;
  return Math.max(0, c.timeout - (Date.now() / 1000 - (c.created || Date.now() / 1000)));
}

export function ConfirmBar() {
  const all = useApp((s) => s.confirmations);
  const resolve = useApp((s) => s.resolveConfirmation);
  const page = useRoute()[0] || "home";
  const items = useMemo(() => all.filter((c) => c.kind !== "agent" || page !== "ai"), [all, page]);
  const c = items[0];
  const [busy, setBusy] = useState("");

  useEffect(() => {
    if (!c) return;
    const onKey = (e: KeyboardEvent) => {
      // Esc — «Отклонить», если фокус не в другом поле или окне (у них свой Esc)
      const focus = document.activeElement;
      const free = !focus || focus === document.body || !!focus.closest(".confirm-bar");
      if (e.key === "Escape" && free && busy !== c.id) {
        e.preventDefault();
        setBusy(c.id);
        void resolve(c.id, false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [c, busy, resolve]);

  if (!c) return null;
  const act = (allow: boolean) => {
    if (busy === c.id) return;
    setBusy(c.id);
    void resolve(c.id, allow);
  };
  const left = secondsLeft(c);
  const source = SOURCE[c.source] ?? "";
  return (
    <div className="confirm-bar-wrap">
      <div className="confirm-bar pop-in" role="alertdialog" aria-label={c.title} key={c.id}>
        <div className="confirm-bar-head">
          <span className="confirm-bar-eyebrow">Подтвердите действие</span>
          {source && <span className="confirm-bar-src">{source}</span>}
        </div>
        <div className="confirm-bar-row">
          <span className="confirm-bar-shield">
            <ShieldAlert />
          </span>
          <div className="confirm-bar-text">
            <div className="confirm-bar-title">{c.title}</div>
            {c.detail && <div className="confirm-bar-detail">{c.detail}</div>}
          </div>
        </div>
        <div className="confirm-bar-actions">
          <Button disabled={busy === c.id} onClick={() => act(false)}>
            Отклонить
          </Button>
          <Button variant="primary" disabled={busy === c.id} onClick={() => act(true)}>
            Разрешить
          </Button>
        </div>
        {items.length > 1 && <div className="confirm-bar-more">Ещё запросов: {items.length - 1}</div>}
        {Number.isFinite(left) && (
          <span className="confirm-bar-countdown" aria-hidden>
            <i
              style={
                {
                  "--from": Math.min(1, left / c.timeout),
                  animationDuration: `${left.toFixed(2)}s`,
                } as React.CSSProperties
              }
            />
          </span>
        )}
      </div>
    </div>
  );
}
