// «Создать команду с помощью AI»: описание словами → POST /api/commands/build → шаги → «Применить».
import { Sparkles, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/api/client";
import type { CommandAction } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { navigate } from "@/lib/router";
import { cx } from "@/ui";
import { actionIcon, countText, stepNote, valueText, W_STEPS } from "./model";
import { useApps, useCatalog } from "./store";

export interface BuildStep {
  title: string;
  detail?: string;
  icon?: string;
  note?: string;
  hint?: string;
  source?: string;
}

export interface BuildResult {
  name: string;
  phrases: string[];
  optional?: string[];
  actions: CommandAction[];
  steps: BuildStep[];
  icon?: string;
  say?: string;
}

type Phase = "idle" | "building" | "done" | "error";

const EXAMPLE = "Например: открой OBS, подожди 2 секунды и включи музыку в Spotify";

export function AiBuilder({
  open,
  onClose,
  onApply,
}: {
  open: boolean;
  onClose: () => void;
  onApply: (result: BuildResult) => void;
}) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<BuildResult | null>(null);
  const [error, setError] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  const req = useRef(0);
  const byType = useCatalog((s) => s.byType);
  const apps = useApps((s) => s.apps);

  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) {
      req.current++;
      setPhase((p) => (p === "building" ? "idle" : p));
      return;
    }
    const prev = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => area.current?.focus(), 40);
    const key = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    document.addEventListener("keydown", key);
    return () => {
      clearTimeout(t);
      document.removeEventListener("keydown", key);
      prev?.focus?.();
    };
  }, [open]);

  const build = async () => {
    const description = text.trim();
    if (!description) {
      area.current?.focus();
      return;
    }
    const my = ++req.current;
    setPhase("building");
    setError("");
    try {
      const res = await api.post<BuildResult>("/api/commands/build", { description });
      if (my !== req.current) return;
      const actions = Array.isArray(res?.actions) ? res.actions : [];
      if (!actions.length) throw new Error("Не получилось разобрать описание — попробуйте написать по шагам.");
      setResult({
        ...res,
        phrases: Array.isArray(res.phrases) ? res.phrases : [],
        actions,
        steps: Array.isArray(res.steps) ? res.steps : [],
      });
      setPhase("done");
    } catch (e) {
      if (my !== req.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  };

  if (!open) return null;

  const steps: (BuildStep & { iconKey: string })[] = (result?.actions ?? []).map((action, i) => {
    const at = byType.get(action.type);
    const s = result?.steps[i];
    return {
      title: s?.title || at?.title || action.type,
      detail: s?.detail ?? valueText(action, at, apps),
      note: s?.note ?? s?.hint ?? s?.source ?? stepNote(action, at, apps),
      iconKey: s?.icon || actionIcon(action, at, apps),
    };
  });
  const aiProblem = /ключ|key|провайдер|provider|настро|ai/i.test(error);

  return createPortal(
    <div className="modal-backdrop ai-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ai-panel" role="dialog" aria-modal="true" aria-labelledby="ai-builder-title">
        <div className="ai-head">
          <Sparkles className="ai-spark" />
          <h3 id="ai-builder-title">Создать команду с помощью AI</h3>
          <span className="spacer" />
          <button type="button" className="icon-btn ghost sm" aria-label="Закрыть" title="Закрыть" onClick={onClose}>
            <X />
          </button>
        </div>
        <textarea
          ref={area}
          className="textarea ai-text"
          value={text}
          placeholder={EXAMPLE}
          spellCheck={false}
          readOnly={phase === "building"}
          onChange={(e) => {
            setText(e.target.value);
            if (phase === "done" || phase === "error") setPhase("idle");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !e.shiftKey)) {
              e.preventDefault();
              if (phase !== "building") build();
            }
          }}
          aria-label="Что должна делать команда"
        />
        {phase === "done" ? (
          <div className="btn btn-primary btn-lg btn-block ai-go done" role="status">
            <Sparkles />
            Готово — {countText(steps.length, W_STEPS)}
          </div>
        ) : (
          <button
            type="button"
            className={cx("btn btn-primary btn-lg btn-block ai-go", phase === "building" && "building")}
            onClick={build}
            disabled={phase === "building"}
            aria-busy={phase === "building"}
          >
            <Sparkles />
            {phase === "building" ? "Собираю из действий…" : "Собрать"}
            {phase === "building" && <span className="ai-progress" aria-hidden />}
          </button>
        )}

        {phase === "error" && (
          <div className="ai-error" role="alert">
            <TriangleAlert />
            <div className="grow">
              <div>Не получилось собрать команду</div>
              <div className="faint small">{error}</div>
            </div>
            {aiProblem && (
              <button type="button" className="btn btn-sm" onClick={() => navigate("settings", "ai")}>
                Настроить Jarvis AI
              </button>
            )}
          </div>
        )}

        {phase === "done" && result && (
          <>
            <div className="ai-steps">
              {steps.map((s, i) => (
                <div key={i} className="ai-step" style={{ animationDelay: `${i * 70}ms` }}>
                  <span className="ai-step-n">{i + 1}</span>
                  <span className="ai-step-icon">
                    <AppIcon name={s.iconKey} size={34} radius={10} />
                  </span>
                  <span className="grow">
                    <span className="ai-step-title ellipsis">{s.title}</span>
                    {s.detail && <span className="ai-step-detail ellipsis">{s.detail}</span>}
                  </span>
                  {s.note && <span className="ai-step-note">{s.note}</span>}
                </div>
              ))}
            </div>
            {(result.name || result.phrases.length > 0) && (
              <div className="ai-summary">
                {result.name && (
                  <span>
                    <span className="faint">Название:</span> {result.name}
                  </span>
                )}
                {result.phrases.length > 0 && (
                  <span className="ai-phrases">
                    <span className="faint">Фразы:</span>
                    {result.phrases.slice(0, 3).map((p) => (
                      <span key={p} className="chip">
                        {p}
                      </span>
                    ))}
                    {result.phrases.length > 3 && <span className="faint">+{result.phrases.length - 3}</span>}
                  </span>
                )}
              </div>
            )}
            <div className="ai-foot">
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setPhase("idle");
                  requestAnimationFrame(() => area.current?.focus());
                }}
              >
                Изменить описание
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  // следующее открытие — с чистого листа
                  setPhase("idle");
                  setResult(null);
                  setText("");
                  onApply(result);
                }}
              >
                Применить
              </button>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
