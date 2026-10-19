// Карточка агента над полем ввода: «● Агент Jarvis · Шаг 2 из 4 · ■ Стоп», текущее действие, шаги и
// «Жду вашего разрешения» (подтверждения kind = "agent" из общего хранилища).
import {
  Check,
  ChevronDown,
  CircleAlert,
  CircleCheck,
  CircleStop,
  CircleX,
  Eye,
  Globe,
  Hourglass,
  Keyboard,
  ListTodo,
  MessageCircleQuestionMark,
  MousePointer2,
  MoveVertical,
  Search,
  ShieldCheck,
  Sparkles,
  Square,
  X,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import type { Confirmation } from "@/api/types";
import { useApp } from "@/store/app";
import { Button, cx, IconButton } from "@/ui";
import { useAi } from "./store";
import { isTerminal } from "./types";

function actionIcon(text: string): ReactNode {
  const t = text.toLowerCase();
  if (/^(открыва|запуска|перехожу)/.test(t)) return <Globe />;
  if (/^(нажима|кликаю|щёлкаю|щелкаю|выбираю)/.test(t)) return <MousePointer2 />;
  if (/^(ввож|пишу|печата|набираю|заполня)/.test(t)) return <Keyboard />;
  if (/^(ищу|ищем|поиск)/.test(t)) return <Search />;
  if (/^(прокруч|листаю)/.test(t)) return <MoveVertical />;
  if (/^(жду|ожида)/.test(t)) return <Hourglass />;
  if (/^(спрашива|уточня)/.test(t)) return <MessageCircleQuestionMark />;
  if (/^(составляю|планирую|думаю)/.test(t)) return <ListTodo />;
  if (/^(смотрю|читаю|изучаю|анализ|проверяю)/.test(t)) return <Eye />;
  return <Sparkles />;
}

const STATUS_TEXT: Record<string, string> = {
  planning: "Составляю план…",
  running: "Работаю…",
  waiting: "Жду вашего разрешения",
  done: "Готово",
  stopped: "Остановлено",
  error: "Не получилось",
};

function Approval({ c }: { c: Confirmation }) {
  const resolve = useApp((s) => s.resolveConfirmation);
  const [busy, setBusy] = useState<"allow" | "deny" | null>(null);
  const act = (allow: boolean) => {
    setBusy(allow ? "allow" : "deny");
    resolve(c.id, allow).finally(() => setBusy(null));
  };
  return (
    <div className="agent-approval pop-in">
      <div className="agent-approval-text">
        <ShieldCheck className="shield" />
        <div className="grow">
          <div className="t">{c.title}</div>
          {c.detail && <div className="d">{c.detail}</div>}
        </div>
      </div>
      <div className="agent-approval-btns">
        <Button onClick={() => act(false)} loading={busy === "deny"} disabled={!!busy}>
          Отклонить
        </Button>
        <Button variant="primary" onClick={() => act(true)} loading={busy === "allow"} disabled={!!busy}>
          Разрешить
        </Button>
      </div>
    </div>
  );
}

export function AgentPanel() {
  const agent = useAi((s) => s.agent);
  const stopAgent = useAi((s) => s.stopAgent);
  const dismiss = useAi((s) => s.dismissAgent);
  const confirmations = useApp((s) => s.confirmations);
  const approvals = useMemo(() => confirmations.filter((c) => c.kind === "agent"), [confirmations]);
  const [stepsOpen, setStepsOpen] = useState(false);
  const [stopping, setStopping] = useState(false);

  const terminal = agent ? isTerminal(agent.status) : false;
  const endedAt = agent?.endedAt;
  useEffect(() => {
    if (!terminal || !endedAt) return;
    setStopping(false);
    const t = setTimeout(dismiss, agent?.status === "error" ? 9000 : 4500);
    return () => clearTimeout(t);
  }, [terminal, endedAt, dismiss, agent?.status]);

  if (!agent && approvals.length === 0) return null;

  const running = !!agent && !terminal;
  const now = agent ? agent.text || STATUS_TEXT[agent.status] : "";
  const steps = agent?.steps ?? [];
  const plan = agent?.plan ?? [];
  const current = agent ? Math.max(1, Math.min(agent.step || 1, plan.length)) : 1;
  // итог: «Готово» только если агент сам подтвердил успех; «Требует внимания» — сделано не всё
  const outcome = agent?.outcome ?? "";
  const finalKind =
    agent?.status === "done" ? (outcome === "failed" ? "error" : outcome === "partial" ? "partial" : "done") : (agent?.status ?? "");
  const finalText = finalKind === "partial" ? "Требует внимания" : STATUS_TEXT[finalKind] ?? "";

  return (
    <section className={cx("agent-card pop-in", terminal && `ended ${agent?.status}`)} aria-live="polite">
      <div className="agent-head">
        <span className={cx("agent-dot", running && "live", terminal && agent?.status)} />
        <span className="agent-name">Агент Jarvis</span>
        <span className="spacer" />
        {agent && agent.total > 0 && (
          <span className="agent-step">
            Шаг {Math.max(1, Math.min(agent.step, agent.total))} из {agent.total}
          </span>
        )}
        {running ? (
          <button
            type="button"
            className="agent-stop"
            disabled={stopping}
            onClick={() => {
              setStopping(true);
              stopAgent().finally(() => setTimeout(() => setStopping(false), 4000));
            }}
          >
            <Square /> {stopping ? "Останавливаю…" : "Стоп"}
          </button>
        ) : (
          agent && <IconButton icon={<X />} label="Скрыть" variant="ghost" size="sm" onClick={dismiss} />
        )}
      </div>

      {approvals.length > 0 ? (
        <>
          <div className="agent-now">Жду вашего разрешения</div>
          {approvals.map((c) => (
            <Approval key={c.id} c={c} />
          ))}
        </>
      ) : (
        agent && (
          <div className={cx("agent-now", terminal && "final")}>
            {terminal ? (
              finalKind === "done" ? (
                <CircleCheck className="ok" />
              ) : finalKind === "stopped" ? (
                <CircleStop className="muted-icon" />
              ) : finalKind === "partial" ? (
                <CircleAlert className="warn" />
              ) : (
                <CircleX className="err" />
              )
            ) : (
              actionIcon(now)
            )}
            <span>{terminal ? finalText : now}</span>
          </div>
        )
      )}

      {running && plan.length > 0 && approvals.length === 0 && (
        <ol className="agent-plan">
          {plan.map((title, i) => {
            const n = i + 1;
            const state = n < current ? "done" : n === current ? "now" : "next";
            return (
              <li key={n} className={state}>
                <span className="agent-plan-mark">{state === "done" ? <Check /> : n}</span>
                <span className="ellipsis">{title}</span>
              </li>
            );
          })}
        </ol>
      )}

      {steps.length > 0 && (
        <div className="agent-steps">
          <button type="button" className="agent-steps-toggle" onClick={() => setStepsOpen(!stepsOpen)} aria-expanded={stepsOpen}>
            Действия · {steps.length}
            <ChevronDown className={cx(stepsOpen && "open")} />
          </button>
          {stepsOpen && (
            <ol className="agent-step-list">
              {steps.map((s) => (
                <li key={s.index} className={s.status}>
                  {s.status === "running" ? (
                    <span className="spinner" />
                  ) : s.status === "ok" ? (
                    <CircleCheck />
                  ) : (
                    <CircleX />
                  )}
                  <span className="ellipsis">{s.title}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}
