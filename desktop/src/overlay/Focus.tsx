// «Фокус» поверх экрана — левый верхний угол: компактная таблетка с кольцом и временем, пока идёт фокус,
// перерыв или пауза (наведите — появятся «пауза» и «закончить»), а под ней карточки: «Время перерыва»,
// «Перерыв окончен — возвращаемся?», «С возвращением» после автопаузы и «Telegram подождёт до перерыва,
// сэр». Мышь ловят только таблетка и карточки (data-interactive) — остальное окно сквозное, как у всех.
import { Coffee, Pause, Play, Square, Target, UserRound } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { onConnection } from "@/api/events";
import type { FocusAlert, FocusState } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { FocusRing, focusClock, focusLabel, focusTone, leftFrac, useFocusLeft } from "@/pages/extensions/focusKit";
import { cx } from "@/ui";
import { emitOverlay, useOverlayEvent } from "./bus";
import { DEMO, Fade, pluginAction, Slot } from "./common";

interface Card extends FocusAlert {
  id: number;
  stamp: number; // такая же карточка пришла снова — освежить текст и таймер, не перерисовывая
  leaving: boolean;
}

const MAX_CARDS = 3;
const LEAVE_MS = 360;
const TTL: Record<FocusAlert["kind"], number> = {
  distraction: 9000,
  focus_done: 45000,
  break_done: 60000,
  back: 60000,
};
let seq = 0;

function sameCard(a: FocusAlert, b: FocusAlert): boolean {
  return a.kind === b.kind && (a.kind !== "distraction" || a.key === b.key);
}

/** Карточка больше не к месту: отвлечение — только во время фокуса, «С возвращением» — пока пауза… */
function stale(c: Card, s: FocusState): boolean {
  switch (c.kind) {
    case "distraction":
      return s.phase !== "focus";
    case "focus_done":
      return s.phase === "focus" || (s.phase === "idle" && !s.waiting);
    case "break_done":
      return c.auto ? s.phase !== "focus" : !(s.phase === "idle" && s.waiting);
    case "back":
      return s.phase !== "paused";
    default:
      return false;
  }
}

function FocusPill({ state, at }: { state: FocusState; at: number }) {
  const left = useFocusLeft(state, at);
  const tone = focusTone(state);
  const paused = state.phase === "paused";
  const [busy, setBusy] = useState(false);
  const act = (action: string) => {
    setBusy(true);
    pluginAction("focus", action).finally(() => setBusy(false));
  };
  return (
    <div className={cx("fx-pill", `fxt-${tone}`)} data-interactive role="timer" aria-label={`${focusLabel(state)}: ${focusClock(left)}`}>
      <FocusRing size={26} frac={leftFrac(state, left)} tone={tone} stroke={12} />
      <span className="fx-pill-time">{focusClock(left)}</span>
      <span className="fx-pill-label">{focusLabel(state)}</span>
      <span className="fx-pill-ctl">
        <button
          type="button"
          className="fx-pill-btn"
          disabled={busy}
          onClick={() => act(paused ? "resume" : "pause")}
          aria-label={paused ? "Продолжить" : "Пауза"}
          title={paused ? "Продолжить" : "Пауза"}
        >
          {paused ? <Play /> : <Pause />}
        </button>
        <button type="button" className="fx-pill-btn stop" disabled={busy} onClick={() => act("stop")} aria-label="Закончить" title="Закончить фокус">
          <Square />
        </button>
      </span>
    </div>
  );
}

function FocusCard({ card, onClose }: { card: Card; onClose: (id: number) => void }) {
  const ttl = DEMO ? 0 : TTL[card.kind];
  const left = useRef(ttl);
  const started = useRef(0);
  const [hover, setHover] = useState(false);

  useEffect(() => {
    left.current = ttl; // пришла такая же — отсчёт заново
  }, [card.stamp, ttl]);
  useEffect(() => {
    if (!ttl || hover || card.leaving) return;
    started.current = performance.now();
    const timer = window.setTimeout(() => onClose(card.id), Math.max(400, left.current));
    return () => {
      clearTimeout(timer);
      left.current -= performance.now() - started.current;
    };
  }, [ttl, hover, card.leaving, card.id, card.stamp, onClose]);

  const act = (action: string, args: Record<string, unknown> = {}) => {
    pluginAction("focus", action, args);
    onClose(card.id);
  };
  const dismiss = () => onClose(card.id);

  let icon: ReactNode;
  let buttons: ReactNode;
  switch (card.kind) {
    case "distraction":
      icon = <AppIcon name={card.icon || card.name || "unknown"} size={40} radius={11} />;
      buttons = (
        <>
          <button type="button" className="btn ov-btn-deny" onClick={() => act("allow", { key: card.key })} title="Не напоминать о нём до конца серии">
            Нужно по работе
          </button>
          <button type="button" className="btn btn-primary" onClick={dismiss}>
            Ок
          </button>
        </>
      );
      break;
    case "focus_done":
      icon = (
        <span className={cx("fx-card-ico", card.long ? "fxt-long" : "fxt-break")}>
          <Coffee />
        </span>
      );
      buttons = (
        <>
          <button type="button" className="btn ov-btn-deny" onClick={() => act("skip_break")}>
            Пропустить перерыв
          </button>
          <button type="button" className="btn btn-primary" onClick={dismiss}>
            Отдыхаю
          </button>
        </>
      );
      break;
    case "break_done":
      icon = (
        <span className="fx-card-ico fxt-focus">
          <Target />
        </span>
      );
      buttons = card.auto ? (
        <button type="button" className="btn btn-primary fx-wide" onClick={dismiss}>
          Ок
        </button>
      ) : (
        <>
          <button type="button" className="btn ov-btn-deny" onClick={dismiss}>
            Позже
          </button>
          <button type="button" className="btn btn-primary" onClick={() => act("resume")}>
            Начать фокус
          </button>
        </>
      );
      break;
    default:
      icon = (
        <span className="fx-card-ico fxt-paused">
          <UserRound />
        </span>
      );
      buttons = (
        <>
          <button type="button" className="btn ov-btn-deny" onClick={() => act("stop")}>
            Закончить
          </button>
          <button type="button" className="btn btn-primary" onClick={() => act("resume")}>
            Продолжить
          </button>
        </>
      );
  }

  return (
    <div
      className={cx("ov-card fx-card ov-anim", `is-${card.kind}`)}
      data-interactive
      role="status"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <div className="fx-card-row">
        {icon}
        <div className="fx-card-text">
          <div className="fx-card-title">{card.title}</div>
          {card.text && <div className="fx-card-msg">{card.text}</div>}
        </div>
      </div>
      <div className={cx("ov-actions", card.kind === "break_done" && card.auto && "single")}>{buttons}</div>
      {!!ttl && card.kind === "distraction" && (
        <span className="ov-countdown fx-card-countdown" aria-hidden key={card.stamp}>
          <i style={{ "--from": 1, animationDuration: `${ttl}ms`, animationPlayState: hover ? "paused" : "running" } as React.CSSProperties} />
        </span>
      )}
    </div>
  );
}

export function FocusOverlay({ hidden }: { hidden: boolean }) {
  const [current, setCurrent] = useState<FocusState | null>(null);
  // таблетка уходит с анимацией — пусть до конца показывает последнее «живое» состояние
  const [pill, setPill] = useState<{ state: FocusState; at: number } | null>(null);
  const [cards, setCards] = useState<Card[]>([]);

  const close = useCallback((id: number) => {
    setCards((list) => list.map((c) => (c.id === id ? { ...c, leaving: true } : c)));
  }, []);
  useEffect(() => {
    if (!cards.some((c) => c.leaving)) return;
    const timer = window.setTimeout(() => setCards((list) => list.filter((c) => !c.leaving)), LEAVE_MS + 40);
    return () => clearTimeout(timer);
  }, [cards]);

  const apply = useCallback((state: FocusState) => {
    setCurrent(state);
    if (state.phase !== "idle") setPill({ state, at: Date.now() });
    setCards((list) => list.map((c) => (!c.leaving && stale(c, state) ? { ...c, leaving: true } : c)));
  }, []);

  useOverlayEvent<FocusState>("plugin.focus.state", (s) => {
    if (s && typeof s.phase === "string") apply(s);
  });

  useOverlayEvent<FocusAlert>("plugin.focus.alert", (a) => {
    if (!a?.kind || !a.title) return;
    setCards((list) => {
      const same = list.find((c) => !c.leaving && sameCard(c, a));
      if (same) return list.map((c) => (c === same ? { ...c, ...a, stamp: c.stamp + 1 } : c));
      const live = list.filter((c) => !c.leaving);
      const drop = new Set(live.slice(0, Math.max(0, live.length + 1 - MAX_CARDS)).map((c) => c.id));
      const kept = list.map((c) => (drop.has(c.id) ? { ...c, leaving: true } : c));
      return [...kept, { ...a, id: ++seq, stamp: 0, leaving: false }];
    });
  });

  // состояние при запуске оверлея и после переподключения к ядру (события без окна теряются)
  useEffect(() => {
    if (DEMO) return;
    let alive = true;
    const load = () =>
      api
        .get<{ items?: { id: string; enabled: boolean }[] }>("/api/plugins")
        .then((list) => {
          if (!list?.items?.some((p) => p.id === "focus" && p.enabled)) {
            if (alive) setCurrent(null); // плагин выключен — таблетки нет
            return;
          }
          return api
            .post<{ state?: FocusState }>("/api/plugins/focus/action", { action: "state", args: {} })
            .then((r) => {
              if (alive && r?.state) apply(r.state);
            });
        })
        .catch(() => {
          if (alive) setCurrent(null);
        });
    const off = onConnection((online) => {
      if (online) load();
    });
    return () => {
      alive = false;
      off();
    };
  }, [apply]);

  const active = !!current && current.phase !== "idle";
  return (
    <div className="fx-ov">
      <Fade show={active && !hidden && !!pill}>
        {(leaving) =>
          pill && (
            <div className={cx("fx-pill-wrap ov-anim", leaving && "leaving")}>
              <FocusPill state={pill.state} at={pill.at} />
            </div>
          )
        }
      </Fade>
      {cards.map((c) => (
        <Slot key={c.id} show={!c.leaving && !hidden}>
          <FocusCard card={c} onClose={close} />
        </Slot>
      ))}
    </div>
  );
}

// --- демо-сцена «focus» (overlay/demo.tsx) ------------------------------------------------------------------
/** Идёт второй фокус, открыли Telegram — карточка «подождёт до перерыва»; через 5 с фокус кончается:
 *  таблетка становится перерывом и появляется «Время перерыва, сэр». */
export function focusDemo(at: (ms: number, fn: () => void) => void) {
  const now = () => Date.now() / 1000;
  const base: Omit<FocusState, "phase" | "left_s" | "total_s" | "ends_at" | "next"> = {
    paused_from: "",
    reason: "",
    long_break: false,
    cycle: 2,
    long_every: 4,
    waiting: false,
    today_min: 75,
    today_sessions: 3,
    streak: 4,
    focus_min: 25,
    break_min: 5,
    long_break_min: 15,
    guard: "remind",
    allowed: [],
  };
  at(150, () =>
    emitOverlay("plugin.focus.state", { ...base, phase: "focus", left_s: 1122, total_s: 1500, ends_at: now() + 1122, next: "break" }),
  );
  at(900, () =>
    emitOverlay("plugin.focus.alert", {
      kind: "distraction",
      title: "Telegram подождёт до перерыва, сэр",
      text: "До перерыва — 19 минут. Вы справитесь.",
      phase: "focus",
      left_s: 1121,
      ts: now(),
      key: "app:telegram",
      name: "Telegram",
      icon: "telegram",
      site: false,
      closed: false,
    }),
  );
  at(5200, () => {
    emitOverlay("plugin.focus.alert", {
      kind: "focus_done",
      title: "Время перерыва, сэр",
      text: "Вы сосредоточенно работали 25 минут. Отдохните 5 минут.",
      phase: "break",
      left_s: 300,
      ts: now(),
      minutes: 25,
      break_min: 5,
      long: false,
    });
    emitOverlay("plugin.focus.state", {
      ...base,
      phase: "break",
      left_s: 300,
      total_s: 300,
      ends_at: now() + 300,
      next: "focus",
      today_min: 100,
      today_sessions: 4,
    });
  });
}
