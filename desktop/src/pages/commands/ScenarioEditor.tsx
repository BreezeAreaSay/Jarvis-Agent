// Редактор сценария «НОВЫЙ СЦЕНАРИЙ» / «РЕДАКТИРОВАНИЕ»: название и значок, «Когда» (триггеры), «Условия»
// (дни и часы), «Что сделать» (те же действия, что у команд — ActionList), «Что сказать после». Ctrl+S — сохранить.
import {
  BellRing,
  CirclePlus,
  ClipboardList,
  Mic,
  Play,
  Rocket,
  Sparkles,
  Trash2,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { CommandAction, Scenario, ScenarioIn, ScenarioTemplate, ScenarioTriggerType } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { Button, cx, Empty, IconButton, notify, notifyError, Toggle } from "@/ui";
import { ActionList, type DraftAction } from "./ActionList";
import { countText, newKey, W_ACTIONS } from "./model";
import { ConfirmDialog, Popover } from "./Overlays";
import { ScenarioIcon, WeekdayChips } from "./ScenarioParts";
import {
  availableSlots,
  conditionsLabel,
  draftErrors,
  ICON_KEYS,
  lastRunLine,
  newTrigger,
  SCENARIO_ICONS,
} from "./scenarioModel";
import { bindScenarioEvents, scenarioMemory, useScenarios } from "./scenarioStore";
import { useApps, useCatalog } from "./store";
import { AddTriggerMenu, type DraftTrigger, TimeInput, TriggerQuickStart, TriggerRow } from "./TriggerFields";

interface Draft {
  name: string;
  icon: string;
  enabled: boolean;
  triggers: DraftTrigger[];
  conditions: { weekdays: number[]; time_from: string; time_to: string };
  actions: DraftAction[];
  say: string;
}

const EMPTY: Draft = {
  name: "",
  icon: "sparkles",
  enabled: true,
  triggers: [],
  conditions: { weekdays: [], time_from: "", time_to: "" },
  actions: [],
  say: "",
};

type Source = Pick<Scenario, "name" | "icon" | "enabled" | "triggers" | "conditions" | "actions" | "say">;

function fromSource(s: Source): Draft {
  return {
    name: s.name,
    icon: s.icon || "sparkles",
    enabled: s.enabled ?? true,
    triggers: s.triggers.map((t) => ({ ...structuredClone(t), key: newKey() })) as DraftTrigger[],
    conditions: {
      weekdays: [...(s.conditions?.weekdays ?? [])],
      time_from: s.conditions?.time_from ?? "",
      time_to: s.conditions?.time_to ?? "",
    },
    actions: s.actions.map((a) => ({ key: newKey(), type: a.type, value: a.value ?? "" })),
    say: s.say ?? "",
  };
}

function snapshot(d: Draft): string {
  return JSON.stringify({
    ...d,
    triggers: d.triggers.map(({ key: _k, ...t }) => t),
    actions: d.actions.map((a) => [a.type, a.value]),
  });
}

/** «Доброе утро» уже есть — «Доброе утро 2». */
function uniqueName(base: string, taken: string[]): string {
  const used = new Set(taken.map((n) => n.toLowerCase()));
  if (!used.has(base.toLowerCase())) return base;
  for (let n = 2; ; n++) if (!used.has(`${base} ${n}`.toLowerCase())) return `${base} ${n}`;
}

const QUICK: { type: string; label: string; icon: ReactNode }[] = [
  { type: "Jarvis.Briefing", label: "Сводка дня", icon: <ClipboardList /> },
  { type: "Speak", label: "Сказать вслух", icon: <Mic /> },
  { type: "Notify", label: "Уведомление", icon: <BellRing /> },
  { type: "System.VolumeSet", label: "Громкость", icon: <Volume2 /> },
  { type: "Jarvis.SilentMode", label: "Тихий режим", icon: <VolumeX /> },
  { type: "Launch.App", label: "Запустить программу", icon: <Rocket /> },
];

export function ScenarioEditor({ id, template }: { id?: string; template?: string }) {
  const catalog = useCatalog((s) => s.items);
  const byType = useCatalog((s) => s.byType);
  const scenarios = useScenarios((s) => s.items);
  const creating = !id;

  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [state, setState] = useState<"loading" | "ready" | "missing">(creating && !template ? "ready" : "loading");
  const [source, setSource] = useState<Scenario | null>(null);
  const baseline = useRef(snapshot(EMPTY));
  const [tried, setTried] = useState(false);
  const [saving, setSaving] = useState(false);
  const [leave, setLeave] = useState<(() => void) | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [autoOpenKey, setAutoOpenKey] = useState<string | null>(null);
  const [freshTrigger, setFreshTrigger] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const whenRef = useRef<HTMLElement>(null);
  const doRef = useRef<HTMLElement>(null);

  useEffect(() => {
    bindScenarioEvents();
    useCatalog.getState().load();
    useApps.getState().load();
    if (useScenarios.getState().status === "idle") useScenarios.getState().load(true);
  }, []);

  // --- загрузка: сценарий для правки или шаблон для нового ---
  useEffect(() => {
    let alive = true;
    const apply = (d: Draft, src: Scenario | null, pristine: boolean) => {
      setSource(src);
      setDraft(d);
      // из шаблона — черновик сразу «изменён»: его ещё нужно сохранить
      baseline.current = snapshot(pristine ? d : EMPTY);
      setState("ready");
    };
    if (id) {
      const local = useScenarios.getState().items.find((s) => s.id === id);
      if (local) apply(fromSource(local), local, true);
      else
        api
          .get<Scenario>(`/api/scenarios/${encodeURIComponent(id)}`)
          .then((s) => alive && apply(fromSource(s), s, true))
          .catch(() => alive && setState("missing"));
    } else if (template) {
      const fromList = (items: ScenarioTemplate[]) => {
        const tpl = items.find((t) => t.id === template);
        if (!tpl) return setState("missing");
        const names = useScenarios.getState().items.map((s) => s.name);
        apply({ ...fromSource({ ...tpl, enabled: true }), name: uniqueName(tpl.name, names) }, null, false);
      };
      const known = useScenarios.getState().templates;
      if (known.length) fromList(known);
      else
        api
          .get<{ items: ScenarioTemplate[] }>("/api/scenarios/templates")
          .then((r) => alive && fromList(r.items ?? []))
          .catch(() => alive && setState("missing"));
    } else {
      apply(EMPTY, null, true);
      requestAnimationFrame(() => nameRef.current?.focus());
    }
    return () => {
      alive = false;
    };
  }, [id, template]);

  const dirty = snapshot(draft) !== baseline.current;
  const errors = draftErrors(draft, byType);
  const slots = useMemo(() => availableSlots(draft.triggers), [draft.triggers]);
  const set = useCallback(<K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v })), []);

  // --- триггеры ---
  const addTrigger = (type: ScenarioTriggerType) => {
    const t = { ...newTrigger(type), key: newKey() } as DraftTrigger;
    setDraft((d) => ({ ...d, triggers: [...d.triggers, t] }));
    setFreshTrigger(t.key);
  };
  const changeTrigger = (t: DraftTrigger) =>
    setDraft((d) => ({ ...d, triggers: d.triggers.map((x) => (x.key === t.key ? t : x)) }));
  const removeTrigger = (key: string) => setDraft((d) => ({ ...d, triggers: d.triggers.filter((x) => x.key !== key) }));

  // --- действия (как в редакторе команд) ---
  const addAction = (type = "Launch.App", open = true) => {
    const t = byType.has(type) ? type : (catalog[0]?.type ?? type);
    const key = newKey();
    setDraft((d) => ({ ...d, actions: [...d.actions, { key, type: t, value: "" }] }));
    if (open) setAutoOpenKey(key);
  };
  const changeAction = useCallback(
    (key: string, patch: Partial<DraftAction>) =>
      setDraft((d) => ({ ...d, actions: d.actions.map((a) => (a.key === key ? { ...a, ...patch } : a)) })),
    [],
  );
  const moveAction = useCallback(
    (from: number, to: number) =>
      setDraft((d) => {
        const list = d.actions.slice();
        const [it] = list.splice(from, 1);
        list.splice(to, 0, it);
        return { ...d, actions: list };
      }),
    [],
  );
  const removeAction = useCallback(
    (key: string) => setDraft((d) => ({ ...d, actions: d.actions.filter((a) => a.key !== key) })),
    [],
  );
  const duplicateAction = useCallback(
    (key: string) =>
      setDraft((d) => {
        const i = d.actions.findIndex((a) => a.key === key);
        if (i < 0) return d;
        const list = d.actions.slice();
        list.splice(i + 1, 0, { ...d.actions[i], key: newKey() });
        return { ...d, actions: list };
      }),
    [],
  );

  // --- сохранение ---
  const save = async () => {
    setTried(true);
    if (errors.name) {
      notify("Укажите название сценария", "error");
      nameRef.current?.focus();
      return;
    }
    if (errors.triggers) {
      notify(draft.triggers.length ? "Проверьте триггеры, отмеченные красным" : "Добавьте хотя бы один триггер", "error");
      whenRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }
    if (errors.work || errors.values) {
      notify(errors.work ? "Добавьте действие или фразу, которую скажет Jarvis" : "Заполните значения действий, отмеченные красным", "error");
      doRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }
    const body: ScenarioIn = {
      name: draft.name.trim(),
      icon: draft.icon,
      enabled: draft.enabled,
      triggers: draft.triggers.map(({ key: _k, ...t }) =>
        t.type === "phrase" ? { ...t, phrases: t.phrases.map((p) => p.trim()).filter(Boolean) } : t,
      ),
      conditions: draft.conditions,
      actions: draft.actions.map((a): CommandAction => ({ type: a.type, value: a.value.trim() })),
      say: draft.say.trim(),
      ...(!id && template ? { template } : {}),
    };
    setSaving(true);
    try {
      const res = id
        ? await api.patch<Scenario>(`/api/scenarios/${encodeURIComponent(id)}`, body)
        : await api.post<Scenario>("/api/scenarios", body);
      if (res && typeof res === "object" && "id" in res) {
        useScenarios.getState().upsert(res);
        scenarioMemory.highlight = res.id;
      }
      baseline.current = snapshot(draft);
      notify(creating ? "Сценарий создан" : "Сценарий сохранён", "ok");
      navigate("commands", "scenarios");
    } catch (e) {
      notifyError(e);
    } finally {
      setSaving(false);
    }
  };

  const guard = (go: () => void) => {
    if (dirty && !saving) setLeave(() => go);
    else go();
  };
  const cancel = () => guard(() => navigate("commands", "scenarios"));

  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.code === "KeyS") {
        e.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);

  const removeScenario = async () => {
    if (!id) return;
    try {
      await useScenarios.getState().remove(id);
      notify(`Сценарий «${source?.name ?? draft.name}» удалён`, "ok");
      baseline.current = snapshot(draft);
      navigate("commands", "scenarios");
    } catch (e) {
      notifyError(e);
    } finally {
      setDeleting(false);
    }
  };

  const runNow = async () => {
    if (!id) return;
    try {
      const res = await useScenarios.getState().run(id);
      if (res?.ok === false) notify(`${res.title} — ${res.message || "не получилось"}`, "error");
      else notify(`Сценарий «${source?.name ?? draft.name}» — выполнено ✓`, "ok");
    } catch (e) {
      notifyError(e);
    }
  };
  const running = useScenarios((s) => (id ? !!s.running[id] : false));
  const live = id ? scenarios.find((s) => s.id === id) : undefined;

  if (state !== "ready") {
    return (
      <div className="page">
        <PageHeader title={creating ? "Новый сценарий" : "Редактирование"} />
        <div className="page-body">
          {state === "loading" ? (
            <div className="ed-loading">
              <span className="spinner" /> Загружаю сценарий…
            </div>
          ) : (
            <Empty icon={<Sparkles />}>
              {template ? "Такого шаблона нет." : "Сценарий не найден — возможно, его удалили."}
              <div style={{ marginTop: 12 }}>
                <Button onClick={() => navigate("commands", "scenarios")}>К сценариям</Button>
              </div>
            </Empty>
          )}
        </div>
      </div>
    );
  }

  const condLabel = conditionsLabel(draft.conditions);
  return (
    <div className="page cmd-editor-page sc-editor-page">
      <PageHeader title={creating ? "Новый сценарий" : "Редактирование"} />
      <div className="ed-bar">
        <button type="button" className="btn ed-bar-btn" onClick={cancel}>
          Отмена
        </button>
        <Button variant="primary" className="ed-bar-btn" loading={saving} onClick={save} title="Сохранить (Ctrl+S)">
          Сохранить
        </Button>
        {id && (
          <Button
            className="ed-bar-btn"
            icon={running ? undefined : <Play />}
            loading={running}
            disabled={dirty}
            title={dirty ? "Сначала сохраните изменения" : "Выполнить сценарий сейчас"}
            onClick={runNow}
          >
            Запустить
          </Button>
        )}
        <span className="spacer" />
        {dirty ? (
          <span className="ed-dirty">{template && creating ? "Шаблон ещё не сохранён" : "Есть несохранённые изменения"}</span>
        ) : (
          live && <span className="ed-dirty">{lastRunLine(live)}</span>
        )}
        {id && (
          <IconButton icon={<Trash2 />} label="Удалить сценарий" variant="ghost" className="sc-ed-del" onClick={() => setDeleting(true)} />
        )}
      </div>

      <div className="page-body sc-ed-body">
        <section className="card sc-ed-head">
          <IconPicker value={draft.icon} onChange={(v) => set("icon", v)} />
          <div className="ed-field sc-ed-name">
            <label className="field-label" htmlFor="sc-name">
              Название
            </label>
            <input
              id="sc-name"
              ref={nameRef}
              className={cx("input", tried && errors.name && "invalid")}
              value={draft.name}
              placeholder="Например: Доброе утро"
              spellCheck={false}
              maxLength={60}
              onChange={(e) => set("name", e.target.value)}
            />
          </div>
          <label className="sc-ed-enabled">
            <span>
              <b>{draft.enabled ? "Включён" : "Выключен"}</b>
              <span className="faint">{draft.enabled ? "Срабатывает сам" : "Только вручную"}</span>
            </span>
            <Toggle checked={draft.enabled} onChange={(v) => set("enabled", v)} label="Сценарий включён" />
          </label>
        </section>

        <div className="sc-ed-grid">
          <section ref={whenRef} className={cx("card sc-ed-section", tried && errors.triggers && "invalid")}>
            <SectionTitle n={1} title="Когда" hint="Любой из триггеров запускает сценарий">
              {draft.triggers.length > 0 && (
                <AddTriggerMenu onAdd={addTrigger} className="btn-sm">
                  <CirclePlus />
                  Триггер
                </AddTriggerMenu>
              )}
            </SectionTitle>
            {draft.triggers.length ? (
              <div className="sc-trig-list">
                {draft.triggers.map((t, i) => (
                  <TriggerRow
                    key={t.key}
                    trigger={t}
                    error={tried ? (errors.triggerErrors[i] ?? "") : ""}
                    autoFocus={t.key === freshTrigger}
                    onChange={changeTrigger}
                    onRemove={() => removeTrigger(t.key)}
                  />
                ))}
              </div>
            ) : (
              <TriggerQuickStart onAdd={addTrigger} />
            )}
          </section>

          <section className="card sc-ed-section sc-ed-cond">
            <SectionTitle n={2} title="Условия" hint="Необязательно" />
            <div className="sc-cond-row">
              <span className="sc-cond-label">Дни недели</span>
              <WeekdayChips
                size="sm"
                value={draft.conditions.weekdays}
                onChange={(weekdays) => set("conditions", { ...draft.conditions, weekdays })}
              />
            </div>
            <div className="sc-cond-row">
              <span className="sc-cond-label">Время</span>
              <span className="sc-cond-time">
                <span className="faint">с</span>
                <TimeInput
                  optional
                  value={draft.conditions.time_from}
                  label="С какого времени"
                  onChange={(time_from) => set("conditions", { ...draft.conditions, time_from })}
                />
                <span className="faint">до</span>
                <TimeInput
                  optional
                  value={draft.conditions.time_to}
                  label="До какого времени"
                  onChange={(time_to) => set("conditions", { ...draft.conditions, time_to })}
                />
                {(draft.conditions.time_from || draft.conditions.time_to) && (
                  <IconButton
                    size="sm"
                    variant="ghost"
                    icon={<X />}
                    label="Без ограничения по времени"
                    onClick={() => set("conditions", { ...draft.conditions, time_from: "", time_to: "" })}
                  />
                )}
              </span>
            </div>
            <div className={cx("sc-cond-note", condLabel && "on")}>
              {condLabel ? `Сработает только: ${condLabel}` : "Без ограничений — в любой день и время"}
            </div>
          </section>
        </div>

        <section ref={doRef} className={cx("card sc-ed-section", tried && (errors.work || errors.values) && "invalid")}>
          <SectionTitle n={3} title="Что сделать" hint="Те же действия, что в командах, — по порядку">
            {draft.actions.length > 0 && (
              <>
                <span className={cx("ed-count", tried && errors.values && "err")}>
                  {countText(draft.actions.length, W_ACTIONS)}
                </span>
                <Button size="sm" icon={<CirclePlus />} onClick={() => addAction()}>
                  Действие
                </Button>
              </>
            )}
          </SectionTitle>
          {draft.actions.length > 0 ? (
            <ActionList
              actions={draft.actions}
              catalog={catalog}
              byType={byType}
              phrases={slots}
              tried={tried}
              autoOpenKey={autoOpenKey}
              onAutoOpened={() => setAutoOpenKey(null)}
              onChange={changeAction}
              onMove={moveAction}
              onRemove={removeAction}
              onDuplicate={duplicateAction}
            />
          ) : (
            <div className="ed-empty sc-do-empty">
              <div className="ed-empty-text">Добавьте действия — или только фразу ниже, и Jarvis просто её скажет.</div>
              <div className="ed-quick">
                {QUICK.map((q) => (
                  <button key={q.type} type="button" className="ed-quick-btn" onClick={() => addAction(q.type, false)}>
                    {q.icon}
                    {q.label}
                  </button>
                ))}
                <button type="button" className="ed-quick-btn" onClick={() => addAction()}>
                  <CirclePlus />
                  Другое…
                </button>
              </div>
            </div>
          )}
          <div className="ed-field sc-say">
            <label className="field-label" htmlFor="sc-say">
              Что сказать после
            </label>
            <input
              id="sc-say"
              className={cx("input", tried && errors.work && "invalid")}
              value={draft.say}
              placeholder="Например: Хорошего дня, сэр! (пусто — молча)"
              spellCheck={false}
              maxLength={300}
              onChange={(e) => set("say", e.target.value)}
            />
          </div>
        </section>
      </div>

      <ConfirmDialog
        open={!!leave}
        title="Выйти без сохранения?"
        confirmText="Выйти"
        cancelText="Остаться"
        danger
        onClose={() => setLeave(null)}
        onConfirm={() => {
          const go = leave;
          setLeave(null);
          baseline.current = snapshot(draft);
          go?.();
        }}
      >
        Изменения в сценарии «{draft.name.trim() || "без названия"}» пропадут.
      </ConfirmDialog>
      <ConfirmDialog
        open={deleting}
        title="Удалить сценарий?"
        confirmText="Удалить"
        danger
        onClose={() => setDeleting(false)}
        onConfirm={removeScenario}
      >
        Сценарий «{source?.name ?? draft.name}» перестанет срабатывать. Это нельзя отменить.
      </ConfirmDialog>
    </div>
  );
}

function SectionTitle({ n, title, hint, children }: { n: number; title: string; hint?: string; children?: ReactNode }) {
  return (
    <div className="sc-sec-title">
      <span className="sc-sec-n">{n}</span>
      <span className="sc-sec-name">{title}</span>
      {hint && <span className="sc-sec-hint">{hint}</span>}
      <span className="spacer" />
      {children}
    </div>
  );
}

function IconPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className="ed-icon-btn sc-ed-icon"
        title="Значок сценария"
        aria-label="Значок сценария"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <ScenarioIcon name={value} size={52} radius={16} />
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} width={300} maxHeight={320} className="icon-pop">
        <div className="icon-pop-head">Значок сценария</div>
        <div className="sc-icon-grid" role="listbox" aria-label="Значки">
          {ICON_KEYS.map((k) => (
            <button
              key={k}
              type="button"
              role="option"
              aria-selected={value === k}
              title={SCENARIO_ICONS[k].label}
              aria-label={SCENARIO_ICONS[k].label}
              className={cx("icon-cell", value === k && "selected")}
              onClick={() => {
                onChange(k);
                setOpen(false);
                btn.current?.focus();
              }}
            >
              <ScenarioIcon name={k} size={34} radius={10} />
            </button>
          ))}
        </div>
      </Popover>
    </>
  );
}
