// Панель агента (вверху по центру): «● Агент Jarvis · Шаг 2 из 4 · ■ Стоп», что он делает сейчас
// («Нажимаю «Написать»»), короткий список шагов. Когда агенту нужно разрешение — «Жду вашего разрешения»
// с карточкой действия. После завершения — итоговая строка и через 4 с панель уходит.
import {
  AppWindow,
  Check,
  CircleAlert,
  Eye,
  Hourglass,
  Keyboard,
  ListChecks,
  MousePointer2,
  Search,
  Sparkles,
  Square,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { useApp } from "@/store/app";
import { cx } from "@/ui";
import { useOverlayEvent } from "./bus";
import { DEMO, overlayToast, Slot } from "./common";
import { ApprovalBox, ConfirmButtons, Countdown } from "./Confirm";

interface AgentState {
  run_id: string;
  task: string;
  status: "planning" | "running" | "waiting" | "done" | "stopped" | "error" | "idle";
  step: number;
  total: number;
  text: string;
}

interface AgentStep {
  run_id: string;
  index: number;
  title: string;
  status: "running" | "ok" | "error";
}

const FINISHED = new Set(["done", "stopped", "error"]);
const HIDE_AFTER_MS = 4000;

function actionIcon(text: string, status: AgentState["status"]): ReactNode {
  const t = text.toLowerCase();
  if (status === "planning" || t.startsWith("составляю")) return <ListChecks />;
  if (status === "waiting" || t.startsWith("жду")) return <Hourglass />;
  if (/^(нажима|кликаю|щёлкаю|выбира)/.test(t)) return <MousePointer2 />;
  if (/^(ввож|печата|пишу|набира|вставля)/.test(t)) return <Keyboard />;
  if (/^(откры|запуска|переключа|разворачива|закрыва)/.test(t)) return <AppWindow />;
  if (/^(ищу|поиск|ищем)/.test(t)) return <Search />;
  if (/^(смотр|изуча|читаю|делаю снимок|осматрива)/.test(t)) return <Eye />;
  return <Sparkles />;
}

function StepIcon({ status }: { status: AgentStep["status"] }) {
  if (status === "running") return <span className="spinner ov-step-spin" />;
  return status === "ok" ? <Check className="ov-step-ok" /> : <X className="ov-step-err" />;
}

export function AgentPanel({ hidden }: { hidden: boolean }) {
  const [run, setRun] = useState<AgentState | null>(null);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [gone, setGone] = useState(false);
  const [stopping, setStopping] = useState(false);
  const runId = useRef("");
  const hideTimer = useRef(0);
  const confirmations = useApp((s) => s.confirmations);
  const approvals = useMemo(() => confirmations.filter((c) => c.kind === "agent"), [confirmations]);
  const approval = approvals[0];

  useOverlayEvent<AgentState>("agent.state", (s) => {
    if (!s || s.status === "idle") return;
    clearTimeout(hideTimer.current);
    if (s.run_id !== runId.current) {
      runId.current = s.run_id;
      setSteps([]);
      setStopping(false);
    }
    setRun(s);
    setGone(false);
    if (FINISHED.has(s.status)) hideTimer.current = window.setTimeout(() => setGone(true), HIDE_AFTER_MS);
  });

  useOverlayEvent<AgentStep>("agent.step", (st) => {
    if (!st || (runId.current && st.run_id !== runId.current)) return;
    setSteps((list) => {
      const i = list.findIndex((x) => x.index === st.index);
      if (i >= 0) return list.map((x, k) => (k === i ? st : x));
      return [...list, st].slice(-12);
    });
  });

  useEffect(() => () => clearTimeout(hideTimer.current), []);

  const stop = async () => {
    setStopping(true);
    if (DEMO) return;
    try {
      await api.post("/api/agent/stop");
    } catch (e) {
      setStopping(false);
      overlayToast("Агент не остановился", e instanceof Error ? e.message : String(e));
    }
  };

  const finished = !!run && FINISHED.has(run.status);
  const active = (!!run && !gone) || !!approval;
  // над полноэкранной игрой — только если ждём разрешения
  const show = active && (!hidden || !!approval);
  const status = run?.status ?? "waiting";
  const stepLabel = run && run.total > 0 ? `Шаг ${Math.max(1, Math.min(run.step || 1, run.total))} из ${run.total}` : "";
  const recent = steps.slice(-3);

  return (
    <Slot show={show}>
      <div className={cx("ov-card ov-agent ov-anim", `is-${status}`, approval && "is-asking")} data-interactive>
        <div className="ov-agent-head">
          <span className={cx("ov-agent-dot", !finished && "live")} />
          <span className="ov-agent-name">Агент Jarvis</span>
          <span className="spacer" />
          {stepLabel && <span className="ov-agent-step">{stepLabel}</span>}
          {!finished && (
            <button type="button" className="ov-stop" onClick={stop} disabled={stopping}>
              <Square />
              {stopping ? "Стоп…" : "Стоп"}
            </button>
          )}
        </div>

        {approval ? (
          <>
            <div className="ov-agent-title">Жду вашего разрешения</div>
            <ApprovalBox c={approval}>
              <ConfirmButtons c={approval} />
            </ApprovalBox>
            {approvals.length > 1 && <div className="ov-agent-more">Ещё запросов: {approvals.length - 1}</div>}
            <Countdown c={approval} />
          </>
        ) : finished ? (
          <div className={cx("ov-agent-final", `is-${status}`)}>
            <span className="ov-agent-final-ico">{status === "done" ? <Check /> : status === "error" ? <CircleAlert /> : <Square />}</span>
            <div className="grow">
              <div className="ov-agent-final-title">
                {status === "done" ? "Готово" : status === "error" ? "Не получилось" : "Остановлено"}
              </div>
              {run?.text && <div className="ov-agent-final-text">{run.text}</div>}
            </div>
          </div>
        ) : (
          <>
            <div className="ov-agent-action" key={run?.text}>
              <span className="ov-agent-action-ico">{actionIcon(run?.text ?? "", status)}</span>
              <span className="ov-agent-action-text">{run?.text || "Составляю план…"}</span>
            </div>
            {recent.length > 0 && (
              <ol className="ov-agent-steps">
                {recent.map((s) => (
                  <li key={s.index} className={cx(`is-${s.status}`)}>
                    <StepIcon status={s.status} />
                    <span className="ellipsis">{s.title}</span>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </div>
    </Slot>
  );
}
