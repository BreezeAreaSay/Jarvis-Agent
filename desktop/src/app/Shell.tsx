// Каркас главного окна: фон, боковая навигация, кнопки окна, страница по маршруту.
import { CircleUser, House, Minus, Puzzle, Settings, Sparkles, SquareTerminal, X } from "lucide-react";
import { lazy, Suspense, type ReactNode } from "react";
import { LogoMark, Orb } from "@/components/Orb";
import { bridge, isElectron } from "@/lib/bridge";
import { navigate, useRoute } from "@/lib/router";
import { useApp } from "@/store/app";
import { CoreDown } from "@/components/CoreDown";
import { useUpdates } from "@/lib/updates";
import { cx, Notices } from "@/ui";

const HomePage = lazy(() => import("@/pages/home/HomePage"));
const CommandsPage = lazy(() => import("@/pages/commands/CommandsPage"));
const ExtensionsPage = lazy(() => import("@/pages/extensions/ExtensionsPage"));
const AiPage = lazy(() => import("@/pages/ai/AiPage"));
const SettingsPage = lazy(() => import("@/pages/settings/SettingsPage"));
const ProfilePage = lazy(() => import("@/pages/profile/ProfilePage"));
const Onboarding = lazy(() => import("@/pages/onboarding/Onboarding"));

const NAV: { id: string; label: string; icon: ReactNode }[] = [
  { id: "home", label: "Главная", icon: <House /> },
  { id: "commands", label: "Команды", icon: <SquareTerminal /> },
  { id: "extensions", label: "Расширения", icon: <Puzzle /> },
  { id: "ai", label: "Jarvis AI", icon: <Sparkles /> },
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

function TitleBar() {
  const connected = useApp((s) => s.connected);
  return (
    <div className="titlebar">
      {!connected && (
        <span className="conn">
          <span className="status-dot busy" /> Подключаюсь к ядру…
        </span>
      )}
      {isElectron && (
        <>
          <button className="win-btn" aria-label="Свернуть" onClick={() => bridge.windowControl("minimize")}>
            <Minus />
          </button>
          <button className="win-btn close" aria-label="Закрыть" onClick={() => bridge.windowControl("close")}>
            <X />
          </button>
        </>
      )}
    </div>
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
    default:
      content = <HomePage route={route.slice(1)} />;
  }
  return (
    <div className="app-root">
      <div className="app-bg" />
      <Sidebar page={page} locked={ready && !onboarded} />
      <main className="main">
        <TitleBar />
        <Suspense fallback={null}>{ready && (onboarded ? content : <Onboarding />)}</Suspense>
      </main>
      {!ready && <Splash />}
      <CoreDown />
      <Notices />
    </div>
  );
}
