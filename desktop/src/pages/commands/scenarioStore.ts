// Данные «Сценариев»: список, шаблоны, запуски. Живые обновления — события scenarios.changed и scenario.run.
import { useEffect } from "react";
import { create } from "zustand";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { ExecResult, Scenario, ScenarioRunEvent, ScenarioTemplate } from "@/api/types";

type Status = "idle" | "loading" | "ready" | "error";

/** id сценария, который подсветить в списке (после сохранения или добавления шаблона). */
export const scenarioMemory = { highlight: "" };

function errText(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) return "Ядро пока не умеет работать со сценариями (нет маршрута).";
  return e instanceof Error ? e.message : String(e);
}

interface ScenariosState {
  items: Scenario[];
  status: Status;
  error: string;
  templates: ScenarioTemplate[];
  templatesStatus: Status;
  /** сценарии, запущенные кнопкой и ещё не завершившиеся */
  running: Record<string, boolean>;
  /** последний запуск по событию scenario.run: id → {ok, ts} — для вспышки карточки */
  lastEvent: Record<string, { ok: boolean; at: number }>;
  load: (silent?: boolean) => Promise<void>;
  loadTemplates: (silent?: boolean) => Promise<void>;
  upsert: (item: Scenario) => void;
  drop: (id: string) => void;
  setEnabled: (id: string, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  run: (id: string) => Promise<ExecResult>;
  fromTemplate: (templateId: string) => Promise<Scenario>;
}

export const useScenarios = create<ScenariosState>((set, get) => ({
  items: [],
  status: "idle",
  error: "",
  templates: [],
  templatesStatus: "idle",
  running: {},
  lastEvent: {},

  async load(silent = false) {
    if (!silent || get().status === "idle") set({ status: get().items.length ? get().status : "loading" });
    try {
      const res = await api.get<{ items: Scenario[] }>("/api/scenarios");
      set({ items: Array.isArray(res?.items) ? res.items : [], status: "ready", error: "" });
    } catch (e) {
      if (silent && get().status === "ready") return;
      set({ status: "error", error: errText(e) });
    }
  },

  async loadTemplates(silent = false) {
    if (!silent) set({ templatesStatus: get().templates.length ? get().templatesStatus : "loading" });
    try {
      const res = await api.get<{ items: ScenarioTemplate[] }>("/api/scenarios/templates");
      set({ templates: Array.isArray(res?.items) ? res.items : [], templatesStatus: "ready" });
    } catch {
      if (!(silent && get().templatesStatus === "ready")) set({ templatesStatus: "error" });
    }
  },

  upsert(item) {
    const items = get().items;
    const i = items.findIndex((s) => s.id === item.id);
    set({ items: i >= 0 ? items.map((s) => (s.id === item.id ? item : s)) : [...items, item] });
  },

  drop(id) {
    set({ items: get().items.filter((s) => s.id !== id) });
  },

  async setEnabled(id, enabled) {
    const prev = get().items.find((s) => s.id === id);
    if (prev) get().upsert({ ...prev, enabled });
    try {
      get().upsert(await api.patch<Scenario>(`/api/scenarios/${encodeURIComponent(id)}`, { enabled }));
    } catch (e) {
      if (prev) get().upsert(prev);
      throw e;
    }
  },

  async remove(id) {
    await api.del(`/api/scenarios/${encodeURIComponent(id)}`);
    get().drop(id);
    get().loadTemplates(true);
  },

  async run(id) {
    set({ running: { ...get().running, [id]: true } });
    try {
      return await api.post<ExecResult>(`/api/scenarios/${encodeURIComponent(id)}/run`);
    } finally {
      const { [id]: _done, ...rest } = get().running;
      set({ running: rest });
    }
  },

  async fromTemplate(templateId) {
    const item = await api.post<Scenario>("/api/scenarios/from-template", { template_id: templateId });
    get().upsert(item);
    get().loadTemplates(true);
    return item;
  },
}));

let bound = false;

/** Подписка на живые изменения (один раз на окно). */
export function bindScenarioEvents() {
  if (bound) return;
  bound = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const reload = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const st = useScenarios.getState();
      if (st.status !== "idle") st.load(true);
      if (st.templatesStatus !== "idle") st.loadTemplates(true);
    }, 250);
  };
  onEvent<{ op: string; item?: Scenario; id?: string }>("scenarios.changed", (e) => {
    const st = useScenarios.getState();
    if (e.data.op === "delete" && e.data.id) st.drop(e.data.id);
    else if (e.data.item) st.upsert(e.data.item);
    else reload();
    if (e.data.op !== "update") st.loadTemplates(true);
  });
  onEvent<ScenarioRunEvent>("scenario.run", (e) => {
    if (!e.data?.id) return;
    const st = useScenarios.getState();
    useScenarios.setState({ lastEvent: { ...st.lastEvent, [e.data.id]: { ok: e.data.ok, at: Date.now() } } });
  });
  // после переподключения к ядру данные могли устареть
  onEvent("hello", reload);
}

/** Сколько сценариев (для вкладки раздела) — список подгружается тихо один раз. */
export function useScenarioCount(): number {
  const count = useScenarios((s) => s.items.length);
  useEffect(() => {
    bindScenarioEvents();
    if (useScenarios.getState().status === "idle") useScenarios.getState().load(true);
  }, []);
  return count;
}
