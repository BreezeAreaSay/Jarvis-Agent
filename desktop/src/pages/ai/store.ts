// Состояние раздела Jarvis AI: список чатов, сообщения, ответы по кускам (chat.*), прогон агента (agent.*).
// События ядра подключает bindAiEvents() — один раз за жизнь окна, чтобы ответ дописывался, даже пока
// открыт другой раздел.
import { create } from "zustand";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { Chat, GeneratedInfo, UiMessage } from "@/api/types";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { notifyError } from "@/ui";
import {
  type AgentRun,
  type AgentStatus,
  type AgentStep,
  DRAFT,
  type Failure,
  isTerminal,
  type Mode,
  type PendingReply,
  type Thread,
  type ThreadMessage,
  type ToolRow,
  type Usage,
} from "./types";
import { errText } from "./util";

const emptyThread = (): Thread => ({
  messages: [],
  loaded: false,
  loading: false,
  error: "",
  notFound: false,
  pending: null,
  sending: false,
  stopping: false,
  failure: null,
});

const newPending = (): PendingReply => ({
  messageId: null,
  text: "",
  status: null,
  tools: [],
  memory: [],
  images: [],
  startedAt: Date.now(),
  updatedAt: Date.now(),
});

/** Сколько ждать хоть какого-то события ответа, прежде чем показать ошибку. */
const SILENCE_MS = 180_000;

let seq = 0;
const localId = () => `local-${Date.now().toString(36)}-${++seq}`;
const nowS = () => Date.now() / 1000;

export function sortChats(list: Chat[]): Chat[] {
  return [...list].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || b.updated - a.updated);
}

function upsertChat(list: Chat[], chat: Chat): Chat[] {
  const i = list.findIndex((c) => c.id === chat.id);
  return i >= 0 ? list.map((c) => (c.id === chat.id ? { ...c, ...chat } : c)) : [chat, ...list];
}

function sameText(a: string, b: string): boolean {
  return a.trim() === b.trim();
}

/** Справочные инструменты (поиск, погода, списки, «запомнить») — не действия: карточкой «Выполнено» их не
 *  показываем (так же решает ядро для meta.actions). */
export const LOOKUP_TOOL = /^(list_|get_|web_|fetch_|search_|read_)|^remember$/;

/** Итог ответа из того, что пришло по кускам (если ядро не прислало chat.message). */
function finalizePending(chatId: string, p: PendingReply, extra: Partial<UiMessage["meta"]> = {}): ThreadMessage | null {
  const actions = extra.actions?.length
    ? extra.actions
    : p.tools
        .filter((t) => !LOOKUP_TOOL.test(t.name))
        .map((t) => ({ title: t.title, ok: t.status !== "error", detail: t.detail }));
  const sources = extra.sources ?? [];
  const memory = p.memory.length ? p.memory : extra.memory;
  const images = p.images.length
    ? { images: p.images.map((i) => i.src), generated: p.images.map((i) => i.info ?? { prompt: "" }) }
    : {};
  if (!p.text.trim() && !actions.length && !sources.length && !memory?.length && !extra.error && !p.images.length)
    return null;
  return {
    id: p.messageId ?? localId(),
    chat_id: chatId,
    role: "assistant",
    content: p.text,
    created: nowS(),
    meta: { ...extra, actions, sources, ...(memory?.length ? { memory } : {}), ...images },
  };
}

interface AiState {
  chats: Chat[];
  chatsLoaded: boolean;
  chatsError: string;
  threads: Record<string, Thread>;
  agent: AgentRun | null;
  usage: Usage | null;
  models: string[] | null;
  modelsLoading: boolean;
  modelsError: string;
  draftMode: Mode | null;
  lastKey: string | null;

  loadChats: () => Promise<void>;
  loadThread: (id: string) => Promise<void>;
  loadUsage: () => Promise<void>;
  loadModels: (force?: boolean) => Promise<void>;
  newChat: () => void;
  send: (key: string, text: string, opts: { mode: Mode; images?: string[] }) => Promise<void>;
  stop: (key: string) => Promise<void>;
  retry: (key: string, mode: Mode) => void;
  dismissFailure: (key: string) => void;
  rename: (id: string, title: string) => Promise<void>;
  setPinned: (id: string, pinned: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  setMode: (key: string, mode: Mode) => Promise<void>;
  setChatModel: (id: string, model: string) => Promise<void>;
  loadAgent: () => Promise<void>;
  stopAgent: () => Promise<void>;
  dismissAgent: () => void;
}

export const useAi = create<AiState>((set, get) => {
  /** Изменить ветку сообщений (создаётся при необходимости). */
  const patchThread = (key: string, fn: (t: Thread) => Partial<Thread>) =>
    set((s) => {
      const cur = s.threads[key] ?? emptyThread();
      return { threads: { ...s.threads, [key]: { ...cur, ...fn(cur) } } };
    });

  return {
    chats: [],
    chatsLoaded: false,
    chatsError: "",
    threads: {},
    agent: null,
    usage: null,
    models: null,
    modelsLoading: false,
    modelsError: "",
    draftMode: null,
    lastKey: null,

    async loadChats() {
      try {
        const res = await api.get<{ items: Chat[] }>("/api/chats");
        set({ chats: sortChats(res.items ?? []), chatsLoaded: true, chatsError: "" });
      } catch (e) {
        set({ chatsLoaded: true, chatsError: errText(e) });
      }
    },

    async loadThread(id) {
      if (get().threads[id]?.loading) return;
      patchThread(id, () => ({ loading: true, error: "", notFound: false }));
      try {
        const res = await api.get<{ chat: Chat; messages: UiMessage[] }>(`/api/chats/${encodeURIComponent(id)}`);
        set((s) => {
          const cur = s.threads[id] ?? emptyThread();
          const server = res.messages ?? [];
          const ids = new Set(server.map((m) => m.id));
          const merged: ThreadMessage[] = [...server];
          for (const m of cur.messages) {
            if (ids.has(m.id)) continue;
            // своё сообщение, которое ядро уже сохранило, — не дублировать
            if (m.local && m.role === "user" && server.some((x) => x.role === "user" && sameText(x.content, m.content)))
              continue;
            merged.push(m);
          }
          merged.sort((a, b) => a.created - b.created);
          // ответ уже записан ядром — «пишется…» больше не нужно
          let pending = cur.pending;
          if (pending?.messageId && ids.has(pending.messageId)) pending = null;
          return {
            chats: res.chat ? sortChats(upsertChat(s.chats, res.chat)) : s.chats,
            threads: { ...s.threads, [id]: { ...cur, messages: merged, pending, loaded: true, loading: false } },
          };
        });
      } catch (e) {
        const status = (e as { status?: number }).status;
        patchThread(id, () => ({ loading: false, error: errText(e), notFound: status === 404 }));
      }
    },

    async loadUsage() {
      try {
        set({ usage: await api.get<Usage>("/api/ai/usage") });
      } catch {
        /* счётчик расходов — необязательная мелочь */
      }
    },

    async loadModels(force) {
      if (get().modelsLoading || (get().models && !force)) return;
      set({ modelsLoading: true, modelsError: "" });
      try {
        const res = await api.get<{ items: string[] }>("/api/ai/models");
        set({ models: res.items ?? [], modelsLoading: false });
      } catch (e) {
        set({ modelsLoading: false, modelsError: errText(e) });
      }
    },

    newChat() {
      set((s) => ({ draftMode: null, threads: { ...s.threads, [DRAFT]: { ...emptyThread(), loaded: true } } }));
      navigate("ai", DRAFT);
    },

    async send(key, text, { mode, images }) {
      // режим «Агент» — тоже через чат: ядро запускает агента в этом чате и пишет туда итог
      let chatId = key;
      if (key === DRAFT) {
        patchThread(DRAFT, () => ({ sending: true, failure: null }));
        try {
          const chat = await api.post<Chat>("/api/chats", { mode });
          set((s) => ({
            chats: sortChats(upsertChat(s.chats, chat)),
            threads: { ...s.threads, [chat.id]: { ...emptyThread(), loaded: true } },
          }));
          chatId = chat.id;
          patchThread(DRAFT, () => ({ sending: false }));
          navigate("ai", chat.id);
        } catch (e) {
          patchThread(DRAFT, () => ({ sending: false, failure: { error: errText(e), retryText: text, images } }));
          return;
        }
      }
      const local: ThreadMessage = {
        id: localId(),
        chat_id: chatId,
        role: "user",
        content: text,
        created: nowS(),
        local: true,
        meta: images?.length ? { images } : {},
      };
      patchThread(chatId, (t) => ({
        messages: [...t.messages, local],
        sending: true,
        failure: null,
        pending: t.pending ?? { ...newPending(), status: { kind: "thinking", text: "" } },
      }));
      try {
        const res = await api.post<{ chat_id: string; message_id: string; reply_id?: string }>(
          `/api/chats/${encodeURIComponent(chatId)}/messages`,
          { text, mode, ...(images?.length ? { images } : {}) },
        );
        patchThread(chatId, (t) => ({
          sending: false,
          // id будущего ответа — чтобы узнать его в событиях, даже если первым придёт итог
          pending:
            t.pending && !t.pending.messageId && res?.reply_id && !t.messages.some((m) => m.id === res.reply_id)
              ? { ...t.pending, messageId: res.reply_id }
              : t.pending,
        }));
      } catch (e) {
        patchThread(chatId, (t) => ({
          sending: false,
          pending: t.pending && (t.pending.text || t.pending.tools.length) ? t.pending : null,
          messages: t.messages.filter((m) => m.id !== local.id),
          failure: { error: errText(e), retryText: text, images },
        }));
      }
    },

    async stop(key) {
      const t = get().threads[key];
      const chatBusy = !!t && (t.sending || !!t.pending);
      const agent = get().agent;
      // в этом чате ответ не пишется — значит, «Стоп» относится к агенту
      if (!chatBusy && agent && !isTerminal(agent.status)) return get().stopAgent();
      if (key === DRAFT) return;
      patchThread(key, () => ({ stopping: true }));
      try {
        await api.post(`/api/chats/${encodeURIComponent(key)}/stop`);
      } catch (e) {
        notifyError(e);
      }
      // ядро должно прислать chat.done; если промолчало — закрываем ответ сами
      setTimeout(() => {
        const t = get().threads[key];
        if (!t?.stopping) return;
        const msg = t.pending ? finalizePending(key, t.pending) : null;
        patchThread(key, (cur) => ({
          stopping: false,
          sending: false,
          pending: null,
          messages: msg && !cur.messages.some((m) => m.id === msg.id) ? [...cur.messages, msg] : cur.messages,
        }));
      }, 3000);
    },

    retry(key, mode) {
      const f = get().threads[key]?.failure;
      if (!f) return;
      patchThread(key, () => ({ failure: null }));
      get().send(key, f.retryText, { mode, images: f.images });
    },

    dismissFailure(key) {
      patchThread(key, () => ({ failure: null }));
    },

    async rename(id, title) {
      const prev = get().chats.find((c) => c.id === id);
      if (!prev || !title.trim() || title.trim() === prev.title) return;
      set((s) => ({ chats: upsertChat(s.chats, { ...prev, title: title.trim() }) }));
      try {
        const chat = await api.patch<Chat>(`/api/chats/${encodeURIComponent(id)}`, { title: title.trim() });
        set((s) => ({ chats: upsertChat(s.chats, chat) }));
      } catch (e) {
        set((s) => ({ chats: upsertChat(s.chats, prev) }));
        notifyError(e);
      }
    },

    async setPinned(id, pinned) {
      const prev = get().chats.find((c) => c.id === id);
      if (!prev) return;
      set((s) => ({ chats: sortChats(upsertChat(s.chats, { ...prev, pinned })) }));
      try {
        const chat = await api.patch<Chat>(`/api/chats/${encodeURIComponent(id)}`, { pinned });
        set((s) => ({ chats: sortChats(upsertChat(s.chats, chat)) }));
      } catch (e) {
        set((s) => ({ chats: sortChats(upsertChat(s.chats, prev)) }));
        notifyError(e);
      }
    },

    async remove(id) {
      try {
        await api.del(`/api/chats/${encodeURIComponent(id)}`);
        set((s) => {
          const threads = { ...s.threads };
          delete threads[id];
          return { chats: s.chats.filter((c) => c.id !== id), threads };
        });
      } catch (e) {
        notifyError(e);
      }
    },

    async setMode(key, mode) {
      if (key === DRAFT) {
        set({ draftMode: mode });
        return;
      }
      const prev = get().chats.find((c) => c.id === key);
      if (!prev) return;
      set((s) => ({ chats: upsertChat(s.chats, { ...prev, mode }) }));
      try {
        const chat = await api.patch<Chat>(`/api/chats/${encodeURIComponent(key)}`, { mode });
        set((s) => ({ chats: upsertChat(s.chats, chat) }));
      } catch (e) {
        set((s) => ({ chats: upsertChat(s.chats, prev) }));
        notifyError(e);
      }
    },

    async setChatModel(id, model) {
      const prev = get().chats.find((c) => c.id === id);
      if (!prev) return;
      set((s) => ({ chats: upsertChat(s.chats, { ...prev, model }) }));
      try {
        const chat = await api.patch<Chat>(`/api/chats/${encodeURIComponent(id)}`, { model });
        set((s) => ({ chats: upsertChat(s.chats, chat) }));
      } catch (e) {
        set((s) => ({ chats: upsertChat(s.chats, prev) }));
        notifyError(e);
      }
    },

    async loadAgent() {
      try {
        const d = await api.get<{
          run_id: string;
          task: string;
          status: string;
          step: number;
          total: number;
          text: string;
          plan?: string[];
          outcome?: AgentRun["outcome"];
          chat_id?: string;
        }>("/api/agent");
        if (!d?.run_id || !["planning", "running", "waiting"].includes(d.status)) return;
        const cur = get().agent;
        if (cur?.runId === d.run_id) return;
        set({
          agent: {
            runId: d.run_id,
            task: d.task,
            status: d.status as AgentStatus,
            step: d.step,
            total: d.total,
            text: d.text,
            steps: [],
            plan: d.plan ?? [],
            outcome: d.outcome ?? "",
            chatId: d.chat_id ?? "",
            startedAt: Date.now(),
          },
        });
      } catch {
        /* старое ядро без GET /api/agent — панель появится по событиям */
      }
    },

    async stopAgent() {
      const started = get().agent;
      try {
        await api.post("/api/agent/stop");
      } catch (e) {
        notifyError(e);
      }
      // ядро должно прислать agent.state stopped; если промолчало — закрываем панель сами
      setTimeout(() => {
        const a = get().agent;
        if (!a || isTerminal(a.status) || a.runId !== started?.runId) return;
        set({ agent: { ...a, status: "stopped", text: "Остановлено.", endedAt: Date.now() } });
      }, 6000);
    },

    dismissAgent() {
      const a = get().agent;
      if (a && isTerminal(a.status)) set({ agent: null });
    },
  };
});

// --- события ядра ---------------------------------------------------------------------------------------
let bound = false;

export function bindAiEvents() {
  if (bound) return;
  bound = true;
  const st = useAi;
  const thread = (id: string) => st.getState().threads[id];
  const patch = (id: string, fn: (t: Thread) => Partial<Thread>) =>
    st.setState((s) => {
      const cur = s.threads[id];
      if (!cur) return {};
      const next = fn(cur);
      if (next.pending && next.pending !== cur.pending) next.pending = { ...next.pending, updatedAt: Date.now() };
      return { threads: { ...s.threads, [id]: { ...cur, ...next } } };
    });

  // ядро перезапустилось или связь вернулась — события могли потеряться
  // (раздел подключается после первого hello, так что любой следующий — это переподключение)
  onEvent("hello", () => {
    const s = st.getState();
    s.loadChats();
    s.loadUsage();
    for (const [key, t] of Object.entries(s.threads))
      if (key !== DRAFT && (t.pending || key === s.lastKey)) s.loadThread(key);
  });

  // в «Приватности» удалили картинки (или их убрала очистка по срокам) — открытые чаты перечитать
  const reloadOpen = () => {
    const s = st.getState();
    for (const [key, t] of Object.entries(s.threads)) if (key !== DRAFT && t.loaded && !t.pending) s.loadThread(key);
  };
  onEvent("data.cleared", reloadOpen);
  onEvent("data.purged", reloadOpen);

  // сторож: ответ «пишется», а событий давно нет
  setInterval(() => {
    const now = Date.now();
    for (const [key, t] of Object.entries(st.getState().threads)) {
      if (!t.pending || t.stopping || now - t.pending.updatedAt < SILENCE_MS) continue;
      const lastUser = [...t.messages].reverse().find((m) => m.role === "user");
      patch(key, (cur) => {
        const partial = cur.pending ? finalizePending(key, cur.pending) : null;
        return {
          pending: null,
          sending: false,
          messages: partial ? [...cur.messages, partial] : cur.messages,
          failure: {
            error: "Jarvis не ответил — проверьте подключение к ИИ и попробуйте ещё раз",
            retryText: lastUser?.content ?? "",
            images: lastUser?.meta?.images,
          },
        };
      });
    }
  }, 15_000);
  let usageTimer: ReturnType<typeof setTimeout> | undefined;
  const refreshUsage = () => {
    clearTimeout(usageTimer);
    usageTimer = setTimeout(() => st.getState().loadUsage(), 600);
  };

  // сменили провайдера или адрес — список моделей уже не тот
  const aiNow = useApp.getState().settings?.ai;
  let providerKey = aiNow ? `${aiNow.provider}|${aiNow.base_url}` : "";
  onEvent<{ settings: { ai?: { provider?: string; base_url?: string } } }>("settings.changed", (e) => {
    const ai = e.data.settings?.ai;
    const key = `${ai?.provider ?? ""}|${ai?.base_url ?? ""}`;
    if (providerKey && key !== providerKey) st.setState({ models: null, modelsError: "" });
    providerKey = key;
  });

  onEvent<{ chat: Chat }>("chat.created", (e) => {
    st.setState((s) => ({ chats: sortChats(upsertChat(s.chats, e.data.chat)) }));
  });
  onEvent<{ chat: Chat }>("chat.updated", (e) => {
    st.setState((s) => ({ chats: sortChats(upsertChat(s.chats, e.data.chat)) }));
  });
  onEvent<{ id: string }>("chat.deleted", (e) => {
    st.setState((s) => {
      const threads = { ...s.threads };
      delete threads[e.data.id];
      return { chats: s.chats.filter((c) => c.id !== e.data.id), threads };
    });
  });

  onEvent<{ chat_id: string; message: UiMessage }>("chat.message", (e) => {
    const { chat_id, message } = e.data;
    if (!thread(chat_id)) return;
    patch(chat_id, (t) => {
      const m: ThreadMessage = message;
      let pending = t.pending;
      // итог от ядра — он главный: карточки, источники и память берём из его meta
      if (m.role === "assistant" && pending && (!pending.messageId || pending.messageId === m.id)) pending = null;
      let messages: ThreadMessage[];
      if (t.messages.some((x) => x.id === m.id)) {
        messages = t.messages.map((x) => (x.id === m.id ? { ...m, meta: { ...x.meta, ...m.meta } } : x));
      } else if (m.role === "user") {
        const i = t.messages.findIndex((x) => x.local && x.role === "user" && sameText(x.content, m.content));
        messages = i >= 0 ? t.messages.map((x, j) => (j === i ? m : x)) : [...t.messages, m];
      } else {
        messages = [...t.messages, m];
      }
      return {
        messages,
        pending,
        ...(m.role === "assistant" ? { stopping: false } : {}),
        ...(m.role === "assistant" && m.meta?.error ? { failure: null } : {}),
      };
    });
  });

  onEvent<{ chat_id: string; message_id: string; delta: string }>("chat.delta", (e) => {
    const { chat_id, message_id, delta } = e.data;
    if (!thread(chat_id)) return;
    patch(chat_id, (t) => {
      if (t.messages.some((m) => m.id === message_id)) return {};
      const p = t.pending ?? newPending();
      return { pending: { ...p, messageId: p.messageId ?? message_id, text: p.text + (delta ?? ""), status: null } };
    });
  });

  onEvent<{ chat_id: string; message_id: string; status: string; text: string }>("chat.status", (e) => {
    const { chat_id, message_id, status, text } = e.data;
    if (!thread(chat_id)) return;
    patch(chat_id, (t) => {
      if (status === "done" || status === "error") return t.pending ? { pending: { ...t.pending, status: null } } : {};
      if (message_id && t.messages.some((m) => m.id === message_id)) return {};
      const p = t.pending ?? newPending();
      const kind = status === "searching" || status === "tool" ? status : "thinking";
      return { pending: { ...p, messageId: p.messageId ?? message_id ?? null, status: { kind, text: text ?? "" } } };
    });
  });

  onEvent<{ chat_id: string; message_id: string } & ToolRow>("chat.tool", (e) => {
    const { chat_id, message_id, call_id, name, title, status, detail } = e.data;
    if (!thread(chat_id)) return;
    patch(chat_id, (t) => {
      const p = t.pending ?? newPending();
      const row: ToolRow = { call_id, name, title: title || name, status, detail };
      const tools = p.tools.some((x) => x.call_id === call_id)
        ? p.tools.map((x) => (x.call_id === call_id ? { ...x, ...row, title: row.title || x.title } : x))
        : [...p.tools, row];
      return { pending: { ...p, messageId: p.messageId ?? message_id ?? null, tools } };
    });
  });

  // нарисованная картинка — в ответ, который сейчас пишется (итог с meta.images придёт в chat.message)
  onEvent<{ chat_id: string; message_id?: string; image: string; info?: GeneratedInfo }>("chat.image", (e) => {
    const { chat_id, message_id, image, info } = e.data;
    if (!thread(chat_id) || !image) return;
    patch(chat_id, (t) => {
      if (message_id && t.messages.some((m) => m.id === message_id)) return {};
      const p = t.pending ?? newPending();
      if (p.images.some((i) => i.src === image)) return {};
      return { pending: { ...p, messageId: p.messageId ?? message_id ?? null, images: [...p.images, { src: image, info }] } };
    });
  });

  onEvent<{
    chat_id: string;
    message_id: string;
    text?: string;
    sources?: { title: string; url: string; fetched_at?: string }[];
    actions?: { title: string; ok: boolean; detail?: string }[];
    usage?: Record<string, number>;
  }>("chat.done", (e) => {
    const d = e.data;
    refreshUsage();
    if (!thread(d.chat_id)) return;
    patch(d.chat_id, (t) => {
      if (t.messages.some((m) => m.id === d.message_id) || !t.pending)
        return { pending: null, stopping: false, sending: false };
      const p: PendingReply = { ...t.pending, messageId: d.message_id ?? t.pending.messageId, text: d.text ?? t.pending.text };
      const msg = finalizePending(d.chat_id, p, { sources: d.sources ?? [], actions: d.actions ?? [], usage: d.usage });
      return { pending: null, stopping: false, sending: false, messages: msg ? [...t.messages, msg] : t.messages };
    });
  });

  onEvent<{ chat_id: string; message_id?: string; error: string }>("chat.error", (e) => {
    const { chat_id, message_id, error } = e.data;
    if (!thread(chat_id)) return;
    patch(chat_id, (t) => {
      // ядро уже сохранило ответ с meta.error (пришёл chat.message) — второй раз ошибку не показываем
      if (message_id && t.messages.some((m) => m.id === message_id))
        return { pending: null, sending: false, stopping: false, failure: null };
      const partial = t.pending ? finalizePending(chat_id, { ...t.pending, memory: [] }) : null;
      const lastUser = [...t.messages].reverse().find((m) => m.role === "user");
      const failure: Failure = {
        error: error || "Не получилось ответить",
        retryText: lastUser?.content ?? "",
        images: lastUser?.meta?.images,
      };
      return {
        pending: null,
        sending: false,
        stopping: false,
        messages: partial ? [...t.messages, partial] : t.messages,
        failure,
      };
    });
  });

  onEvent<{ item: { id: string; text: string; source: string } }>("memory.added", (e) => {
    // событие без chat_id: относим к чату, который сейчас отвечает
    const threads = st.getState().threads;
    let key: string | null = null;
    let latest = 0;
    for (const [k, t] of Object.entries(threads)) {
      if (t.pending && t.pending.startedAt > latest) {
        latest = t.pending.startedAt;
        key = k;
      }
    }
    if (!key) return;
    const text = e.data.item?.text;
    if (!text) return;
    patch(key, (t) => (t.pending && !t.pending.memory.includes(text) ? { pending: { ...t.pending, memory: [...t.pending.memory, text] } } : {}));
  });

  onEvent<{
    run_id: string;
    task: string;
    status: AgentStatus | "idle";
    step: number;
    total: number;
    text: string;
    plan?: string[];
    outcome?: AgentRun["outcome"];
    chat_id?: string;
  }>(
    "agent.state",
    (e) => {
      const d = e.data;
      if (!d?.run_id || d.status === "idle") return;
      const a = st.getState().agent;
      const base: AgentRun =
        a && a.runId === d.run_id
          ? a
          : {
              runId: d.run_id,
              task: d.task ?? "",
              status: d.status,
              step: 0,
              total: 0,
              text: "",
              steps: [],
              plan: [],
              outcome: "",
              chatId: d.chat_id ?? "",
              startedAt: Date.now(),
            };
      const run: AgentRun = {
        ...base,
        task: d.task || base.task,
        status: d.status,
        step: d.step ?? base.step,
        total: d.total ?? base.total,
        text: d.text ?? base.text,
        plan: d.plan ?? base.plan,
        outcome: d.outcome ?? base.outcome,
        chatId: d.chat_id || base.chatId,
      };
      if (isTerminal(d.status)) {
        run.endedAt = base.endedAt ?? Date.now();
        if (d.status === "done") run.steps = run.steps.map((x) => (x.status === "running" ? { ...x, status: "ok" } : x));
      }
      st.setState({ agent: run });
    },
  );

  onEvent<{ run_id: string; index: number; title: string; status: AgentStep["status"] }>("agent.step", (e) => {
    const d = e.data;
    let a = st.getState().agent;
    if (!a || a.runId !== d.run_id) {
      if (a && !isTerminal(a.status)) return;
      a = {
        runId: d.run_id,
        task: "",
        status: "running",
        step: 0,
        total: 0,
        text: d.title,
        steps: [],
        plan: [],
        outcome: "",
        chatId: "",
        startedAt: Date.now(),
      };
    }
    const row: AgentStep = { index: d.index, title: d.title, status: d.status };
    const steps = a.steps.some((s) => s.index === d.index)
      ? a.steps.map((s) => (s.index === d.index ? { ...s, ...row, title: row.title || s.title } : s))
      : [...a.steps, row].sort((x, y) => x.index - y.index);
    st.setState({ agent: { ...a, steps } });
  });
}
