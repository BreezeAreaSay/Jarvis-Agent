// Данные раздела «Команды»: список команд, каталог действий, программы, паки. Живые обновления — события
// commands.changed и packs.changed.
import { useMemo } from "react";
import { create } from "zustand";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { ActionType, AppEntry, CommandInfo, Pack } from "@/api/types";
import { appLabel, type Category, FALLBACK_ACTIONS } from "./model";

type Status = "idle" | "loading" | "ready" | "error";

/** Сколько строк списка показывать за раз. */
export const PAGE = 100;

/** Состояние списка между переходами (список → редактор → список). */
export const listMemory = {
  category: "all",
  query: "",
  state: "all" as "all" | "on" | "off",
  shown: PAGE,
  scrollTop: 0,
  /** id команды, которую подсветить после сохранения */
  highlight: "",
};

function errText(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) return "Ядро пока не умеет работать с командами (нет маршрута).";
  return e instanceof Error ? e.message : String(e);
}

// --- команды -----------------------------------------------------------------------------------------
interface CommandsState {
  items: CommandInfo[];
  categories: Category[];
  total: number;
  status: Status;
  error: string;
  load: (silent?: boolean) => Promise<void>;
  setEnabled: (id: string, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  upsert: (cmd: CommandInfo) => void;
}

export const useCommands = create<CommandsState>((set, get) => ({
  items: [],
  categories: [],
  total: 0,
  status: "idle",
  error: "",

  async load(silent = false) {
    if (!silent || get().status === "idle") set({ status: get().items.length ? get().status : "loading" });
    try {
      const res = await api.get<{ items: CommandInfo[]; categories?: Category[]; total?: number }>("/api/commands");
      const items = Array.isArray(res?.items) ? res.items : [];
      set({
        items,
        categories: Array.isArray(res?.categories) ? res.categories : [],
        total: typeof res?.total === "number" ? res.total : items.length,
        status: "ready",
        error: "",
      });
    } catch (e) {
      if (silent && get().status === "ready") return;
      set({ status: "error", error: errText(e) });
    }
  },

  async setEnabled(id, enabled) {
    const prev = get().items;
    set({ items: prev.map((c) => (c.id === id ? { ...c, enabled } : c)) });
    try {
      const res = await api.patch<CommandInfo>(`/api/commands/${encodeURIComponent(id)}`, { enabled });
      if (res && typeof res === "object" && "id" in res) get().upsert(res);
    } catch (e) {
      set({ items: get().items.map((c) => (c.id === id ? { ...c, enabled: !enabled } : c)) });
      throw e;
    }
  },

  async remove(id) {
    await api.del(`/api/commands/${encodeURIComponent(id)}`);
    set({ items: get().items.filter((c) => c.id !== id), total: Math.max(0, get().total - 1) });
  },

  upsert(cmd) {
    const items = get().items;
    const i = items.findIndex((c) => c.id === cmd.id);
    if (i >= 0) set({ items: items.map((c) => (c.id === cmd.id ? cmd : c)) });
    else set({ items: [...items, cmd], total: get().total + 1 });
  },
}));

// --- каталог действий -----------------------------------------------------------------------------------
interface CatalogState {
  items: ActionType[];
  byType: Map<string, ActionType>;
  fromServer: boolean;
  status: Status;
  load: () => Promise<void>;
}

function indexCatalog(items: ActionType[]): Map<string, ActionType> {
  return new Map(items.map((it) => [it.type, it]));
}

export const useCatalog = create<CatalogState>((set, get) => ({
  items: FALLBACK_ACTIONS,
  byType: indexCatalog(FALLBACK_ACTIONS),
  fromServer: false,
  status: "idle",
  async load() {
    if (get().status === "loading" || get().fromServer) return;
    set({ status: "loading" });
    try {
      const res = await api.get<{ items: ActionType[] }>("/api/commands/actions");
      const items = (Array.isArray(res?.items) ? res.items : []).map((it) => ({
        ...it,
        params: Array.isArray(it.params) ? it.params : [],
        category: it.category || "Другое",
        title: it.title || it.type,
      }));
      if (items.length) set({ items, byType: indexCatalog(items), fromServer: true, status: "ready" });
      else set({ status: "ready" });
    } catch {
      set({ status: "error" });
    }
  },
}));

// --- программы из «Пуска» ---------------------------------------------------------------------------------
interface AppsState {
  apps: AppEntry[];
  status: Status;
  load: (refresh?: boolean) => Promise<void>;
}

export const useApps = create<AppsState>((set, get) => ({
  apps: [],
  status: "idle",
  async load(refresh = false) {
    if (get().status === "loading" || (get().status === "ready" && !refresh)) return;
    set({ status: "loading" });
    try {
      const res = await api.get<{ items: AppEntry[] }>(`/api/apps?refresh=${refresh ? 1 : 0}`);
      const apps = (Array.isArray(res?.items) ? res.items : []).slice().sort((x, y) => x.name.localeCompare(y.name, "ru"));
      set({ apps, status: "ready" });
    } catch {
      set({ status: "error" });
    }
  },
}));

// --- паки -------------------------------------------------------------------------------------------
interface PacksState {
  items: Pack[];
  totals: { packs: number; commands: number };
  status: Status;
  error: string;
  busy: Record<string, "install" | "remove">;
  load: (silent?: boolean) => Promise<void>;
  install: (id: string) => Promise<Pack | null>;
  remove: (id: string) => Promise<Pack | null>;
}

function patchPack(items: Pack[], pack: Pack | null, id: string, installed: boolean): Pack[] {
  return items.map((p) => (p.id === id ? (pack && pack.id === id ? { ...p, ...pack } : { ...p, installed }) : p));
}

export const usePacks = create<PacksState>((set, get) => ({
  items: [],
  totals: { packs: 0, commands: 0 },
  status: "idle",
  error: "",
  busy: {},

  async load(silent = false) {
    if (!silent) set({ status: get().items.length ? get().status : "loading" });
    try {
      const res = await api.get<{ items: Pack[]; totals?: { packs: number; commands: number } }>("/api/packs");
      const items = Array.isArray(res?.items) ? res.items : [];
      set({
        items,
        totals: res?.totals ?? {
          packs: items.length,
          commands: items.reduce((n, p) => n + (p.commands_count || p.commands?.length || 0), 0),
        },
        status: "ready",
        error: "",
      });
    } catch (e) {
      if (silent && get().status === "ready") return;
      set({ status: "error", error: errText(e) });
    }
  },

  async install(id) {
    set({ busy: { ...get().busy, [id]: "install" } });
    try {
      let pack: Pack | null;
      try {
        pack = await api.post<Pack>(`/api/packs/${encodeURIComponent(id)}/install`);
      } catch (e) {
        // запасной путь: POST /api/packs/{id}
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
          pack = await api.post<Pack>(`/api/packs/${encodeURIComponent(id)}`);
        } else throw e;
      }
      set({ items: patchPack(get().items, pack, id, true) });
      return pack;
    } finally {
      const { [id]: _done, ...rest } = get().busy;
      set({ busy: rest });
    }
  },

  async remove(id) {
    set({ busy: { ...get().busy, [id]: "remove" } });
    try {
      const pack = await api.del<Pack>(`/api/packs/${encodeURIComponent(id)}`);
      set({ items: patchPack(get().items, pack, id, false) });
      return pack;
    } finally {
      const { [id]: _done, ...rest } = get().busy;
      set({ busy: rest });
    }
  },
}));

// --- события ------------------------------------------------------------------------------------------
let bound = false;

/** Подписка на живые изменения (один раз на окно). */
export function bindCommandEvents() {
  if (bound) return;
  bound = true;
  let tCmd: ReturnType<typeof setTimeout> | undefined;
  let tPacks: ReturnType<typeof setTimeout> | undefined;
  const reloadCommands = () => {
    clearTimeout(tCmd);
    tCmd = setTimeout(() => {
      if (useCommands.getState().status !== "idle") useCommands.getState().load(true);
    }, 250);
  };
  const reloadPacks = () => {
    clearTimeout(tPacks);
    tPacks = setTimeout(() => {
      if (usePacks.getState().status !== "idle") usePacks.getState().load(true);
    }, 250);
  };
  onEvent("commands.changed", reloadCommands);
  onEvent("packs.changed", () => {
    reloadPacks();
    reloadCommands();
  });
  // после переподключения к ядру данные могли устареть
  onEvent("hello", () => {
    reloadCommands();
    reloadPacks();
  });
}

/** Имена паков по id (для бейджей «Пак «Spotify»»). */
export function usePackNames(): Map<string, string> {
  const items = usePacks((s) => s.items);
  return useMemo(() => new Map(items.map((p) => [p.id, p.name || appLabel(p.app)])), [items]);
}
