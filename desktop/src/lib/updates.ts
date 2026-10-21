// Обновления Jarvis (electron-updater в главном процессе): состояние для настроек, трея и значка в меню.
import { create } from "zustand";
import { bridge, isElectron } from "@/lib/bridge";

export type UpdateStatus = "disabled" | "idle" | "checking" | "none" | "available" | "downloading" | "downloaded" | "error";

export interface UpdateState {
  status: UpdateStatus;
  current: string;
  version?: string;
  notes?: string;
  date?: string;
  percent?: number;
  transferred?: number;
  total?: number;
  speed?: number;
  error?: string;
  checkedAt?: number;
  /** false — на GitHub ещё нет ни одного выпуска. */
  published?: boolean;
}

export const useUpdates = create<UpdateState>(() => ({ status: "disabled", current: bridge.version }));

let bound = false;

/** Подписка на состояние обновлений (один раз на окно). */
export function bindUpdates() {
  if (bound || !isElectron) return;
  bound = true;
  void bridge.updateState?.().then((state) => state && useUpdates.setState(state, true));
  bridge.on("update:state", (state) => useUpdates.setState(state as UpdateState, true));
}

export const RELEASES_URL = "https://github.com/stonebridgeway/Jarvis-Agent/releases";

export const updates = {
  check: () => bridge.send("update:check"),
  download: () => bridge.send("update:download"),
  install: () => bridge.send("update:install"),
};
