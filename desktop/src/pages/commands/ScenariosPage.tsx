// «Сценарии»: #/commands/scenarios — список карточек (или витрина шаблонов, пока сценариев нет),
// #/commands/scenarios/new[/<шаблон>] и #/commands/scenarios/<id> — редактор.
import "@/styles/scenarios.css";
import { LayoutTemplate, Pencil, Play, Plus, RotateCw, TriangleAlert, Trash2, X, Zap } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import type { Scenario } from "@/api/types";
import { Button, cx, IconButton, Modal, notify, notifyError, Toggle } from "@/ui";
import { navigate } from "@/lib/router";
import { countText } from "./model";
import { ConfirmDialog } from "./Overlays";
import { ScenarioEditor } from "./ScenarioEditor";
import { iconGlow, ScenarioIcon, TemplateGrid, TriggerChips } from "./ScenarioParts";
import { actionsSummary, runsText, W_SCENARIOS, whenText } from "./scenarioModel";
import { bindScenarioEvents, scenarioMemory, useScenarios } from "./scenarioStore";
import { SectionHeader } from "./SectionHeader";
import { useApps, useCatalog, useCommands } from "./store";

/** Маршруты раздела: route — части после «scenarios». */
export function ScenariosRoute({ route }: { route: string[] }) {
  const [arg, extra] = route;
  if (arg === "new") return <ScenarioEditor key={`new:${extra ?? ""}`} template={extra} />;
  if (arg) return <ScenarioEditor key={arg} id={arg} />;
  return <ScenariosPage />;
}

export function ScenariosPage() {
  const items = useScenarios((s) => s.items);
  const status = useScenarios((s) => s.status);
  const error = useScenarios((s) => s.error);
  const [gallery, setGallery] = useState(false);
  const [deleting, setDeleting] = useState<Scenario | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);
  const [highlight] = useState(() => {
    const id = scenarioMemory.highlight;
    scenarioMemory.highlight = "";
    return id;
  });

  useEffect(() => {
    bindScenarioEvents();
    const st = useScenarios.getState();
    st.load(st.status === "ready");
    st.loadTemplates(st.templatesStatus === "ready");
    useCatalog.getState().load();
    useApps.getState().load();
    if (useCommands.getState().status === "idle") useCommands.getState().load(); // счётчик в заголовке
  }, []);

  useEffect(() => {
    if (!highlight) return;
    requestAnimationFrame(() =>
      document.querySelector(`[data-sc="${highlight}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
    );
  }, [highlight, items.length]);

  const enabled = items.filter((s) => s.enabled).length;
  const empty = status === "ready" && items.length === 0;

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusyDelete(true);
    try {
      await useScenarios.getState().remove(deleting.id);
      notify(`Сценарий «${deleting.name}» удалён`, "ok");
      setDeleting(null);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusyDelete(false);
    }
  };

  return (
    <div className="page">
      <SectionHeader
        tab="scenarios"
        actions={
          <>
            {!empty && (
              <Button icon={<LayoutTemplate />} onClick={() => setGallery(true)}>
                Шаблоны
              </Button>
            )}
            <Button variant="primary" icon={<Plus />} onClick={() => navigate("commands", "scenarios", "new")}>
              Новый сценарий
            </Button>
          </>
        }
      />
      <div className="page-body sc-body">
        {status === "error" && !items.length ? (
          <div className="cmd-state">
            <TriangleAlert />
            <div className="cmd-state-title">Не удалось загрузить сценарии</div>
            <div className="faint small">{error}</div>
            <Button size="sm" icon={<RotateCw />} onClick={() => useScenarios.getState().load()}>
              Повторить
            </Button>
          </div>
        ) : status !== "ready" && !items.length ? (
          <div className="sc-grid">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="sc-card skeleton" style={{ animationDelay: `${i * 60}ms` }} />
            ))}
          </div>
        ) : empty ? (
          <EmptyGallery />
        ) : (
          <>
            <div className="sc-summary">
              <span className="chip accent">{countText(items.length, W_SCENARIOS)}</span>
              <span className="faint small">
                {enabled === items.length ? "все включены" : `включено ${enabled} из ${items.length}`} · Jarvis
                следит за триггерами сам, даже когда окно закрыто
              </span>
            </div>
            <div className="sc-grid">
              {items.map((s, i) => (
                <ScenarioCard
                  key={s.id}
                  item={s}
                  index={i}
                  highlight={s.id === highlight}
                  onDelete={() => setDeleting(s)}
                />
              ))}
              <button
                type="button"
                className="sc-card sc-card-add"
                onClick={() => navigate("commands", "scenarios", "new")}
              >
                <span className="sc-add-plus">
                  <Plus />
                </span>
                <span className="sc-add-text">
                  <b>Новый сценарий</b>
                  <span>Когда что-то происходит — Jarvis делает дела сам</span>
                </span>
              </button>
            </div>
          </>
        )}
      </div>

      <Modal
        open={gallery}
        onClose={() => setGallery(false)}
        title="Шаблоны сценариев"
        icon={<LayoutTemplate style={{ width: 18, height: 18, color: "var(--accent-text)" }} />}
        width={980}
      >
        <p className="sc-modal-lead">
          Готовые сценарии на каждый день. «Добавить» — сразу включить, «Настроить» — открыть в редакторе.
        </p>
        <TemplateGrid compact onAdded={() => setGallery(false)} />
      </Modal>

      <ConfirmDialog
        open={!!deleting}
        title="Удалить сценарий?"
        confirmText="Удалить"
        danger
        busy={busyDelete}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
      >
        Сценарий «{deleting?.name}» перестанет срабатывать. Это нельзя отменить.
      </ConfirmDialog>
    </div>
  );
}

// --- карточка сценария ----------------------------------------------------------------------------------
function ScenarioCard({
  item,
  index,
  highlight,
  onDelete,
}: {
  item: Scenario;
  index: number;
  highlight: boolean;
  onDelete: () => void;
}) {
  const byType = useCatalog((s) => s.byType);
  const apps = useApps((s) => s.apps);
  const running = useScenarios((s) => !!s.running[item.id]);
  const lastEvent = useScenarios((s) => s.lastEvent[item.id]);
  const [flash, setFlash] = useState(false);
  const summary = useMemo(() => actionsSummary(item.actions, item.say, byType, apps), [item, byType, apps]);

  useEffect(() => {
    if (!lastEvent || Date.now() - lastEvent.at > 4000) return;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 1600);
    return () => clearTimeout(t);
  }, [lastEvent]);

  const run = async () => {
    try {
      const res = await useScenarios.getState().run(item.id);
      if (res?.data?.cancelled) notify(`${res.title} — отменено`, "info");
      else if (res?.ok === false) notify(`${res.title} — ${res.message || "не получилось"}`, "error");
      else notify(`Сценарий «${item.name}» — выполнено ✓`, "ok");
    } catch (e) {
      notifyError(e);
    }
  };
  const toggle = (v: boolean) =>
    useScenarios
      .getState()
      .setEnabled(item.id, v)
      .then(() => notify(v ? `Сценарий «${item.name}» включён` : `Сценарий «${item.name}» выключен`, "info"))
      .catch(notifyError);
  const edit = () => navigate("commands", "scenarios", item.id);

  return (
    <article
      data-sc={item.id}
      className={cx(
        "sc-card",
        !item.enabled && "off",
        highlight && "flash",
        flash && (lastEvent?.ok === false ? "ran-err" : "ran"),
      )}
      style={{ ...iconGlow(item.icon), animationDelay: `${Math.min(index, 10) * 40}ms` } as CSSProperties}
    >
      <div className="sc-card-top">
        <button type="button" className="sc-card-hit" onClick={edit} title="Открыть сценарий">
          <ScenarioIcon name={item.icon} size={46} radius={14} />
          <span className="sc-card-text">
            <span className="sc-card-name ellipsis">{item.name}</span>
            <TriggerChips triggers={item.triggers} conditions={item.conditions} />
          </span>
        </button>
        <Toggle
          checked={item.enabled}
          onChange={toggle}
          label={item.enabled ? `Выключить «${item.name}»` : `Включить «${item.name}»`}
        />
      </div>
      <div className="sc-card-foot">
        <span className="sc-card-do" title={item.actions.length ? "Что сделает" : "Что скажет"}>
          <Zap />
          <span className="ellipsis">{summary.text}</span>
          {summary.more > 0 && <span className="cmd-more-n">+{summary.more}</span>}
        </span>
        <LastRun item={item} />
        <span className="sc-card-actions">
          <button
            type="button"
            className="btn btn-sm btn-ghost cmd-run"
            disabled={running}
            onClick={run}
            title={`Запустить «${item.name}» сейчас`}
          >
            {running ? <span className="spinner" /> : <Play />}
            Запустить
          </button>
          <IconButton size="sm" variant="ghost" icon={<Pencil />} label="Изменить" onClick={edit} />
          <IconButton size="sm" variant="ghost" icon={<Trash2 />} label="Удалить" onClick={onDelete} />
        </span>
      </div>
    </article>
  );
}

function LastRun({ item }: { item: Scenario }) {
  if (!item.last_run) return <span className="sc-last never">ещё не запускался</span>;
  const ok = item.last_ok !== false;
  const title = ok
    ? `Последний запуск: ${whenText(item.last_run)} · всего ${runsText(item.runs)}`
    : `Не получилось: ${item.last_error || "ошибка"}`;
  return (
    <span className={cx("sc-last", ok ? "ok" : "err")} title={title}>
      {whenText(item.last_run)} {ok ? <span className="sc-last-mark">✓</span> : <X className="sc-last-x" />}
    </span>
  );
}

// --- пустой раздел: витрина шаблонов ----------------------------------------------------------------------
function EmptyGallery() {
  return (
    <div className="sc-empty">
      <section className="sc-hero">
        <div className="sc-hero-orbit" aria-hidden>
          {["sunrise", "gamepad", "eye", "door", "sunset", "wave", "battery", "calendar", "music"].map((k, i) => (
            <span key={k} className="sc-hero-tile" style={{ "--i": i } as CSSProperties}>
              <ScenarioIcon name={k} size={i % 3 === 0 ? 56 : 46} />
            </span>
          ))}
        </div>
        <div className="sc-hero-body">
          <div className="eyebrow">Автоматизация</div>
          <h2 className="sc-hero-title">Сценарии — Jarvis делает дела сам</h2>
          <p className="sc-hero-lead">
            По расписанию, по фразе или когда что-то происходит на компьютере: открылась программа, вы вернулись,
            садится аккумулятор, скоро встреча.
          </p>
          <div className="sc-hero-actions">
            <Button variant="primary" icon={<Plus />} onClick={() => navigate("commands", "scenarios", "new")}>
              Создать с нуля
            </Button>
            <span className="faint small">или начните с шаблона ниже</span>
          </div>
        </div>
      </section>
      <div className="sc-section-title">
        <LayoutTemplate />
        Готовые шаблоны
      </div>
      <TemplateGrid />
    </div>
  );
}
