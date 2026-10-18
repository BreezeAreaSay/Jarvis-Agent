// Оверлей — прозрачное окно поверх всех программ на рабочей области основного экрана (над панелью задач):
// строка голоса, уведомления, подтверждения, агент, субтитры, диктовка, чтение вслух, конспект и питомец.
// Окно сквозное для мыши: ловят её только интерактивные элементы (см. clickthrough.ts).
import "@/styles/overlay.css";
import { useRef } from "react";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { AgentPanel } from "./AgentPanel";
import { useClickThrough } from "./clickthrough";
import { type Bounds, DEMO, useBounds, useFullscreenHide, useUiZoom } from "./common";
import { Confirmations } from "./Confirm";
import { DemoBackdrop, useDemo } from "./demo";
import { Pet } from "./pet/Pet";
import { DictationPill, ReaderPlayer, Subtitles, SummaryBadge } from "./PluginViews";
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
      <div className="ov-bottom">
        <Subtitles hidden={hidden} />
        <DictationPill hidden={hidden} />
      </div>
      <ReaderPlayer hidden={hidden} bottom={readerBottom} />
      <Pet hidden={hidden} />
      <SummaryBadge hidden={hidden} />
      <div className="ov-top">
        <VoicePill hidden={hidden} />
        <AgentPanel hidden={hidden} />
        <Confirmations />
        <Toasts hidden={hidden} />
      </div>
    </div>
  );
}
