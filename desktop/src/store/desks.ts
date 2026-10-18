// Столы (страницы с виджетами): загрузка, выбор, редактирование и сохранение в ядре.
import { create } from "zustand";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { Desk, DeskPreset, WidgetPlacement } from "@/api/types";
import { navigate } from "@/lib/router";

export const DEFAULT_DESK: Desk = {
  id: "main",
  name: "Главная",
  icon: "home",
  preset: "main",
  widgets: [
    { id: "w-history", type: "history", x: 0, y: 0, w: 2, h: 2, config: {} },
    { id: "w-avatar", type: "avatar", x: 2, y: 0, w: 2, h: 3, config: {} },
    { id: "w-control", type: "control", x: 0, y: 2, w: 2, h: 2, config: {} },
    { id: "w-media", type: "media", x: 2, y: 3, w: 2, h: 1, config: {} },
    { id: "w-weather", type: "weather", x: 0, y: 4, w: 1, h: 1, config: {} },
    { id: "w-tasks", type: "tasks", x: 1, y: 4, w: 1, h: 2, config: {} },
    { id: "w-rates", type: "rates", x: 2, y: 4, w: 1, h: 2, config: {} },
    { id: "w-clock", type: "clock", x: 3, y: 4, w: 1, h: 1, config: {} },
    { id: "w-system", type: "system", x: 0, y: 5, w: 1, h: 1, config: {} },
    { id: "w-calendar", type: "calendar", x: 3, y: 5, w: 1, h: 1, config: {} },
  ],
};

interface DesksState {
  desks: Desk[];
  current: string;
  editing: boolean;
  loaded: boolean;
  offline: boolean;
  presets: DeskPreset[];
  load: () => Promise<void>;
  select: (id: string) => void;
  setEditing: (v: boolean) => void;
  setWidgets: (deskId: string, widgets: WidgetPlacement[]) => void;
  createDesk: (name: string, preset: string) => Promise<void>;
  renameDesk: (id: string, name: string) => Promise<void>;
  deleteDesk: (id: string) => Promise<void>;
  loadPresets: () => Promise<void>;
}

let saveTimer: ReturnType<typeof setTimeout> | undefined;

/** Один стол — один раз: событие desks.changed и ответ на создание приходят в любом порядке. */
function uniqueDesks(desks: Desk[]): Desk[] {
  const seen = new Set<string>();
  return desks.filter((d) => !seen.has(d.id) && (seen.add(d.id), true));
}

function persist(desks: Desk[]) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api.put("/api/desks", { items: desks }).catch(() => useDesks.setState({ offline: true }));
  }, 500);
}

export const useDesks = create<DesksState>((set, get) => ({
  desks: [DEFAULT_DESK],
  current: DEFAULT_DESK.id,
  editing: false,
  loaded: false,
  offline: false,
  presets: [],

  async load() {
    try {
      const res = await api.get<{ items: Desk[] }>("/api/desks");
      const desks = res.items?.length ? uniqueDesks(res.items) : [DEFAULT_DESK];
      const current = desks.some((d) => d.id === get().current) ? get().current : desks[0].id;
      set({ desks, current, loaded: true, offline: false });
    } catch {
      set({ loaded: true, offline: true });
    }
  },

  select(id) {
    set({ current: id });
  },

  setEditing(v) {
    set({ editing: v });
  },

  setWidgets(deskId, widgets) {
    const desks = get().desks.map((d) => (d.id === deskId ? { ...d, widgets } : d));
    set({ desks });
    persist(desks);
  },

  async createDesk(name, preset) {
    const desk = await api.post<Desk>("/api/desks", { name, preset });
    // desks.changed с новым столом мог прийти раньше ответа — тогда стол уже в списке, второй раз не добавляем
    const desks = get().desks.some((d) => d.id === desk.id) ? get().desks : [...get().desks, desk];
    set({ desks, current: desk.id });
  },

  async renameDesk(id, name) {
    const desk = await api.patch<Desk>(`/api/desks/${id}`, { name });
    set({ desks: get().desks.map((d) => (d.id === id ? desk : d)) });
  },

  async deleteDesk(id) {
    await api.del(`/api/desks/${id}`);
    const desks = get().desks.filter((d) => d.id !== id);
    set({ desks, current: desks[0]?.id ?? DEFAULT_DESK.id });
  },

  async loadPresets() {
    if (get().presets.length) return;
    try {
      const res = await api.get<{ items: DeskPreset[] }>("/api/desks/presets");
      set({ presets: res.items });
    } catch {
      /* без ядра заготовок нет */
    }
  },
}));

let bound = false;
export function bindDeskEvents() {
  if (bound) return;
  bound = true;
  onEvent<{ items: Desk[] }>("desks.changed", (e) => {
    if (e.data.items?.length) useDesks.setState({ desks: uniqueDesks(e.data.items) });
    else useDesks.getState().load();
  });
  onEvent<{ id: string }>("desk.open", (e) => {
    useDesks.setState({ current: e.data.id });
    navigate("home");
  });
}
