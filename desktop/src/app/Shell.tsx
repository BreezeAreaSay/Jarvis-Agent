// Каркас главного окна: фон, боковая навигация, кнопки окна, страница по маршруту.
import { CircleUser, Gauge, House, MicOff, Minus, Puzzle, Settings, Sparkles, SquareTerminal, X } from "lucide-react";
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import { LogoMark, Orb } from "@/components/Orb";
import { bridge, isElectron } from "@/lib/bridge";
import { navigate, useRoute } from "@/lib/router";
import { useApp } from "@/store/app";
import { ConfirmBar } from "@/components/ConfirmBar";
import { CoreDown } from "@/components/CoreDown";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useUpdates } from "@/lib/updates";
import { cx, Notices } from "@/ui";

const HomePage = lazy(() => import("@/pages/home/HomePage"));
const CommandsPage = lazy(() => import("@/pages/commands/CommandsPage"));
const ExtensionsPage = lazy(() => import("@/pages/extensions/ExtensionsPage"));
const AiPage = lazy(() => import("@/pages/ai/AiPage"));
const SettingsPage = lazy(() => import("@/pages/settings/SettingsPage"));
const ProfilePage = lazy(() => import("@/pages/profile/ProfilePage"));
const SystemPage = lazy(() => import("@/pages/system/SystemPage"));
const Onboarding = lazy(() => import("@/pages/onboarding/Onboarding"));

const NAV: { id: string; label: string; icon: ReactNode }[] = [
  { id: "home", label: "Главная", icon: <House /> },
  { id: "commands", label: "Команды", icon: <SquareTerminal /> },
  { id: "extensions", label: "Расширения", icon: <Puzzle /> },
  { id: "ai", label: "Jarvis AI", icon: <Sparkles /> },
  { id: "system", label: "Системные операции", icon: <Gauge /> },
  { id: "settings", label: "Настройки", icon: <Settings /> },
];

/** locked — идёт первая настройка: разделы откроются после неё. */
function Sidebar({ page, locked }: { page: string; locked: boolean }) {
  const pendingConfirm = useApp((s) => s.confirmations.length > 0);
  const updateReady = useUpdates((s) => s.status === "available" || s.status === "downloaded");
  return (
    <aside className={cx("sidebar", locked && "locked")} inert={locked}>
      <button className="logo" onClick={() => navigate("home")} aria-label="Jarvis">
        <LogoMark size={38} />
      </button>
      {NAV.map((n) => (
        <button
          key={n.id}
          className={cx("nav-btn", page === n.id && "active")}
          onClick={() => navigate(n.id)}
          aria-label={n.label}
        >
          {n.icon}
          {n.id === "ai" && pendingConfirm && <span className="nav-dot" />}
          {n.id === "settings" && updateReady && <span className="nav-dot" title="Доступно обновление" />}
          <span className="nav-tip">{n.label}</span>
        </button>
      ))}
      <span className="spacer" />
      <button
        className={cx("nav-btn", page === "profile" && "active")}
        onClick={() => navigate("profile")}
        aria-label="Профиль"
      >
        <CircleUser />
        <span className="nav-tip">Профиль</span>
      </button>
    </aside>
  );
}

interface SummaryRec {
  seconds: number;
  at: number;
}

/** Идёт запись конспекта созвона — секунды записи (тикают сами) или null. */
function useSummaryRecording(connected: boolean): number | null {
  const [rec, setRec] = useState<SummaryRec | null>(null);
  const [, tick] = useState(0);
  const apply = (state?: string, seconds?: number) =>
    setRec(state === "recording" ? { seconds: Number(seconds) || 0, at: performance.now() } : null);
  useEffect(() => onEvent<{ state: string; seconds: number }>("plugin.summary.state", (e) => apply(e.data?.state, e.data?.seconds)), []);
  // запись могла начаться до открытия окна: состояние плагина — при подключении
  useEffect(() => {
    if (!connected) return;
    api
      .get<{ items: { id: string; status?: { state?: string; seconds?: number } }[] }>("/api/plugins")
      .then((r) => {
        const status = r.items.find((p) => p.id === "summary")?.status;
        apply(status?.state, status?.seconds);
      })
      .catch(() => undefined);
  }, [connected]);
  useEffect(() => {
    if (!rec) return;
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [rec]);
  return rec ? rec.seconds + (performance.now() - rec.at) / 1000 : null;
}

function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Полоса над заголовком страницы — за неё двигают окно; кнопки окна — отдельным блоком поверх (см. shell.css).
 * Слева в полосе — состояние: нет связи с ядром, микрофон выключен (щелчок включает), идёт запись конспекта. */
function TitleBar() {
  const connected = useApp((s) => s.connected);
  const micOff = useApp((s) => !!s.settings && !s.settings.voice.enabled);
  const patchSettings = useApp((s) => s.patchSettings);
  const recording = useSummaryRecording(connected);
  return (
    <>
      <div className="titlebar" />
      <div className="title-status">
        {!connected && (
          <span className="conn">
            <span className="status-dot busy" /> Подключаюсь к ядру…
          </span>
        )}
        {connected && micOff && (
          <button
            type="button"
            className="title-chip mic-off"
            title="Jarvis не слушает — нажмите, чтобы включить микрофон"
            onClick={() => void patchSettings({ voice: { enabled: true } }).catch(() => undefined)}
          >
            <MicOff /> Микрофон выключен
          </button>
        )}
        {connected && recording !== null && (
          <button
            type="button"
            className="title-chip rec"
            title="Идёт запись конспекта — открыть плагин «Конспект»"
            onClick={() => navigate("extensions", "plugins", "summary")}
          >
            <span className="title-rec-dot" /> Запись {fmtClock(recording)}
          </button>
        )}
      </div>
      {isElectron && (
        <div className="win-controls">
          <button className="win-btn" aria-label="Свернуть" onClick={() => bridge.windowControl("minimize")}>
            <Minus />
          </button>
          <button className="win-btn close" aria-label="Закрыть" onClick={() => bridge.windowControl("close")}>
            <X />
          </button>
        </div>
      )}
    </>
  );
}

function Splash() {
  return (
    <div className="splash">
      <div className="orb-box">
        <Orb scale={0.42} />
      </div>
      <div className="h-hero">Jarvis</div>
      <div className="muted row">
        <span className="spinner" /> Запускаю ядро…
      </div>
    </div>
  );
}

const PAGE_NAMES: Record<string, string> = {
  home: "Главная",
  commands: "Команды",
  extensions: "Расширения",
  ai: "Jarvis AI",
  system: "Системные операции",
  settings: "Настройки",
  profile: "Профиль",
};

export function Shell() {
  const route = useRoute();
  const ready = useApp((s) => s.ready);
  const onboarded = useApp((s) => s.settings?.onboarding_done ?? true);
  const page = route[0] || "home";
  let content: ReactNode;
  switch (page) {
    case "commands":
      content = <CommandsPage route={route.slice(1)} />;
      break;
    case "extensions":
      content = <ExtensionsPage route={route.slice(1)} />;
      break;
    case "ai":
      content = <AiPage route={route.slice(1)} />;
      break;
    case "settings":
      content = <SettingsPage route={route.slice(1)} />;
      break;
    case "profile":
      content = <ProfilePage />;
      break;
    case "system":
      content = <SystemPage />;
      break;
    default:
      content = <HomePage route={route.slice(1)} />;
  }
  return (
    <div className="app-root">
      <div className="app-bg" />
      <Sidebar page={page} locked={ready && !onboarded} />
      <main className="main">
        <TitleBar />
        {/* упал раздел — карточка с причиной, а боковая панель и остальной Jarvis живы */}
        <ErrorBoundary key={onboarded ? page : "onboarding"} area={onboarded ? (PAGE_NAMES[page] ?? page) : "Знакомство"}>
          <Suspense fallback={null}>{ready && (onboarded ? content : <Onboarding />)}</Suspense>
        </ErrorBoundary>
      </main>
      {!ready && <Splash />}
      <CoreDown />
      {ready && <ConfirmBar />}
      <Notices />
    </div>
  );
}
