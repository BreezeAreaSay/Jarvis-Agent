// Записки: «Заметки» (последние заметки из ядра, просмотр и правка в окне) и «Стикер» (текст прямо
// на столе, хранится в настройках виджета).
import { NotebookPen, Pin, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { NoteItem } from "@/api/types";
import { Button, cx, Modal, notifyError, TextInput, timeAgo } from "@/ui";
import { WidgetFrame, type WidgetProps } from "../common";
import { HeadButton } from "./shared";

const NOTE_COLORS: Record<string, string> = {
  yellow: "#FBBF24",
  orange: "#FB923C",
  red: "#F87171",
  pink: "#F472B6",
  violet: "#A78BFA",
  purple: "#A78BFA",
  blue: "#60A5FA",
  cyan: "#22D3EE",
  green: "#34D399",
};

function NoteModal({
  note,
  onClose,
}: {
  note: Partial<NoteItem> | null;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setTitle(note?.title ?? "");
    setBody(note?.body ?? "");
  }, [note]);
  const save = async () => {
    if (!title.trim() && !body.trim()) return onClose();
    setBusy(true);
    try {
      if (note?.id) await api.patch(`/api/notes/${encodeURIComponent(note.id)}`, { title: title.trim(), body });
      else await api.post("/api/notes", { title: title.trim(), body });
      onClose();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!note?.id) return;
    try {
      await api.del(`/api/notes/${encodeURIComponent(note.id)}`);
      onClose();
    } catch (e) {
      notifyError(e);
    }
  };
  return (
    <Modal
      open={!!note}
      onClose={onClose}
      title={note?.id ? "Заметка" : "Новая заметка"}
      icon={<NotebookPen style={{ width: 18, height: 18, color: "var(--accent-text)" }} />}
      width={520}
      footer={
        <>
          {note?.id && (
            <Button variant="ghost" icon={<Trash2 />} onClick={remove}>
              Удалить
            </Button>
          )}
          <span className="spacer" />
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" loading={busy} onClick={save}>
            Сохранить
          </Button>
        </>
      }
    >
      <TextInput value={title} onChange={setTitle} placeholder="Заголовок" autoFocus={!note?.id} />
      <textarea
        className="textarea"
        style={{ marginTop: 10, minHeight: 160 }}
        value={body}
        placeholder="Текст заметки"
        onChange={(e) => setBody(e.target.value)}
      />
    </Modal>
  );
}

export function NotesWidget({ h, config }: WidgetProps) {
  const [items, setItems] = useState<NoteItem[] | null>(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Partial<NoteItem> | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .get<{ items: NoteItem[] }>("/api/notes")
        .then((r) => alive && (setItems(r.items ?? []), setError("")))
        .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    load();
    const off = onEvent("notes.changed", () => load());
    return () => {
      alive = false;
      off();
    };
  }, []);
  const sorted = useMemo(
    () => [...(items ?? [])].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || (b.updated ?? 0) - (a.updated ?? 0)),
    [items],
  );
  const one = h <= 1;
  return (
    <WidgetFrame
      icon={<NotebookPen />}
      title={(config.title as string) || "Заметки"}
      actions={<HeadButton icon={<Plus />} label="Новая заметка" onClick={() => setEditing({})} />}
    >
      {!items ? (
        <div className="sub">{error || "Загружаю…"}</div>
      ) : !items.length ? (
        <div className="wx-empty">
          <span>Заметок пока нет. Скажите «запиши: купить батарейки»</span>
        </div>
      ) : (
        <div className="wlist wx-notes">
          {sorted.slice(0, one ? 2 : 20).map((n) => {
            const head = n.title || n.body.split("\n")[0];
            const rest = n.title ? n.body : n.body.split("\n").slice(1).join(" ");
            return (
              <button
                key={n.id}
                className="wx-note"
                style={{ "--note": NOTE_COLORS[n.color] ?? "rgba(255,255,255,0.22)" } as React.CSSProperties}
                onClick={() => setEditing(n)}
              >
                <span className="t ellipsis">
                  {!!n.pinned && <Pin className="pin" />}
                  {head || "Без названия"}
                </span>
                {!one && rest && <span className="b">{rest}</span>}
                {!one && <span className="m">{timeAgo(n.updated || n.created)}</span>}
              </button>
            );
          })}
        </div>
      )}
      <NoteModal note={editing} onClose={() => setEditing(null)} />
    </WidgetFrame>
  );
}

// --- стикер ---------------------------------------------------------------------------------------------
const STICKY = ["yellow", "pink", "green", "blue", "violet"];

export function StickyWidget({ config, setConfig }: WidgetProps) {
  const saved = String(config.text ?? "");
  const color = STICKY.includes(String(config.color)) ? String(config.color) : "yellow";
  const [text, setText] = useState(saved);
  const focused = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const latest = useRef({ config, setConfig });
  latest.current = { config, setConfig };
  useEffect(() => {
    if (!focused.current) setText(saved);
  }, [saved]);
  useEffect(() => () => clearTimeout(timer.current), []);
  const commit = (v: string) => {
    clearTimeout(timer.current);
    const { config: c, setConfig: set } = latest.current;
    if (v !== String(c.text ?? "")) set({ ...c, text: v });
  };
  return (
    <div className={cx("widget wx-sticky", color)}>
      <textarea
        value={text}
        placeholder="Напишите что-нибудь…"
        spellCheck={false}
        onFocus={() => (focused.current = true)}
        onBlur={() => {
          focused.current = false;
          commit(text);
        }}
        onChange={(e) => {
          const v = e.target.value;
          setText(v);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => commit(v), 900);
        }}
      />
      <div className="wx-sticky-colors">
        {STICKY.map((c) => (
          <button
            key={c}
            className={cx("wx-sticky-dot", c, c === color && "on")}
            aria-label="Цвет стикера"
            onClick={() => setConfig({ ...config, text, color: c })}
          />
        ))}
      </div>
    </div>
  );
}
