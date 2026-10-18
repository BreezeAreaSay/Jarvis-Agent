// Общие части «Сценариев»: значок с градиентом, чипы триггеров, дни недели, витрина шаблонов.
import { Check, Filter, Plus, SlidersHorizontal } from "lucide-react";
import { type CSSProperties, useState } from "react";
import type { ScenarioConditions, ScenarioTemplate, ScenarioTrigger } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { navigate } from "@/lib/router";
import { cx, notify, notifyError } from "@/ui";
import { conditionsLabel, SCENARIO_ICONS, TRIGGERS, triggerLabel, WEEKDAYS } from "./scenarioModel";
import { scenarioMemory, useScenarios } from "./scenarioStore";

/** Значок сценария: свой — плитка с градиентом, ключ программы («steam») — фирменный значок. */
export function ScenarioIcon({ name, size = 44, radius }: { name: string; size?: number; radius?: number }) {
  const meta = SCENARIO_ICONS[name];
  if (!meta) return <AppIcon name={name || "command"} size={size} radius={radius ?? Math.round(size * 0.3)} />;
  const Icon = meta.icon;
  return (
    <span
      className="sc-icon"
      aria-hidden
      style={
        {
          width: size,
          height: size,
          borderRadius: radius ?? Math.round(size * 0.3),
          "--sc-from": meta.from,
          "--sc-to": meta.to,
        } as CSSProperties
      }
    >
      <Icon style={{ width: Math.round(size * 0.5), height: Math.round(size * 0.5) }} strokeWidth={2.1} />
    </span>
  );
}

export function iconGlow(name: string): CSSProperties | undefined {
  const meta = SCENARIO_ICONS[name];
  return meta ? ({ "--sc-from": meta.from, "--sc-to": meta.to } as CSSProperties) : undefined;
}

/** Чип триггера: «⏰ Будни 8:00». */
export function TriggerChip({ trigger }: { trigger: ScenarioTrigger }) {
  const Icon = TRIGGERS[trigger.type]?.icon;
  const label = triggerLabel(trigger);
  return (
    <span className={cx("sc-chip", `t-${trigger.type}`)} title={`${TRIGGERS[trigger.type]?.title ?? ""}: ${label}`}>
      {Icon && <Icon />}
      <span className="ellipsis">{label}</span>
    </span>
  );
}

export function TriggerChips({
  triggers,
  conditions,
  max = 3,
}: {
  triggers: ScenarioTrigger[];
  conditions?: ScenarioConditions;
  max?: number;
}) {
  const shown = triggers.slice(0, max);
  const extra = triggers.length - shown.length;
  const cond = conditionsLabel(conditions);
  return (
    <span className="sc-chips">
      {shown.map((t, i) => (
        <TriggerChip key={`${t.type}-${i}`} trigger={t} />
      ))}
      {extra > 0 && <span className="sc-chip more">+{extra}</span>}
      {cond && (
        <span className="sc-chip cond" title={`Только: ${cond}`}>
          <Filter />
          <span className="ellipsis">{cond}</span>
        </span>
      )}
    </span>
  );
}

/** Дни недели чипами. Пустой список — «каждый день» (все дни горят); последний день не снять. */
export function WeekdayChips({
  value,
  onChange,
  size = "md",
}: {
  value: number[];
  onChange: (days: number[]) => void;
  size?: "sm" | "md";
}) {
  const all = !value.length || value.length === 7;
  const on = (d: number) => all || value.includes(d);
  const toggle = (d: number) => {
    const current = all ? [0, 1, 2, 3, 4, 5, 6] : value;
    const next = current.includes(d) ? current.filter((x) => x !== d) : [...current, d].sort((a, b) => a - b);
    if (!next.length) return;
    onChange(next.length === 7 ? [] : next);
  };
  return (
    <span className={cx("sc-days", size === "sm" && "sm")} role="group" aria-label="Дни недели">
      {WEEKDAYS.map((label, d) => (
        <button
          key={label}
          type="button"
          className={cx("sc-day", on(d) && "on", d >= 5 && "weekend")}
          aria-pressed={on(d)}
          onClick={() => toggle(d)}
        >
          {label}
        </button>
      ))}
    </span>
  );
}

// --- шаблоны --------------------------------------------------------------------------------------------
export function TemplateGrid({ onAdded, compact }: { onAdded?: () => void; compact?: boolean }) {
  const templates = useScenarios((s) => s.templates);
  const status = useScenarios((s) => s.templatesStatus);
  const [busy, setBusy] = useState<string | null>(null);

  const add = async (t: ScenarioTemplate) => {
    setBusy(t.id);
    try {
      const item = await useScenarios.getState().fromTemplate(t.id);
      scenarioMemory.highlight = item.id;
      notify(`Сценарий «${item.name}» добавлен и включён`, "ok");
      onAdded?.();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(null);
    }
  };

  if (status !== "ready" && !templates.length) {
    return (
      <div className={cx("sc-tpl-grid", compact && "compact")}>
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="sc-tpl skeleton" style={{ animationDelay: `${i * 60}ms` }} />
        ))}
      </div>
    );
  }
  return (
    <div className={cx("sc-tpl-grid", compact && "compact")}>
      {templates.map((t, i) => (
        <article
          key={t.id}
          className={cx("sc-tpl", t.added && "added")}
          style={{ ...iconGlow(t.icon), animationDelay: `${i * 45}ms` } as CSSProperties}
        >
          <button
            type="button"
            className="sc-tpl-main"
            title="Открыть в редакторе и настроить"
            onClick={() => {
              onAdded?.();
              navigate("commands", "scenarios", "new", t.id);
            }}
          >
            <ScenarioIcon name={t.icon} size={52} radius={16} />
            <span className="sc-tpl-name">{t.name}</span>
            <span className="sc-tpl-desc">{t.description}</span>
          </button>
          <TriggerChips triggers={t.triggers} conditions={t.conditions} />
          <div className="sc-tpl-foot">
            {t.added ? (
              <span className="sc-tpl-added">
                <Check /> Добавлен
              </span>
            ) : (
              <button
                type="button"
                className="btn btn-sm btn-primary sc-tpl-add"
                disabled={busy === t.id}
                onClick={() => add(t)}
              >
                {busy === t.id ? <span className="spinner" /> : <Plus />}
                Добавить
              </button>
            )}
            <button
              type="button"
              className="btn btn-sm btn-ghost sc-tpl-edit"
              onClick={() => {
                onAdded?.();
                navigate("commands", "scenarios", "new", t.id);
              }}
            >
              <SlidersHorizontal />
              {t.added ? "Ещё один" : "Настроить"}
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}
