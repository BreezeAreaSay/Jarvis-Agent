// Оверлей — прозрачное окно поверх всех программ на рабочей области основного экрана (над панелью задач):
// строка голоса, уведомления, подтверждения, агент, субтитры, диктовка, чтение вслух, конспект и питомец.
// Окно сквозное для мыши: ловят её только интерактивные элементы (см. clickthrough.ts).
import "@/styles/overlay.css";
import { useRef } from "react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { AgentPanel } from "./AgentPanel";
import { ClipboardOverlay } from "./ClipboardOverlay";
import { useClickThrough } from "./clickthrough";
import { type Bounds, DEMO, useBounds, useFullscreenHide, useUiZoom } from "./common";
import { Confirmations } from "./Confirm";
import { DemoBackdrop, useDemo } from "./demo";
import { FileSearchOverlay } from "./FileSearch";
import { FocusOverlay } from "./Focus";
import { Pet } from "./pet/Pet";
import { DictationPill, ReaderPlayer, RecordingBadges, Subtitles } from "./PluginViews";
import { Toasts } from "./Toasts";
import { VoicePill } from "./VoicePill";

const READER_W = 360;
const READER_H = 236;
const EDGE = 22;

/** Аватар по умолчанию стоит в правом нижнем углу — мини-плеер чтения поднимаем над ним. */
function useReaderBottom(bounds: Bounds): number {
  const avatar = useApp((s) => s.settings?.avatar);
  const zoom = useUiZoom();
  if (!avatar?.desktop) return EDGE / zoom;
  const size = (avatar.size ?? 150) + 40;
  let bottom = EDGE;
  if (!Array.isArray(avatar.position)) {
    bottom = 24 + size + 10;
  } else {
    const ax = avatar.position[0] - bounds.x;
    const ay = avatar.position[1] - bounds.y;
    const left = bounds.width - EDGE - READER_W * zoom;
    const top = bounds.height - EDGE - READER_H * zoom;
    if (ax + size > left && ay + size > top && ax < bounds.width) bottom = Math.max(EDGE, bounds.height - ay + 10);
  }
  return bottom / zoom;
}

export default function OverlayApp() {
  useClickThrough();
  const bounds = useBounds();
  const hidden = useFullscreenHide();
  const windowRef = useRef<HTMLDivElement>(null);
  useDemo(DEMO, windowRef, bounds);
  const readerBottom = useReaderBottom(bounds);

  return (
    <div className={cx("ov-root", DEMO && "is-demo")}>
      {DEMO && <DemoBackdrop scene={DEMO} windowRef={windowRef} />}
      {/* каждая часть — со своим предохранителем: упавшая исчезнет, остальные работают (причина — в ui.log) */}
      <div className="ov-bottom">
        <ErrorBoundary silent area="оверлей: субтитры">
          <Subtitles />
        </ErrorBoundary>
        <ErrorBoundary silent area="оверлей: диктовка">
          <DictationPill hidden={hidden} />
        </ErrorBoundary>
      </div>
      <ErrorBoundary silent area="оверлей: чтение вслух">
        <ReaderPlayer hidden={hidden} bottom={readerBottom} />
      </ErrorBoundary>
      <ErrorBoundary silent area="оверлей: питомец">
        <Pet hidden={hidden} />
      </ErrorBoundary>
      <ErrorBoundary silent area="оверлей: запись и субтитры">
        <RecordingBadges hidden={hidden} />
      </ErrorBoundary>
      <ErrorBoundary silent area="оверлей: фокус">
        <FocusOverlay hidden={hidden} />
      </ErrorBoundary>
      <div className="ov-top">
        <ErrorBoundary silent area="оверлей: строка голоса">
          <VoicePill hidden={hidden} />
        </ErrorBoundary>
        <ErrorBoundary silent area="оверлей: агент">
          <AgentPanel hidden={hidden} />
        </ErrorBoundary>
        <ErrorBoundary silent area="оверлей: поиск по ПК">
          <FileSearchOverlay hidden={hidden} />
        </ErrorBoundary>
        <ErrorBoundary silent area="оверлей: подтверждения">
          <Confirmations />
        </ErrorBoundary>
        <ErrorBoundary silent area="оверлей: уведомления">
          <Toasts hidden={hidden} />
        </ErrorBoundary>
      </div>
      <ErrorBoundary silent area="оверлей: умный буфер">
        <ClipboardOverlay />
      </ErrorBoundary>
    </div>
  );
}
