// Живой аватар на рабочем столе: сфера Jarvis во всё маленькое прозрачное окно и тонкое кольцо состояния
// голоса (слушаю — ярче, думаю — бегущая дуга, говорю — пульсирует с речью). Перетаскивается мышью (окно
// двигает главный процесс), щелчок открывает Jarvis, правый щелчок — меню; под указателем — кнопки плеера.
// Микрофон выключен — сфера серее и с красным значком (щелчок по значку включает микрофон).
import "@/styles/overlay.css";
import { MicOff, Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { MediaInfo } from "@/api/types";
import { Orb } from "@/components/Orb";
import { bridge } from "@/lib/bridge";
import { levels } from "@/lib/levels";
import { accentRgb } from "@/lib/theme";
import { useApp } from "@/store/app";
import { cx } from "@/ui";

const ORB_SCALE = 0.36;
const DRAG_THRESHOLD = 4;

/** Кольцо состояния голоса поверх сферы. Рисуется, только пока есть что показать. */
function StateRing() {
  const ref = useRef<HTMLCanvasElement>(null);
  const accent = useApp((s) => s.settings?.interface.accent ?? "#3B82F6");
  const animated = useApp((s) => (s.settings ? s.settings.interface.animations && s.settings.avatar.animated : true));
  const opts = useRef({ accent, animated });
  opts.current = { accent, animated };

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    let raf = 0;
    let last = performance.now();
    let alpha = 0;
    let level = 0;
    let angle = 0;
    let w = 0;
    let h = 0;
    let drawn = "";
    const stateKey = () => `${levels.state}|${levels.speaking}`;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = canvas.clientWidth || window.innerWidth;
      h = canvas.clientHeight || window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const draw = (now: number) => {
      raf = 0;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const { accent: color, animated: anim } = opts.current;
      const state = levels.state;
      const speaking = levels.speaking || state === "speaking";
      const listening = state === "listening";
      const processing = state === "processing";
      const error = state === "error";
      const target = listening ? 0.95 : processing ? 0.8 : speaking ? 0.7 : error ? 0.35 : 0;
      const lvl = listening ? levels.mic : speaking ? levels.tts : 0;
      alpha += (target - alpha) * Math.min(1, dt * (anim ? 7 : 60));
      level += (lvl - level) * Math.min(1, dt * (lvl > level ? 16 : 6));
      angle += dt * (anim ? 4.2 : 0);

      ctx.clearRect(0, 0, w, h);
      if (alpha > 0.01) {
        const rgb = error ? [248, 113, 113] : accentRgb(color);
        const cx = w / 2;
        const cy = h / 2;
        const base = Math.min(w, h) * ORB_SCALE * 1.17;
        const r = base * (1 + (speaking ? level * 0.07 : listening ? level * 0.04 : 0));
        const width = 2 + (listening || speaking ? level * 3.5 : 0);
        ctx.save();
        ctx.lineCap = "round";
        ctx.shadowColor = `rgba(${rgb.join(",")}, ${0.8 * alpha})`;
        ctx.shadowBlur = 10 + level * 14;
        if (processing && anim) {
          // бледное кольцо и бегущая по нему дуга
          ctx.strokeStyle = `rgba(${rgb.join(",")}, ${0.22 * alpha})`;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.stroke();
          const grad = ctx.createConicGradient(angle, cx, cy);
          grad.addColorStop(0, `rgba(${rgb.join(",")}, 0)`);
          grad.addColorStop(0.3, `rgba(${rgb.join(",")}, ${alpha})`);
          grad.addColorStop(0.31, `rgba(${rgb.join(",")}, 0)`);
          grad.addColorStop(1, `rgba(${rgb.join(",")}, 0)`);
          ctx.strokeStyle = grad;
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.arc(cx, cy, r, angle, angle + Math.PI * 0.62);
          ctx.stroke();
        } else {
          ctx.strokeStyle = `rgba(${rgb.join(",")}, ${(listening ? 0.85 : 0.6) * alpha})`;
          ctx.lineWidth = width;
          ctx.beginPath();
          ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.stroke();
          if (listening) {
            ctx.strokeStyle = `rgba(255, 255, 255, ${0.35 * alpha})`;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.stroke();
          }
        }
        ctx.restore();
      }
      // кадры — только пока кольцо живое (слушает/думает/говорит) или плавно появляется/гаснет
      drawn = stateKey();
      const live = anim && (listening || processing || speaking);
      if (!document.hidden && (live || Math.abs(target - alpha) > 0.01)) raf = requestAnimationFrame(draw);
    };

    const kick = () => {
      if (!raf) {
        last = performance.now();
        raf = requestAnimationFrame(draw);
      }
    };
    resize();
    const ro = new ResizeObserver(() => {
      resize();
      kick();
    });
    ro.observe(canvas);
    const offs = [onEvent("voice.state", kick), onEvent("tts.state", kick), onEvent("voice.wake", kick)];
    document.addEventListener("visibilitychange", kick);
    // страховка: состояние могло смениться без события (переподключение) — дешёвая проверка раз в 0,5 с
    const poll = window.setInterval(() => {
      if (!raf && stateKey() !== drawn) kick();
    }, 500);
    kick();
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(poll);
      ro.disconnect();
      for (const off of offs) off();
      document.removeEventListener("visibilitychange", kick);
    };
  }, []);

  return <canvas ref={ref} className="av-ring" aria-hidden />;
}

/** Микрофон выключен: красный значок на сфере — щелчок включает (сфера при этом не открывает Jarvis). */
function MutedBadge() {
  const stop = (e: React.PointerEvent) => e.stopPropagation();
  const enable = () => void useApp.getState().patchSettings({ voice: { enabled: true } }).catch(() => undefined);
  return (
    <button
      type="button"
      className="av-mute"
      title="Микрофон выключен — нажмите, чтобы включить"
      aria-label="Включить микрофон"
      onPointerDown={stop}
      onPointerUp={stop}
      onClick={enable}
    >
      <MicOff />
    </button>
  );
}

/** Плеер под сферой: назад, пауза/играть, вперёд — для того, что играет в Windows (Spotify, браузер, …). */
function MediaControls({ media }: { media: MediaInfo | null }) {
  const playing = media?.status === "playing";
  const act = (action: string) => void api.post(`/api/media/${action}`).catch(() => undefined);
  // кнопки не начинают перетаскивание и не открывают Jarvis
  const stop = (e: React.PointerEvent) => e.stopPropagation();
  const title = media?.title ? [media.title, media.artist].filter(Boolean).join(" — ") : "";
  return (
    <div className="av-media" onPointerDown={stop} onPointerUp={stop}>
      {title && <div className="av-media-title">{title}</div>}
      <div className="av-media-pill">
        <button type="button" title="Предыдущий трек" aria-label="Предыдущий трек" onClick={() => act("prev")}>
          <SkipBack />
        </button>
        <button
          type="button"
          className="main"
          title={playing ? "Пауза" : "Играть"}
          aria-label={playing ? "Пауза" : "Играть"}
          onClick={() => act("play_pause")}
        >
          {playing ? <Pause /> : <Play />}
        </button>
        <button type="button" title="Следующий трек" aria-label="Следующий трек" onClick={() => act("next")}>
          <SkipForward />
        </button>
      </div>
    </div>
  );
}

export default function AvatarApp() {
  const settings = useApp((s) => s.settings);
  const connected = useApp((s) => s.connected);
  const [fullscreen, setFullscreen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [hover, setHover] = useState(false);
  const [media, setMedia] = useState<MediaInfo | null>(null);
  const drag = useRef<{ x: number; y: number; id: number; moved: boolean; raf: number; last: [number, number] } | null>(null);

  useEffect(() => onEvent<{ fullscreen: boolean }>("fullscreen.changed", (e) => setFullscreen(!!e.data.fullscreen)), []);
  useEffect(() => onEvent<MediaInfo>("media.changed", (e) => setMedia(e.data ?? null)), []);
  // что играет сейчас — при подключении (дальше приходит media.changed)
  useEffect(() => {
    if (!connected) return;
    api
      .get<MediaInfo & { error?: string }>("/api/widgets/media")
      .then((m) => !m?.error && setMedia(m))
      .catch(() => undefined);
  }, [connected]);

  const hidden = !!settings && (!settings.avatar.desktop || (fullscreen && settings.avatar.hide_fullscreen));
  // выключенный микрофон — не то же, что ядро не отвечает (av-offline): серее и с красным значком
  const muted = connected && !!settings && !settings.voice.enabled;

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.screenX, y: e.screenY, id: e.pointerId, moved: false, raf: 0, last: [e.screenX, e.screenY] };
    bridge.send("avatar:drag-start");
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    if (!d.moved && Math.hypot(e.screenX - d.x, e.screenY - d.y) < DRAG_THRESHOLD) return;
    if (!d.moved) {
      d.moved = true;
      setDragging(true);
    }
    d.last = [e.screenX, e.screenY];
    // окно двигает главный процесс — не чаще раза за кадр
    if (!d.raf) {
      d.raf = requestAnimationFrame(() => {
        if (!drag.current) return;
        drag.current.raf = 0;
        bridge.send("avatar:drag", { screenX: drag.current.last[0], screenY: drag.current.last[1] });
      });
    }
  };

  const finish = (e: React.PointerEvent<HTMLDivElement>, cancelled: boolean) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    drag.current = null;
    cancelAnimationFrame(d.raf);
    setDragging(false);
    if (d.moved) {
      bridge.send("avatar:drag", { screenX: e.screenX, screenY: e.screenY });
      bridge.send("avatar:drag-end");
    } else if (!cancelled) {
      bridge.send("main:show");
    }
  };

  return (
    <div
      className={cx("av-root", hidden && "av-hidden", !connected && "av-offline", muted && "av-muted", dragging && "av-dragging")}
      title={dragging || hover ? undefined : muted ? "Jarvis — микрофон выключен. Нажмите, чтобы открыть" : "Jarvis — нажмите, чтобы открыть"}
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={(e) => finish(e, false)}
      onPointerCancel={(e) => finish(e, true)}
      onContextMenu={(e) => {
        e.preventDefault();
        bridge.send("avatar:menu");
      }}
    >
      {!hidden && (
        <>
          <div className="av-orb">
            <Orb scale={ORB_SCALE} />
          </div>
          <StateRing />
          {muted && !dragging && <MutedBadge />}
          {hover && !dragging && <MediaControls media={media} />}
        </>
      )}
    </div>
  );
}
