// Плагины: список с ядра (GET /api/plugins), живые обновления (plugins.changed) и последние события
// plugin.<id>.<имя> — общие для страницы «Расширения» и виджета «Плагины».
import { create } from "zustand";
import { api } from "@/api/client";
import { onConnection, onEvent } from "@/api/events";
import type { Plugin } from "./pluginKit";
import { sortPlugins } from "./pluginKit";

type LiveEvents = Record<string, Record<string, { data: Record<string, unknown>; ts: number }>>;

interface PluginsState {
  items: Plugin[] | null;
  error: string;
  loading: boolean;
  live: LiveEvents;
  load: () => Promise<void>;
  update: (id: string, patch: { enabled?: boolean; config?: Record<string, unknown> }) => Promise<Plugin>;
  action: (id: string, action: string, args?: Record<string, unknown>) => Promise<Record<string, unknown>>;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export const usePlugins = create<PluginsState>((set, get) => ({
  items: null,
  error: "",
  loading: false,
  live: {},

  async load() {
    set({ loading: true });
    try {
      const res = await api.get<{ items: Plugin[] }>("/api/plugins");
      set({ items: sortPlugins(res.items ?? []), error: "", loading: false });
    } catch (e) {
      set({ error: message(e), loading: false });
    }
  },

  async update(id, patch) {
    const prev = get().items;
    if (prev && patch.enabled !== undefined) {
      // переключатель отзывается сразу; ответ ядра всё равно придёт
      set({ items: prev.map((p) => (p.id === id ? { ...p, enabled: !!patch.enabled } : p)) });
    }
    try {
      const plugin = await api.put<Plugin>(`/api/plugins/${encodeURIComponent(id)}`, patch);
      const items = get().items;
      if (plugin && plugin.id && items) set({ items: items.map((p) => (p.id === id ? plugin : p)) });
      return plugin;
    } catch (e) {
      if (prev && patch.enabled !== undefined) {
        const before = prev.find((p) => p.id === id);
        set({ items: (get().items ?? prev).map((p) => (p.id === id && before ? { ...p, enabled: before.enabled } : p)) });
      }
      throw e;
    }
  },

  async action(id, action, args = {}) {
    return api.post<Record<string, unknown>>(`/api/plugins/${encodeURIComponent(id)}/action`, { action, args });
  },
}));

let bound = false;

/** Подписаться на события плагинов (один раз на окно) и загрузить список. */
export function bindPlugins() {
  if (bound) {
    if (!usePlugins.getState().items && !usePlugins.getState().loading) usePlugins.getState().load();
    return;
  }
  bound = true;
  usePlugins.getState().load();
  onEvent<{ items?: Plugin[] }>("plugins.changed", (e) => {
    if (Array.isArray(e.data?.items)) usePlugins.setState({ items: sortPlugins(e.data.items), error: "" });
    else usePlugins.getState().load();
  });
  onEvent<Record<string, unknown>>("plugin.*", (e) => {
    const [, id, ...rest] = e.type.split(".");
    const name = rest.join(".");
    if (!id || !name) return;
    const live = usePlugins.getState().live;
    usePlugins.setState({
      live: { ...live, [id]: { ...(live[id] ?? {}), [name]: { data: e.data ?? {}, ts: Date.now() } } },
    });
  });
  let wasOnline = true;
  onConnection((online) => {
    if (online && !wasOnline) usePlugins.getState().load();
    wasOnline = online;
  });
}
