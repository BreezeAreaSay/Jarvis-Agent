import "@fontsource-variable/inter";
import "@fontsource-variable/manrope";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/shell.css";
import "./styles/widgets.css";

import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { initCore } from "@/api/client";
import { startEvents } from "@/api/events";
import { Shell } from "@/app/Shell";
import { bindLevels } from "@/lib/levels";
import { windowKind } from "@/lib/router";
import { bindAppEvents } from "@/store/app";

const OverlayApp = lazy(() => import("@/overlay/OverlayApp"));
const AvatarApp = lazy(() => import("@/avatar/AvatarApp"));

const kind = windowKind();
if (kind !== "main") document.body.classList.add("transparent");

bindAppEvents();
bindLevels();
initCore().then(() => startEvents());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={null}>
      {kind === "overlay" ? <OverlayApp /> : kind === "avatar" ? <AvatarApp /> : <Shell />}
    </Suspense>
  </StrictMode>,
);
