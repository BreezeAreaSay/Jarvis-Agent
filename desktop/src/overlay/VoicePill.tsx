// Строка голоса (вверху по центру): кружок микрофона пульсирует от громкости, «Джарвис» — подсвеченный
// чип, дальше живой текст фразы (voice.partial → voice.final), справа точки-волна. После выполнения команды
// строка сворачивается в кружок и исчезает (~1,5 с); без текста в покое — через 4 с.
import { Mic } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { VoiceState } from "@/api/types";
import { levels } from "@/lib/levels";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { useOverlayEvent } from "./bus";
import { Slot, useRaf } from "./common";

type Phase = "listening" | "processing" | "done" | "miss";

interface Pill {
  show: boolean;
  wake: boolean;
  text: string;
  phase: Phase;
}

const EMPTY: Pill = { show: false, wake: false, text: "", phase: "listening" };
const DONE_HIDE_MS = 1500;
const IDLE_HIDE_MS = 4000;
const STALE_HIDE_MS = 15000;

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Убрать слово-активатор из текста: «Джарвис, включи музыку» → «включи музыку». */
export function stripWake(text: string, words: string[]): { text: string; found: boolean } {
  const list = [...new Set(words.map((w) => w.trim().toLowerCase()).filter(Boolean))].map(escapeRe);
  if (!list.length || !text) return { text, found: false };
  const re = new RegExp(`(^|[\\s,.!?…«"“—–-]+)(?:${list.join("|")})\\p{L}{0,2}(?=$|[\\s,.!?…»"”—–-])[\\s,.!?…—–-]*`, "iu");
  const m = re.exec(text);
  if (!m) return { text, found: false };
  const before = text.slice(0, m.index).trim();
  const after = text.slice(m.index + m[0].length).trim();
  return { text: [before, after].filter(Boolean).join(" "), found: true };
}

const PLACEHOLDER: Record<Phase, string> = {
  listening: "Слушаю…",
  processing: "Распознаю…",
  done: "Готово",
  miss: "Не расслышал",
};

export function VoicePill({ hidden }: { hidden: boolean }) {
  const assistant = useApp((s) => s.settings?.profile.assistant_name?.trim() || "Джарвис");
  const wakeWords = useApp((s) => s.settings?.voice.wake_words);
  const words = useMemo(() => [...(wakeWords ?? ["джарвис"]), assistant, "jarvis"], [wakeWords, assistant]);
  const [pill, setPill] = useState<Pill>(EMPTY);
  const cur = useRef<Pill>(EMPTY);
  const timers = useRef({ hide: 0, stale: 0 });

  const hide = () => {
    clearTimeout(timers.current.hide);
    clearTimeout(timers.current.stale);
    cur.current = { ...cur.current, show: false };
    setPill(cur.current);
  };

  /** Показать/обновить строку. fresh — начать новый «сеанс» (сбросить текст и чип). */
  const update = (patch: Partial<Pill>, opts: { fresh?: boolean; hideIn?: number } = {}) => {
    clearTimeout(timers.current.hide);
    clearTimeout(timers.current.stale);
    const c = cur.current;
    const fresh = opts.fresh || !c.show || c.phase === "done" || c.phase === "miss";
    cur.current = { ...(fresh ? EMPTY : c), ...patch, show: true };
    setPill(cur.current);
    if (opts.hideIn) timers.current.hide = window.setTimeout(hide, opts.hideIn);
    timers.current.stale = window.setTimeout(hide, STALE_HIDE_MS);
  };

  const sessionOpen = () => cur.current.show && cur.current.phase !== "done" && cur.current.phase !== "miss";

  useEffect(() => {
    const t = timers.current;
    return () => {
      clearTimeout(t.hide);
      clearTimeout(t.stale);
    };
  }, []);

  useOverlayEvent("voice.wake", () => {
    update(sessionOpen() ? { wake: true } : { wake: true, phase: "listening" });
  });

  useOverlayEvent<{ text: string; wake: boolean }>("voice.partial", (d) => {
    const s = stripWake(d.text || "", words);
    const keepWake = sessionOpen() && cur.current.wake;
    update({ text: s.text, wake: keepWake || !!d.wake || s.found, phase: "listening" });
  });

  useOverlayEvent<{ text: string; wake: boolean; command: string }>("voice.final", (d) => {
    const raw = (d.text || "").trim();
    if (!raw) {
      if (cur.current.show) update({ phase: "miss", text: "" }, { hideIn: 1600 });
      return;
    }
    const s = stripWake(raw, words);
    const wake = (sessionOpen() && cur.current.wake) || !!d.wake || s.found;
    const command = (d.command || "").trim();
    if (!command) {
      // одно «Джарвис» — ждём команду следующей фразой
      update({ wake, text: "", phase: "listening" });
      return;
    }
    update({ wake, text: wake ? command : raw, phase: "processing" });
  });

  useOverlayEvent<{ text: string; source: string }>("dispatch.started", (d) => {
    if (d.source !== "voice" || !cur.current.show) return;
    update({ phase: "processing", text: cur.current.text || d.text });
  });

  const finish = () => {
    if (cur.current.show && cur.current.phase === "processing") update({ phase: "done" }, { hideIn: DONE_HIDE_MS });
  };
  useOverlayEvent("dispatch.done", finish);
  useOverlayEvent("command.executed", finish);
  useOverlayEvent("toast", finish);

  useOverlayEvent<{ state: VoiceState }>("voice.state", (d) => {
    const c = cur.current;
    if (d.state === "listening") {
      if (!sessionOpen()) update({ phase: "listening" }, { fresh: true });
    } else if (d.state === "processing") {
      if (!c.show || c.phase === "listening") update({ phase: "processing" });
    } else if (c.show && !c.text && c.phase !== "done" && c.phase !== "miss") {
      update({}, { hideIn: IDLE_HIDE_MS });
    }
  });

  // --- живая часть: кружок микрофона и точки-волна (без перерисовки React) ---------------------------------
  const micRef = useRef<HTMLSpanElement>(null);
  const dotsRef = useRef<HTMLCanvasElement>(null);
  const level = useRef(0);
  const visible = pill.show && !hidden;

  useRaf((dt, now) => {
    const phase = cur.current.phase;
    const target = phase === "listening" ? levels.mic : 0;
    level.current += (target - level.current) * Math.min(1, dt * (target > level.current ? 16 : 5));
    const l = level.current;
    const mic = micRef.current;
    if (mic) {
      mic.style.transform = `scale(${(1 + l * 0.16).toFixed(3)})`;
      mic.style.setProperty("--ring", `${(3 + l * 12).toFixed(1)}px`);
      mic.style.setProperty("--ring-a", (0.14 + l * 0.3).toFixed(3));
    }
    const canvas = dotsRef.current;
    if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = 72;
    const h = 28;
    if (canvas.width !== Math.round(w * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const n = 10;
    const t = now / 1000;
    for (let i = 0; i < n; i++) {
      const x = 4 + i * 7.1;
      let height = 3.2;
      let alpha = 0.55;
      if (phase === "listening") {
        const env = Math.pow(Math.sin((Math.PI * (i + 0.5)) / n), 0.7);
        const wave = 0.55 + 0.45 * Math.sin(t * 9 + i * 1.25);
        height = 3.2 + l * 20 * env * wave;
        alpha = 0.5 + Math.min(0.5, l * 1.2);
      } else if (phase === "processing") {
        const k = Math.max(0, Math.sin(t * 6.5 - i * 0.55));
        alpha = 0.28 + 0.72 * k * k;
        height = 3.2 + 2.2 * k * k;
      } else {
        alpha = 0.32;
      }
      ctx.fillStyle = `rgba(255, 255, 255, ${alpha.toFixed(3)})`;
      ctx.beginPath();
      ctx.roundRect(x - 1.6, h / 2 - height / 2, 3.2, height, 1.6);
      ctx.fill();
    }
  }, visible);

  // длинная фраза: показываем конец, начало уходит под затухание слева. Элементы пересоздаются (строка
  // пряталась над полноэкранной игрой) — поэтому наблюдатель вешается через ref-колбэк на каждый новый узел.
  const [overflow, setOverflow] = useState(0);
  const nodes = useRef<{ body: HTMLDivElement | null; line: HTMLSpanElement | null; ro: ResizeObserver | null }>({
    body: null,
    line: null,
    ro: null,
  });
  const measure = useCallback(() => {
    const { body, line } = nodes.current;
    setOverflow(body && line ? Math.max(0, Math.ceil(line.scrollWidth - body.clientWidth)) : 0);
  }, []);
  const observe = useCallback(() => {
    const n = nodes.current;
    n.ro?.disconnect();
    n.ro = null;
    if (!n.body || !n.line) return;
    n.ro = new ResizeObserver(measure);
    n.ro.observe(n.body);
    n.ro.observe(n.line);
    measure();
  }, [measure]);
  const bodyRef = useCallback(
    (el: HTMLDivElement | null) => {
      nodes.current.body = el;
      observe();
    },
    [observe],
  );
  const lineRef = useCallback(
    (el: HTMLSpanElement | null) => {
      nodes.current.line = el;
      observe();
    },
    [observe],
  );
  useLayoutEffect(measure, [pill.text, pill.wake, pill.phase, measure]);
  useEffect(() => () => nodes.current.ro?.disconnect(), []);

  const placeholder = !pill.text;
  return (
    <Slot show={visible} exitMs={460}>
      <div className={cx("ov-pill", `is-${pill.phase}`)} role="status" aria-live="polite">
        <span className="ov-pill-mic" ref={micRef}>
          <Mic />
        </span>
        {pill.wake && <span className="ov-wake">{assistant}</span>}
        <div className={cx("ov-pill-body", overflow > 0 && "clipped")} ref={bodyRef}>
          <span
            className={cx("ov-pill-line", placeholder ? "ov-pill-ph" : "ov-pill-text")}
            ref={lineRef}
            style={{ transform: overflow ? `translateX(${-overflow}px)` : undefined }}
          >
            {placeholder ? PLACEHOLDER[pill.phase] : pill.text}
          </span>
        </div>
        <canvas className="ov-pill-dots" ref={dotsRef} aria-hidden />
      </div>
    </Slot>
  );
}
