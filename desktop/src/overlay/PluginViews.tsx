// Плагины поверх экрана: живые субтитры и диктовка (внизу по центру), чтение вслух (мини-плеер справа
// внизу), запись конспекта (значок справа вверху).
import { BookOpenText, Check, Mic, Pause, Play, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { levels } from "@/lib/levels";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { useOverlayEvent } from "./bus";
import { Fade, fmtClock, pluginAction, tail, useRaf } from "./common";

// --- живые субтитры -----------------------------------------------------------------------------------
interface SubtitleItem {
  id: number;
  text: string;
  translation: string;
  pending?: boolean;
}

/** Над полноэкранным видео тоже: субтитры для того и запускают (остальное в полноэкранном режиме прячется). */
export function Subtitles() {
  const [item, setItem] = useState<SubtitleItem | null>(null);
  const [show, setShow] = useState(false);
  const timer = useRef(0);

  useOverlayEvent<SubtitleItem>("plugin.subtitles.text", (d) => {
    if (!d?.text?.trim()) return;
    setItem((prev) => (prev && d.id && d.id < prev.id ? prev : d));
    setShow(true);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setShow(false), 6000);
  });
  useOverlayEvent<{ active: boolean }>("plugin.subtitles.state", (d) => {
    if (d && !d.active) {
      clearTimeout(timer.current);
      setShow(false);
    }
  });
  useEffect(() => () => clearTimeout(timer.current), []);

  const translation = item?.translation?.trim() ?? "";
  const original = item?.text?.trim() ?? "";
  const two = !!translation && translation.toLowerCase() !== original.toLowerCase();
  return (
    <Fade show={show && !!item}>
      {(leaving) => (
        <div className={cx("ov-subs ov-anim-up", leaving && "leaving")} aria-live="polite">
          {two && <div className="ov-subs-orig">{tail(original, 140)}</div>}
          <div className="ov-subs-main">
            {tail(two ? translation : original, 120)}
            {item?.pending && <span className="ov-subs-pending" aria-label="Перевожу" />}
          </div>
        </div>
      )}
    </Fade>
  );
}

// --- полоски громкости микрофона ----------------------------------------------------------------------------
function LevelBars({ count = 7, active = true }: { count?: number; active?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const level = useRef(0);
  useRaf((dt, now) => {
    const el = ref.current;
    if (!el) return;
    level.current += (levels.mic - level.current) * Math.min(1, dt * (levels.mic > level.current ? 16 : 5));
    const t = now / 1000;
    const bars = el.children;
    for (let i = 0; i < bars.length; i++) {
      const wave = 0.5 + 0.5 * Math.sin(t * 10 + i * 1.7);
      const h = 0.22 + Math.min(0.78, level.current * 1.6 * (0.45 + 0.55 * wave));
      (bars[i] as HTMLElement).style.transform = `scaleY(${h.toFixed(3)})`;
    }
  }, active);
  return (
    <span className="ov-bars" ref={ref} aria-hidden>
      {Array.from({ length: count }, (_, i) => (
        <i key={i} />
      ))}
    </span>
  );
}

// --- умная диктовка -----------------------------------------------------------------------------------------
export function DictationPill({ hidden }: { hidden: boolean }) {
  const [state, setState] = useState<"idle" | "listening" | "processing">("idle");
  const [flash, setFlash] = useState("");
  const last = useRef("");
  const flashTimer = useRef(0);

  const showFlash = (text: string) => {
    setFlash(text);
    clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(""), 2600);
  };

  useOverlayEvent<{ state: string; text?: string }>("plugin.dictation.state", (d) => {
    const s = d?.state === "listening" || d?.state === "processing" ? d.state : "idle";
    const text = (d?.text ?? "").trim();
    if (s === "processing" && text) {
      last.current = text;
      showFlash(text);
    } else if (s === "listening" && text && text !== last.current) {
      last.current = text; // вставили после правки ИИ — показываем окончательный текст
      showFlash(text);
    }
    if (s === "idle") last.current = "";
    setState(s);
  });
  useEffect(() => () => clearTimeout(flashTimer.current), []);

  const visible = (state !== "idle" || !!flash) && !hidden;
  return (
    <Fade show={visible}>
      {(leaving) => (
        <div className={cx("ov-dict ov-anim-up", leaving && "leaving", flash && "has-flash")}>
          <span className="ov-dict-main">
            {state === "processing" ? <span className="spinner ov-dict-spin" /> : <Mic className="ov-dict-ico" />}
            <span className="ov-dict-label">{state === "processing" ? "Вставляю…" : state === "listening" ? "Слушаю…" : "Готово"}</span>
            {state === "listening" && <LevelBars active={!leaving} />}
          </span>
          {flash && (
            <span className="ov-dict-flash" key={flash}>
              <Check />
              <span className="ellipsis">«{flash}»</span>
            </span>
          )}
        </div>
      )}
    </Fade>
  );
}

// --- чтение вслух ---------------------------------------------------------------------------------------------
interface ReaderProgress {
  state: "playing" | "paused" | "stopped" | "done" | "idle";
  index: number;
  total: number;
  sentence: string;
  title?: string;
  sentences?: string[];
}

const SPEEDS = [1, 1.25, 1.5, 1.75, 2, 0.75];

function fmtSpeed(v: number): string {
  const r = Math.round(v * 100) / 100;
  const s = Number.isInteger(r * 10) ? r.toFixed(1) : r.toFixed(2);
  return s.replace(".", ",") + "×";
}

/** Псевдослучайная «волна» для дорожки: одинаковая для одного текста. */
function waveHeights(n: number, seed: number): number[] {
  let s = (seed * 9301 + 49297) % 233280 || 1;
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    s = (s * 9301 + 49297) % 233280;
    const r = s / 233280;
    out.push(0.32 + 0.68 * (0.5 * r + 0.5 * Math.abs(Math.sin(i * 0.9 + seed))));
  }
  return out;
}

export function ReaderPlayer({ hidden, bottom }: { hidden: boolean; bottom: number }) {
  const [p, setP] = useState<ReaderProgress | null>(null);
  const [show, setShow] = useState(false);
  const sentences = useRef<string[]>([]);
  const hideTimer = useRef(0);
  const configSpeed = useApp((s) => Number(s.settings?.plugins?.reader?.config?.speed) || 1);
  const [speed, setSpeed] = useState<number | null>(null);
  const shownSpeed = speed ?? configSpeed;

  useEffect(() => setSpeed(null), [configSpeed]);

  useOverlayEvent<ReaderProgress>("plugin.reader.progress", (d) => {
    if (!d) return;
    if (Array.isArray(d.sentences) && d.sentences.length) sentences.current = d.sentences.map(String);
    else if (d.sentence && d.index >= 0) sentences.current[d.index] = d.sentence;
    setP(d);
    clearTimeout(hideTimer.current);
    if (d.state === "stopped" || d.state === "done" || d.state === "idle") {
      hideTimer.current = window.setTimeout(() => setShow(false), d.state === "done" ? 2200 : 900);
    } else {
      setShow(true);
    }
  });
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  const seed = p ? (p.total * 31 + (p.title?.length ?? 7)) % 97 : 1;
  const heights = useMemo(() => waveHeights(34, seed), [seed]);

  const playing = p?.state === "playing";
  const total = Math.max(1, p?.total ?? 1);
  const index = Math.max(0, Math.min(total - 1, p?.index ?? 0));
  const progress = p?.state === "done" ? 1 : (index + (playing ? 0.5 : 0)) / total;
  const prev = index > 0 ? sentences.current[index - 1] : "";
  const next = index + 1 < total ? sentences.current[index + 1] : "";

  const act = (action: string, args: Record<string, unknown> = {}) => pluginAction("reader", action, args);
  const toggle = () => {
    if (!p) return;
    const nextState = playing ? "paused" : "playing";
    setP({ ...p, state: nextState });
    act(playing ? "pause" : "resume");
  };
  const seek = (i: number) => {
    if (!p || i < 0 || i >= total) return;
    setP({ ...p, index: i, sentence: sentences.current[i] ?? p.sentence });
    act("seek", { index: i });
  };
  const cycleSpeed = () => {
    const cur = SPEEDS.findIndex((v) => Math.abs(v - shownSpeed) < 0.01);
    const value = SPEEDS[(cur + 1) % SPEEDS.length] ?? 1;
    setSpeed(value);
    act("speed", { speed: value, value });
  };

  return (
    <Fade show={show && !hidden && !!p}>
      {(leaving) => (
        <div className={cx("ov-card ov-reader ov-anim-up", leaving && "leaving")} style={{ bottom }} data-interactive>
          <div className="ov-reader-head">
            <span className="ov-reader-ico">
              <BookOpenText />
            </span>
            <div className="grow">
              <div className="ov-reader-title ellipsis">{p?.title || "Чтение вслух"}</div>
              <div className="ov-reader-sub">
                {p?.state === "paused" ? "Пауза · " : p?.state === "done" ? "Дочитано · " : ""}
                {index + 1} из {total}
              </div>
            </div>
            <button type="button" className="ov-icon-btn" aria-label="Остановить чтение" title="Остановить" onClick={() => act("stop")}>
              <Square />
            </button>
          </div>
          <div className="ov-reader-text">
            {prev && (
              <button type="button" className="ov-reader-line dim" onClick={() => seek(index - 1)} title="Вернуться к этой фразе">
                {prev}
              </button>
            )}
            <div className="ov-reader-line cur" key={index}>
              {p?.sentence}
            </div>
            {next && (
              <button type="button" className="ov-reader-line dim" onClick={() => seek(index + 1)} title="Перейти к этой фразе">
                {next}
              </button>
            )}
          </div>
          <div className="ov-reader-bar">
            <button type="button" className="ov-play" onClick={toggle} aria-label={playing ? "Пауза" : "Продолжить"}>
              {playing ? <Pause /> : <Play />}
            </button>
            <div className={cx("ov-wave", playing && "is-playing")}>
              {heights.map((h, i) => {
                const frac = (i + 0.5) / heights.length;
                return (
                  <button
                    type="button"
                    key={i}
                    className={cx("ov-wave-bar", frac <= progress && "on", Math.abs(frac - progress) < 0.5 / heights.length + 0.001 && "cur")}
                    style={{ "--h": h } as React.CSSProperties}
                    onClick={() => seek(Math.min(total - 1, Math.floor(frac * total)))}
                    aria-label={`Фраза ${Math.min(total, Math.floor(frac * total) + 1)}`}
                  />
                );
              })}
            </div>
            <button type="button" className="ov-speed" onClick={cycleSpeed} title="Скорость чтения">
              {fmtSpeed(shownSpeed)}
            </button>
          </div>
        </div>
      )}
    </Fade>
  );
}

// --- конспект: запись идёт --------------------------------------------------------------------------------
interface SummaryState {
  state: "recording" | "transcribing" | "summarizing" | "done" | "error" | "idle";
  seconds: number;
  progress?: number;
}

export function SummaryBadge({ hidden }: { hidden: boolean }) {
  const [s, setS] = useState<SummaryState | null>(null);
  const [show, setShow] = useState(false);
  const base = useRef({ seconds: 0, at: 0 });
  const [, tick] = useState(0);
  const hideTimer = useRef(0);

  useOverlayEvent<SummaryState>("plugin.summary.state", (d) => {
    if (!d?.state) return;
    setS(d);
    base.current = { seconds: Number(d.seconds) || 0, at: performance.now() };
    clearTimeout(hideTimer.current);
    if (d.state === "idle") setShow(false);
    else if (d.state === "done" || d.state === "error") {
      setShow(true);
      hideTimer.current = window.setTimeout(() => setShow(false), d.state === "done" ? 2500 : 4000);
    } else setShow(true);
  });

  const recording = s?.state === "recording";
  useEffect(() => {
    if (!recording || !show) return;
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [recording, show]);
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  const seconds = recording ? base.current.seconds + (performance.now() - base.current.at) / 1000 : base.current.seconds;
  let label: React.ReactNode;
  switch (s?.state) {
    case "recording":
      label = (
        <>
          Конспект <b className="ov-sum-time">{fmtClock(seconds)}</b>
        </>
      );
      break;
    case "transcribing":
      label = `Расшифровка${s.progress ? ` ${Math.round(s.progress * 100)}%` : "…"}`;
      break;
    case "summarizing":
      label = "Пишу конспект…";
      break;
    case "done":
      label = "Конспект готов";
      break;
    case "error":
      label = "Конспект не получился";
      break;
    default:
      label = "Конспект";
  }

  return (
    <Fade show={show && !hidden && !!s}>
      {(leaving) => (
        <div className={cx("ov-summary ov-anim", leaving && "leaving", `is-${s?.state}`)} data-interactive>
          {s?.state === "recording" ? (
            <span className="ov-rec-dot" />
          ) : s?.state === "done" ? (
            <Check className="ov-sum-ok" />
          ) : s?.state === "error" ? (
            <span className="ov-rec-dot err" />
          ) : (
            <span className="spinner ov-sum-spin" />
          )}
          <span className="ov-sum-label">{label}</span>
          {recording && (
            <button type="button" className="ov-sum-stop" onClick={() => pluginAction("summary", "stop")} title="Остановить запись и сделать конспект">
              <Square />
            </button>
          )}
        </div>
      )}
    </Fade>
  );
}
