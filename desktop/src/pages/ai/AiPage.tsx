// Раздел «Jarvis AI»: вкладки [Чат] [Заметки и память].
// Маршруты: #/ai — последний открытый чат или новый, #/ai/new — новый чат, #/ai/<id> — чат,
// #/ai/notes/<notes|tasks|calendar|memory|reminders> — заметки и память.
import { MessagesSquare, NotebookPen } from "lucide-react";
import { useEffect } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { Tabs, useElementWidth } from "@/ui";
import "@/styles/ai.css";
import { ChatList } from "./ChatList";
import { ChatView, OrbRail } from "./ChatView";
import { NotesView } from "./notes/NotesView";
import { NOTE_TABS, type NoteTab } from "./notes/tabs";
import { bindAiEvents, useAi } from "./store";
import { DRAFT } from "./types";

let lastNotesTab: NoteTab = "notes";
const RAIL_MIN_WIDTH = 1180;

function replaceRoute(...parts: string[]) {
  location.replace(`#/${parts.map(encodeURIComponent).join("/")}`);
}

function ChatScreen({ chatId }: { chatId?: string }) {
  const chats = useAi((s) => s.chats);
  const chatsLoaded = useAi((s) => s.chatsLoaded);
  const lastKey = useAi((s) => s.lastKey);
  const key = chatId || DRAFT;

  // #/ai без чата — вернуться туда, где были в этот раз
  useEffect(() => {
    if (chatId || !lastKey || lastKey === DRAFT || !chatsLoaded) return;
    if (chats.some((c) => c.id === lastKey)) replaceRoute("ai", lastKey);
  }, [chatId, lastKey, chats, chatsLoaded]);

  useEffect(() => {
    if (chatId) useAi.setState({ lastKey: chatId });
  }, [chatId]);

  // чат удалили (здесь или в другом окне) — на новый
  const chatsError = useAi((s) => s.chatsError);
  const gone = chatsLoaded && !chatsError && key !== DRAFT && !chats.some((c) => c.id === key);
  useEffect(() => {
    if (gone) navigate("ai", DRAFT);
  }, [gone]);

  // большая сфера справа — только когда чату хватает места (ширину считает и CSS: см. ai.css «ailayout»)
  const [layout, width] = useElementWidth<HTMLDivElement>();
  return (
    <div className="ai-layout" ref={layout}>
      <ChatList activeId={key === DRAFT ? null : key} />
      <ChatView chatKey={key} />
      {width >= RAIL_MIN_WIDTH && <OrbRail chatKey={key} />}
    </div>
  );
}

export default function AiPage({ route }: { route: string[] }) {
  const section = route[0] === "notes" ? "notes" : "chat";
  const tab: NoteTab = NOTE_TABS.some((t) => t.value === route[1]) ? (route[1] as NoteTab) : lastNotesTab;
  if (section === "notes") lastNotesTab = tab;

  useEffect(() => {
    bindAiEvents();
    const s = useAi.getState();
    s.loadChats();
    s.loadUsage();
    s.loadAgent();
    // «ИИ подключён?» — ядро не шлёт об этом событие: спрашиваем при входе и после смены настроек/ключа
    const check = () =>
      api
        .get<{ ai?: { configured?: boolean } }>("/api/status")
        .then((r) => {
          if (r?.ai && typeof r.ai.configured === "boolean") useApp.setState({ aiConfigured: r.ai.configured });
        })
        .catch(() => undefined);
    check();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(check, 400);
    };
    const offs = [onEvent("settings.changed", soon), onEvent("secrets.changed", soon)];
    return () => {
      clearTimeout(timer);
      for (const off of offs) off();
    };
  }, []);

  return (
    <div className="page ai-page">
      <PageHeader title="Jarvis AI">
        <Tabs
          value={section}
          onChange={(v) => (v === "chat" ? navigate("ai") : navigate("ai", "notes", lastNotesTab))}
          items={[
            { value: "chat", label: "Чат", icon: <MessagesSquare /> },
            { value: "notes", label: "Заметки и память", icon: <NotebookPen /> },
          ]}
        />
      </PageHeader>
      <div className="page-body fill">
        {section === "notes" ? <NotesView tab={tab} /> : <ChatScreen chatId={route[0]} />}
      </div>
    </div>
  );
}
