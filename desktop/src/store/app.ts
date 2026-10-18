// Общее состояние интерфейса: связь с ядром, настройки, голос, история, подтверждения.
import { create } from "zustand";
import { api } from "@/api/client";
import { onConnection, onEvent } from "@/api/events";
import type {
  Confirmation,
  DeepPartial,
  HistoryItem,
  SecretName,
  SecretsStatus,
  Settings,
  VoiceState,
} from "@/api/types";
import { applyTheme } from "@/lib/theme";

interface Partial {
  text: string;
  wake: boolean;
  final?: boolean;
  at: number;
}

interface AppState {
  connected: boolean;
  ready: boolean;
  version: string;
  platform: string;
  settings: Settings | null;
  secrets: SecretsStatus;
  voiceState: VoiceState;
  voiceReason: string;
  aiConfigured: boolean;
  history: HistoryItem[];
  confirmations: Confirmation[];
  partial: Partial | null;
  patchSettings: (patch: DeepPartial<Settings>) => Promise<void>;
  setSecret: (name: SecretName, value: string) => Promise<void>;
  loadHistory: () => Promise<void>;
  resolveConfirmation: (id: string, allow: boolean) => Promise<void>;
}

function merge<T>(base: T, patch: unknown): T {
  if (typeof patch !== "object" || patch === null || Array.isArray(patch)) return patch as T;
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [k, v] of Object.entries(patch as Record<string, unknown>)) {
    out[k] = typeof v === "object" && v !== null && !Array.isArray(v) ? merge(out[k], v) : v;
  }
  return out as T;
}

export const useApp = create<AppState>((set, get) => ({
  connected: false,
  ready: false,
  version: "",
  platform: "",
  settings: null,
  secrets: {},
  voiceState: "off",
  voiceReason: "",
  aiConfigured: false,
  history: [],
  confirmations: [],
  partial: null,

  async patchSettings(patch) {
    const prev = get().settings;
    if (prev) {
      const optimistic = merge(prev, patch);
      set({ settings: optimistic });
      applyTheme(optimistic.interface);
    }
    try {
      const res = await api.patch<{ settings: Settings }>("/api/settings", patch);
      set({ settings: res.settings });
      applyTheme(res.settings.interface);
    } catch (e) {
      if (prev) {
        set({ settings: prev });
        applyTheme(prev.interface);
      }
      throw e;
    }
  },

  async setSecret(name, value) {
    const res = await api.put<{ secrets: SecretsStatus }>("/api/secrets", { name, value });
    set({ secrets: res.secrets });
  },

  async loadHistory() {
    const res = await api.get<{ items: HistoryItem[] }>("/api/history?limit=60");
    set({ history: res.items });
  },

  async resolveConfirmation(id, allow) {
    set({ confirmations: get().confirmations.filter((c) => c.id !== id) });
    try {
      await api.post(`/api/confirm/${id}`, { allow });
    } catch {
      /* подтверждение уже истекло */
    }
  },
}));

let bound = false;

export function bindAppEvents() {
  if (bound) return;
  bound = true;
  onConnection((connected) => useApp.setState({ connected }));

  onEvent<{
    version: string;
    platform: string;
    settings: Settings;
    secrets: SecretsStatus;
    voice: { state: VoiceState };
    ai: { configured: boolean };
  }>("hello", (e) => {
    const d = e.data;
    useApp.setState({
      ready: true,
      version: d.version,
      platform: d.platform,
      settings: d.settings,
      secrets: d.secrets,
      voiceState: d.voice?.state ?? "off",
      aiConfigured: !!d.ai?.configured,
    });
    applyTheme(d.settings.interface);
    useApp.getState().loadHistory().catch(() => undefined);
    api
      .get<{ items: Confirmation[] }>("/api/confirm")
      .then((r) => useApp.setState({ confirmations: r.items }))
      .catch(() => undefined);
  });

  onEvent<{ settings: Settings }>("settings.changed", (e) => {
    useApp.setState({ settings: e.data.settings });
    applyTheme(e.data.settings.interface);
  });

  onEvent<{ name: SecretName; set: boolean }>("secrets.changed", (e) => {
    useApp.setState({ secrets: { ...useApp.getState().secrets, [e.data.name]: e.data.set } });
  });

  onEvent<{ state: VoiceState; reason?: string }>("voice.state", (e) => {
    useApp.setState({ voiceState: e.data.state, voiceReason: e.data.reason ?? "" });
  });

  onEvent<{ configured: boolean }>("ai.configured", (e) => useApp.setState({ aiConfigured: !!e.data.configured }));

  onEvent<{ item: HistoryItem }>("history.added", (e) => {
    useApp.setState({ history: [e.data.item, ...useApp.getState().history].slice(0, 100) });
  });

  onEvent<{ item: HistoryItem }>("history.updated", (e) => {
    useApp.setState({
      history: useApp.getState().history.map((h) => (h.id === e.data.item.id ? e.data.item : h)),
    });
  });

  onEvent("history.cleared", () => useApp.setState({ history: [] }));

  onEvent<Confirmation>("confirm.request", (e) => {
    useApp.setState({ confirmations: [...useApp.getState().confirmations, e.data] });
  });

  onEvent<{ id: string }>("confirm.resolved", (e) => {
    useApp.setState({ confirmations: useApp.getState().confirmations.filter((c) => c.id !== e.data.id) });
  });

  onEvent<{ text: string; wake: boolean }>("voice.partial", (e) => {
    useApp.setState({ partial: { text: e.data.text, wake: e.data.wake, at: Date.now() } });
  });

  onEvent<{ text: string; wake: boolean }>("voice.final", (e) => {
    useApp.setState({ partial: { text: e.data.text, wake: e.data.wake, final: true, at: Date.now() } });
  });
}
