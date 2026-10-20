// Типы раздела Jarvis AI (контракт ядра — docs/CONTRACTS.md §4; общие типы — @/api/types).
import type { Chat, GeneratedInfo, UiMessage } from "@/api/types";

export type Mode = Chat["mode"];

/** Ключ «нового чата»: ещё не создан на ядре, создаётся первым сообщением (#/ai/new). */
export const DRAFT = "new";

export const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: "chat", label: "Только чат", hint: "Просто разговор — без действий на компьютере" },
  { value: "all", label: "Всё вместе", hint: "Ответы, поиск в интернете и команды на ПК" },
  { value: "agent", label: "Агент", hint: "Сам выполняет задачу по шагам и спрашивает разрешения" },
];

export const MODE_LABEL: Record<Mode, string> = { chat: "Только чат", all: "Всё вместе", agent: "Агент" };

export interface ToolRow {
  call_id: string;
  name: string;
  title: string;
  status: "running" | "ok" | "error";
  detail?: string;
}

/** Ответ, который сейчас пишется: статус, вызовы инструментов, текст по кускам. */
export interface PendingReply {
  messageId: string | null;
  text: string;
  status: { kind: "thinking" | "searching" | "tool"; text: string } | null;
  tools: ToolRow[];
  memory: string[];
  /** Картинки, нарисованные в этом ответе (событие chat.image), — видны ещё до конца ответа. */
  images: { src: string; info?: GeneratedInfo }[];
  startedAt: number;
  /** Когда пришло последнее событие ответа — для сторожа «ядро замолчало». */
  updatedAt: number;
}

export interface ThreadMessage extends UiMessage {
  /** Сообщение есть только в интерфейсе (ещё не подтверждено ядром). */
  local?: boolean;
}

/** Поля meta, которые ядро пишет сверх контракта: ответ остановлен, итог агента. */
export interface MetaExtra {
  stopped?: boolean;
  agent?: { run_id: string; status: string; steps: number; plan: string[] };
}

export interface Failure {
  error: string;
  retryText: string;
  images?: string[];
}

export interface Thread {
  messages: ThreadMessage[];
  loaded: boolean;
  loading: boolean;
  error: string;
  notFound: boolean;
  pending: PendingReply | null;
  sending: boolean;
  stopping: boolean;
  failure: Failure | null;
}

export type AgentStatus = "planning" | "running" | "waiting" | "done" | "stopped" | "error";

export interface AgentStep {
  index: number;
  title: string;
  status: "running" | "ok" | "error";
}

/** Прогон агента (события agent.state / agent.step; итог ядро пишет сообщением в чат). */
export interface AgentRun {
  runId: string;
  task: string;
  status: AgentStatus;
  step: number;
  total: number;
  text: string;
  steps: AgentStep[];
  /** План шагов (agent.state plan) и оценка итога самим агентом. */
  plan: string[];
  outcome: "" | "done" | "partial" | "failed";
  /** Чат, куда агент пишет итог: сообщение в него, пока агент работает, — поправка или ответ агенту. */
  chatId: string;
  startedAt: number;
  endedAt?: number;
}

export interface Usage {
  month: { prompt_tokens: number; completion_tokens: number; cost: number };
  budget: number;
}

export function isTerminal(status: AgentStatus): boolean {
  return status === "done" || status === "stopped" || status === "error";
}
