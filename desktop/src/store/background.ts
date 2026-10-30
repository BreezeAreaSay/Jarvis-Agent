// Фоновые задачи: список карточек из ядра (/api/background) и события background.* — для страницы
// «Фоновые задачи» и точки на её значке в боковой панели (есть вопрос, ждущий ответа).
import { create } from "zustand";
import { api } from "@/api/client";
import { onConnection, onEvent } from "@/api/events";

export type TaskStatus =
  | "queued"
  | "running"
  | "waiting"
  | "done"
  | "partial"
  | "failed"
  | "cancelled"
  | "scheduled"
  | "paused";

/** Повтор: в at по дням weekdays (0 — понедельник; пусто — каждый день) или каждые hours часов; {} — нет. */
export type TaskRepeat =
  | { kind: "daily"; at: string; weekdays: number[] }
  | { kind: "hours"; hours: number }
  | Record<string, never>;

export interface TaskRun {
  cycle: number;
  ts: number;
  status: TaskStatus;
  summary: string;
  steps: number;
}

export interface MeasureTable {
  path: string;
  name: string;
  columns: string[];
  rows: string[][];
  total: number;
  numeric: number[];
  link: string;
}

export interface TaskQuestion {
  id: string;
  text: string;
  kind: "question" | "approval";
  options: string[];
  detail: string;
  asked: number;
  answer: string;
  answered: number;
}

export interface TaskEntry {
  ts: number;
  text: string;
  kind: "progress" | "action" | "error" | "note";
}

export interface TaskCard {
  id: string;
  title: string;
  brief: string;
  status: TaskStatus;
  source: string;
  created: number;
  updated: number;
  finished: number;
  plan_done: number;
  plan_total: number;
  steps: number;
  progress: TaskEntry[];
  question: TaskQuestion | null;
  summary: string;
  error: string;
  note: string;
  result: string;
  sources: number;
  repeat: TaskRepeat;
  repeat_text: string;
  next_run: number;
  cycle: number;
  last_run: TaskRun | null;
  tables: string[];
}

export interface TaskFull {
  id: string;
  brief: string;
  title: string;
  deliverable: string;
  status: TaskStatus;
  plan: { text: string; done: boolean }[];
  log: TaskEntry[];
  questions: TaskQuestion[];
  sources: { title: string; url: string }[];
  summary: string;
  note: string;
  result: string;
  error: string;
  steps: number;
  worked_s: number;
  runs: number;
  usage: { prompt_tokens?: number; completion_tokens?: number; cost?: number };
  provider: string;
  model: string;
  card: TaskCard;
  note_link: string;
  result_link: string;
  repeat: TaskRepeat;
  next_run: number;
  cycle: number;
  history: TaskRun[];
  fails: number;
  tables: string[];
}

export const ACTIVE: TaskStatus[] = ["queued", "running", "waiting"];
export const REPEATING: TaskStatus[] = ["scheduled", "paused"];

interface BackgroundState {
  tasks: TaskCard[];
  loaded: boolean;
  missing: boolean;
  load: () => Promise<void>;
  upsert: (card: TaskCard) => void;
}

/** Как в ядре: идущие, потом повторяющиеся (ближайший запуск первым), потом законченные (новые первыми). */
function sorted(tasks: TaskCard[]): TaskCard[] {
  const rank = (t: TaskCard) => (ACTIVE.includes(t.status) ? 0 : REPEATING.includes(t.status) ? 1 : 2);
  return [...tasks].sort((a, b) => {
    const order = rank(a) - rank(b);
    if (order) return order;
    if (rank(a) === 1) return (a.next_run || Infinity) - (b.next_run || Infinity);
    return b.created - a.created;
  });
}

export const useBackground = create<BackgroundState>((set, get) => ({
  tasks: [],
  loaded: false,
  missing: false,
  load: async () => {
    try {
      const r = await api.get<{ items: TaskCard[] }>("/api/background");
      set({ tasks: sorted(r.items), loaded: true, missing: false });
    } catch (e) {
      set({ loaded: true, missing: (e as { status?: number }).status === 404 });
    }
  },
  upsert: (card) => {
    const rest = get().tasks.filter((t) => t.id !== card.id);
    set({ tasks: sorted([card, ...rest]) });
  },
}));

let wired = false;

/** Подписка на события и загрузка при подключении к ядру (один раз на окно). */
export function wireBackground() {
  if (wired) return;
  wired = true;
  onConnection((connected) => {
    if (connected) void useBackground.getState().load();
  });
  onEvent<{ task: TaskCard }>("background.updated", (e) => useBackground.getState().upsert(e.data.task));
  onEvent<{ id: string }>("background.deleted", (e) =>
    useBackground.setState((s) => ({ tasks: s.tasks.filter((t) => t.id !== e.data.id) })),
  );
}

/** Сколько задач ждут ответа человека — для точки в боковой панели. */
export function useWaitingCount(): number {
  return useBackground((s) => s.tasks.filter((t) => t.status === "waiting" && t.question).length);
}
