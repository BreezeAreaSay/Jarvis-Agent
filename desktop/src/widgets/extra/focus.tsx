// Виджет «Фокус» на столе: кольцо таймера и кнопка «начать / пауза» (данные — состояние плагина «Фокус»).
// Высокий — кольцо крупно и минуты за сегодня, широкий — ещё и столбики за неделю.
import { ArrowUpRight, CircleStop, Pause, Play, Target } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { navigate } from "@/lib/router";
import {
  FocusRing,
  focusClock,
  focusLabel,
  focusTone,
  fmtMinutes,
  leftFrac,
  useFocusLeft,
  useFocusState,
  WeekBars,
} from "@/pages/extensions/focusKit";
import type { Plugin } from "@/pages/extensions/pluginKit";
import { bindPlugins, usePlugins } from "@/pages/extensions/pluginStore";
import { cx, notifyError } from "@/ui";
import { WidgetFrame, type WidgetProps } from "../common";
import { HeadButton, useBox } from "./shared";

/** Значок для каталога виджетов. */
export function FocusWidgetIcon() {
  return <Target />;
}

function useRun(): [boolean, (run: () => Promise<unknown>) => Promise<void>] {
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return [busy, run];
}

export function FocusWidget({ w, h, config }: WidgetProps) {
  const plugin = usePlugins((s) => s.items?.find((p) => p.id === "focus"));
  const loading = usePlugins((s) => s.loading);
  useEffect(() => bindPlugins(), []);
  const [busy, run] = useRun();
  const title = (config.title as string) || "Фокус";
  const open = <HeadButton icon={<ArrowUpRight />} label="Открыть «Фокус»" onClick={() => navigate("extensions", "plugins", "focus")} hover />;

  if (plugin?.enabled) return <FocusBody plugin={plugin} w={w} h={h} title={title} open={open} />;
  return (
    <WidgetFrame icon={<Target />} title={title} actions={open}>
      <div className="fx-w-off">
        {h >= 2 && (
          <span className="fx-w-off-ico">
            <Target />
          </span>
        )}
        <span className="sub">{plugin ? "Помодоро, тишина и защита от отвлечений" : loading ? "Загружаю…" : "Плагин «Фокус» недоступен"}</span>
        {plugin && (
          <button className="btn btn-sm btn-soft" disabled={busy} onClick={() => run(() => usePlugins.getState().update("focus", { enabled: true }))}>
            Включить
          </button>
        )}
      </div>
    </WidgetFrame>
  );
}

/** Включённый плагин: кольцо по размеру виджета (меряем тело — оно есть с первого кадра). */
function FocusBody({ plugin, w, h, title, open }: { plugin: Plugin; w: number; h: number; title: string; open: ReactNode }) {
  const { state, at } = useFocusState(plugin);
  const left = useFocusLeft(state, at);
  const [ref, box] = useBox<HTMLDivElement>();
  const [busy, run] = useRun();
  const action = (name: string) => run(() => usePlugins.getState().action("focus", name));

  const phase = state?.phase ?? "idle";
  const tone = focusTone(state);
  const running = phase === "focus" || phase === "break";
  const live = plugin.status?.running !== false;
  const time = phase === "idle" ? focusClock((state?.focus_min ?? 25) * 60) : focusClock(left);
  const main = () => action(running ? "pause" : phase === "paused" || state?.waiting ? "resume" : "start");
  const mainLabel = running ? "Пауза" : phase === "paused" ? "Продолжить" : "Начать фокус";

  const tall = h >= 2;
  const wide = w >= 2;
  // кольцо — сколько позволяет место: высокий узкий — над кнопками, высокий широкий — во всю высоту рядом
  // со столбиками, низкий — по высоте строки
  const fit = tall ? (wide ? Math.min(box.width * 0.42, box.height) : Math.min(box.width * 0.86, box.height - 48)) : Math.min(box.height, box.width * 0.3);
  const ring = Math.round(Math.max(44, fit));
  const timeInRing = ring >= 64;
  const phaseInRing = ring >= 96;

  return (
    <WidgetFrame
      icon={<Target />}
      title={title}
      className={cx("fx-widget", `fxt-${tone}`, tall && "tall", wide && "wide")}
      actions={
        <>
          <span className="fx-w-today" title="В фокусе сегодня">
            {fmtMinutes(state?.today_min ?? 0)}
          </span>
          {open}
        </>
      }
    >
      <div ref={ref} className="fx-w-body">
        {box.width > 0 && (
          <FocusRing size={ring} frac={leftFrac(state, left)} tone={tone} stroke={phaseInRing ? 6.5 : 9}>
            {timeInRing && <span className="fx-ring-time">{time}</span>}
            {phaseInRing && <span className="fx-ring-phase">{focusLabel(state)}</span>}
          </FocusRing>
        )}
        <div className="fx-w-side">
          {!(timeInRing && phaseInRing) && (
            <div className="fx-w-info">
              {!timeInRing && <span className="fx-w-time">{time}</span>}
              {!phaseInRing && <span className="fx-w-phase">{focusLabel(state)}</span>}
            </div>
          )}
          {wide && <WeekBars days={state?.week} className="fx-week-widget" compact />}
          <div className="fx-w-ctl">
            <button className={cx("wx-round-btn fx-w-main", running && "on")} disabled={busy || !live} onClick={main} aria-label={mainLabel} title={mainLabel}>
              {running ? <Pause /> : <Play />}
            </button>
            {phase !== "idle" && (
              <button className="icon-btn ghost sm" disabled={busy} onClick={() => action("stop")} aria-label="Закончить" title="Закончить фокус">
                <CircleStop />
              </button>
            )}
          </div>
        </div>
      </div>
    </WidgetFrame>
  );
}
