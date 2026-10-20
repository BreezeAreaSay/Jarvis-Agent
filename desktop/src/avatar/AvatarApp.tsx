// Живой аватар на рабочем столе: переливающаяся всеми цветами сфера Jarvis из частиц (JarvisOrb) — только пока
// Jarvis слушает, думает или говорит; в остальное время окна нет (главный процесс прячет его по avatar:active,
// чтобы прозрачный квадрат не ловил мышь). Слушает — сфера волнуется от громкости, говорит — волны от его голоса.
// Перетаскивается мышью (окно двигает главный процесс), щелчок — «стоп» (перестать слушать, замолчать), правый
// щелчок — меню; под указателем — кнопки плеера. Микрофон выключен — сфера серее и с красным значком.
import "@/styles/overlay.css";
import { MicOff, Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { MediaInfo, VoiceState } from "@/api/types";
import { JarvisOrb } from "@/components/JarvisOrb";
import { bridge } from "@/lib/bridge";
import { levels } from "@/lib/levels";
import { useApp } from "@/store/app";
import { cx } from "@/ui";

const ORB_SCALE = 0.3;
const DRAG_THRESHOLD = 4;
const ACTIVE: VoiceState[] = ["listening", "processing", "speaking"];
const LINGER_MS = 1200; // между «думаю» и ответом голосом бывает пауза — сфера не мигает
const FADE_MS = 320; // столько гаснет сфера, потом главный процесс прячет окно

/** Jarvis слушает, думает или говорит (с задержкой на исчезновение). */
function useVoiceActive(): boolean {
  const [active, setActive] = useState(false);
  useEffect(() => {
    let state: VoiceState = levels.state;
    let speaking = levels.speaking;
    let timer = 0;
    const check = () => {
      if (speaking || ACTIVE.includes(state)) {
        clearTimeout(timer);
        timer = 0;
        setActive(true);
      } else if (!timer) {
        timer = window.setTimeout(() => {
          timer = 0;
          setActive(false);
        }, LINGER_MS);
      }
    };
    const offs = [
      onEvent<{ state: VoiceState }>("voice.state", (e) => {
        state = e.data.state;
        check();
      }),
      onEvent<{ voice?: { state?: VoiceState } }>("hello", (e) => {
        state = e.data.voice?.state ?? state;
        check();
      }),
      onEvent<{ speaking: boolean }>("tts.state", (e) => {
        speaking = !!e.data.speaking;
        check();
      }),
      onEvent("voice.wake", () => {
        state = "listening";
        check();
      }),
    ];
    check();
    return () => {
      clearTimeout(timer);
      for (const off of offs) off();
    };
  }, []);
  return active;
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
  const active = useVoiceActive();
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

  const hidden =
    !settings || !active || !settings.avatar.desktop || (fullscreen && settings.avatar.hide_fullscreen);
  // окно — только пока сфера видна: показать сразу, спрятать, когда догаснет
  useEffect(() => {
    if (!hidden) {
      bridge.send("avatar:active", true);
      return;
    }
    setHover(false);
    const timer = window.setTimeout(() => bridge.send("avatar:active", false), FADE_MS);
    return () => clearTimeout(timer);
  }, [hidden]);
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
      // «стоп»: перестать слушать, замолчать, бросить ответ (как «стоп» голосом)
      void api.post("/api/stop").catch(() => undefined);
    }
  };

  return (
    <div
      className={cx("av-root", hidden && "av-hidden", !connected && "av-offline", muted && "av-muted", dragging && "av-dragging")}
      title={dragging || hover ? undefined : "Jarvis — нажмите, чтобы остановить"}
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
      <div className="av-orb">
        <JarvisOrb scale={ORB_SCALE} iridescent animated={settings?.avatar.animated ?? true} />
      </div>
      {!hidden && muted && !dragging && <MutedBadge />}
      {!hidden && hover && !dragging && <MediaControls media={media} />}
    </div>
  );
}
