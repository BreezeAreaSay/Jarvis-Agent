// Панель «Браузер агента» (как браузер в приложении Codex): живая картинка вкладки, где работает фоновая задача,
// — по WebSocket /ws/agent-browser (docs/CONTRACTS.md §8). Смотреть можно всегда; щёлкать, листать и печатать —
// когда вкладка своя («Войти на сайты», без задачи) или управление взято у агента («Взять управление»: задача
// ждёт). Закрыли панель — управление само возвращается агенту.
import { ArrowLeft, ArrowRight, Globe, Hand, Loader2, RotateCw, Sparkles } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, useCallback, useEffect, useRef, useState, type WheelEvent } from "react";
import { coreToken, coreUrl } from "@/api/client";
import { Button, cx, IconButton } from "@/ui";

interface ViewState {
  target: string;
  task: string;
  url: string;
  title: string;
  held: boolean;
  status: string;
  error: string;
}

interface Frame {
  src: string;
  width: number;
  height: number;
}

const BUTTONS = ["left", "middle", "right"] as const;

function modifiers(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

/** task — смотреть вкладку этой задачи; без task — своя вкладка панели (войти на сайты для агента). */
export function AgentBrowserView({ task, onClose }: { task?: string; onClose?: () => void }) {
  const [state, setState] = useState<ViewState | null>(null);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [error, setError] = useState("");
  const [address, setAddress] = useState("");
  const [editing, setEditing] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const image = useRef<HTMLImageElement>(null);
  const lastMove = useRef(0);

  useEffect(() => {
    setState(null);
    setFrame(null);
    setError("");
    const url =
      coreUrl().replace(/^http/, "ws") +
      `/ws/agent-browser?token=${encodeURIComponent(coreToken())}` +
      (task ? `&task=${encodeURIComponent(task)}` : "");
    const ws = new WebSocket(url);
    socket.current = ws;
    ws.onmessage = (msg) => {
      let data: { type: string; data: any };
      try {
        data = JSON.parse(msg.data as string);
      } catch {
        return;
      }
      if (data.type === "frame") {
        const meta = data.data.metadata ?? {};
        setFrame({
          src: `data:image/jpeg;base64,${data.data.data}`,
          width: Number(meta.deviceWidth) || 1280,
          height: Number(meta.deviceHeight) || 800,
        });
      } else if (data.type === "state") {
        setState(data.data as ViewState);
        setError("");
      } else if (data.type === "error") {
        setError(String(data.data?.text ?? ""));
      }
    };
    ws.onclose = () => {
      if (socket.current === ws) socket.current = null;
    };
    return () => {
      socket.current = null;
      ws.close();
    };
  }, [task]);

  useEffect(() => {
    if (!editing) setAddress(state?.url && !/^(about|data):/.test(state.url) ? state.url : "");
  }, [state?.url, editing]);

  const send = useCallback((message: Record<string, unknown>) => {
    const ws = socket.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
  }, []);

  const manual = state?.task === "manual";
  const interactive = !!state?.target && (manual || !!state?.held);

  /** Точка на картинке → точка страницы (CSS), как в metadata кадра. */
  const point = (e: { clientX: number; clientY: number }) => {
    const rect = image.current?.getBoundingClientRect();
    if (!rect || !frame || rect.width <= 0) return { x: 0, y: 0 };
    return {
      x: ((e.clientX - rect.left) / rect.width) * frame.width,
      y: ((e.clientY - rect.top) / rect.height) * frame.height,
    };
  };

  const mouse = (kind: string) => (e: MouseEvent<HTMLImageElement>) => {
    if (!interactive) return;
    if (kind === "mouseMoved") {
      const now = performance.now();
      if (now - lastMove.current < 50) return;
      lastMove.current = now;
    } else {
      e.preventDefault();
      (e.currentTarget.parentElement as HTMLElement | null)?.focus();
    }
    send({
      type: "mouse",
      event: kind,
      ...point(e),
      button: kind === "mouseMoved" ? "none" : BUTTONS[e.button] ?? "left",
      clickCount: kind === "mouseMoved" ? 0 : Math.max(1, e.detail),
      modifiers: modifiers(e),
    });
  };

  const wheel = (e: WheelEvent<HTMLImageElement>) => {
    if (!interactive) return;
    send({ type: "wheel", ...point(e), dx: e.deltaX, dy: e.deltaY });
  };

  const key = (kind: "keyDown" | "keyUp") => (e: KeyboardEvent<HTMLDivElement>) => {
    if (!interactive || editing) return;
    if (e.key === "v" && (e.ctrlKey || e.metaKey)) return; // вставка — событием paste (текст из буфера)
    e.preventDefault();
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    const text = plain ? (e.key.length === 1 ? e.key : e.key === "Enter" ? "\r" : "") : "";
    send({
      type: "key",
      event: kind === "keyDown" && !text ? "rawKeyDown" : kind,
      key: e.key,
      code: e.code,
      keyCode: e.keyCode,
      text: kind === "keyDown" ? text : "",
      modifiers: modifiers(e),
    });
  };

  const go = () => {
    let url = address.trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
    send({ type: "navigate", url });
    setEditing(false);
  };

  let note = "";
  if (error) note = error;
  else if (!state) note = "Подключаюсь к браузеру агента…";
  else if (!state.target) note = "Задача ещё не открывала сайты — её браузер появится здесь, когда понадобится.";
  else if (state.status === "error") note = state.error || "Браузер агента не запустился";

  return (
    <div className="abv">
      <div className="abv-bar">
        <IconButton icon={<ArrowLeft />} label="Назад" size="sm" variant="ghost" disabled={!interactive} onClick={() => send({ type: "history", back: true })} />
        <IconButton icon={<ArrowRight />} label="Вперёд" size="sm" variant="ghost" disabled={!interactive} onClick={() => send({ type: "history", back: false })} />
        <IconButton icon={<RotateCw />} label="Обновить" size="sm" variant="ghost" disabled={!interactive} onClick={() => send({ type: "reload" })} />
        <div className={cx("abv-address", interactive && "on")}>
          <Globe />
          <input
            value={address}
            placeholder={interactive ? "Адрес сайта" : state?.title || "Страница агента"}
            readOnly={!interactive}
            spellCheck={false}
            onFocus={() => setEditing(true)}
            onBlur={() => setEditing(false)}
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") go();
              if (e.key === "Escape") (e.target as HTMLInputElement).blur();
            }}
          />
        </div>
        {!manual && state?.target && (
          <Button
            size="sm"
            variant={state.held ? "primary" : "soft"}
            icon={state.held ? <Sparkles /> : <Hand />}
            onClick={() => send({ type: state.held ? "release" : "take" })}
          >
            {state.held ? "Вернуть агенту" : "Взять управление"}
          </Button>
        )}
        {onClose && (
          <Button size="sm" variant="ghost" onClick={onClose}>
            Закрыть
          </Button>
        )}
      </div>
      <div
        className={cx("abv-screen", interactive && "live", frame && !note && "framed")}
        style={frame && !note ? { aspectRatio: `${frame.width} / ${frame.height}` } : undefined}
        tabIndex={0}
        onKeyDown={key("keyDown")}
        onKeyUp={key("keyUp")}
        onPaste={(e) => {
          if (!interactive) return;
          const text = e.clipboardData.getData("text");
          if (text) send({ type: "text", text });
        }}
      >
        {frame && !note ? (
          <img
            ref={image}
            src={frame.src}
            alt={state?.title || "Страница агента"}
            draggable={false}
            onMouseDown={mouse("mousePressed")}
            onMouseUp={mouse("mouseReleased")}
            onMouseMove={mouse("mouseMoved")}
            onContextMenu={(e) => e.preventDefault()}
            onWheel={wheel}
          />
        ) : (
          <div className="abv-note">
            {!state && !error ? <Loader2 className="spin" /> : <Globe />}
            <span>{note || "Жду картинку страницы…"}</span>
          </div>
        )}
      </div>
      <div className="abv-foot faint tiny">
        {manual
          ? "Это браузер агента: войдите здесь в нужные сайты — фоновые задачи будут работать под вашими аккаунтами. Пароли Jarvis не видит."
          : state?.held
            ? "Вы управляете страницей — агент ждёт. Закончите и нажмите «Вернуть агенту»."
            : "Агент работает в этой вкладке сам. Нужно вмешаться (войти, пройти капчу) — «Взять управление»."}
      </div>
    </div>
  );
}
