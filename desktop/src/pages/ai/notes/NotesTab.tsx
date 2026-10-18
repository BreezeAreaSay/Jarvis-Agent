// Заметки: сетка карточек, поиск, создание и правка прямо в карточке, закрепление, цвет, удаление.
import { Palette, Pin, PinOff, Plus, Search, StickyNote } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { NoteItem } from "@/api/types";
import { Button, cx, notifyError, TextInput, timeAgo } from "@/ui";
import { Menu } from "../Menu";
import { plural } from "../util";
import { DeleteButton, ListState, truthy } from "./common";
import { useList } from "./useList";

export const NOTE_COLORS: { key: string; label: string }[] = [
  { key: "", label: "Без цвета" },
  { key: "amber", label: "Жёлтый" },
  { key: "green", label: "Зелёный" },
  { key: "sky", label: "Голубой" },
  { key: "violet", label: "Фиолетовый" },
  { key: "rose", label: "Розовый" },
];

function colorKey(c: string): string {
  return NOTE_COLORS.some((x) => x.key === c) ? c : c ? "custom" : "";
}

function Swatches({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  return (
    <div className="nt-swatches" role="radiogroup" aria-label="Цвет заметки">
      {NOTE_COLORS.map((c) => (
        <button
          key={c.key || "none"}
          type="button"
          role="radio"
          aria-checked={value === c.key}
          aria-label={c.label}
          title={c.label}
          className={cx("nt-swatch", value === c.key && "on")}
          data-color={c.key}
          onClick={() => onChange(c.key)}
        />
      ))}
    </div>
  );
}

function AutoTextarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { value: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 360)}px`;
  }, [props.value]);
  return <textarea ref={ref} rows={3} {...props} />;
}

function NoteEditor({
  note,
  onDone,
}: {
  note: NoteItem | null;
  onDone: (saved?: NoteItem) => void;
}) {
  const [title, setTitle] = useState(note?.title ?? "");
  const [body, setBody] = useState(note?.body ?? "");
  const [color, setColor] = useState(note?.color ?? "");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    const t = title.trim();
    const b = body.trim();
    if (!note && !t && !b) return onDone();
    setBusy(true);
    try {
      if (note) {
        const patch: Partial<NoteItem> = {};
        if (t !== note.title) patch.title = t;
        if (body !== note.body) patch.body = body;
        if (color !== note.color) patch.color = color;
        const saved = Object.keys(patch).length
          ? await api.patch<NoteItem>(`/api/notes/${encodeURIComponent(note.id)}`, patch)
          : note;
        onDone(saved);
      } else {
        onDone(await api.post<NoteItem>("/api/notes", { title: t, body, pinned: false, color }));
      }
    } catch (e) {
      notifyError(e);
      setBusy(false);
    }
  };

  return (
    <article
      className="nt-note editing pop-in"
      data-color={colorKey(color)}
      style={colorKey(color) === "custom" ? ({ "--note": color } as React.CSSProperties) : undefined}
      onKeyDown={(e) => {
        if (e.key === "Escape") onDone();
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) save();
      }}
    >
      <input
        className="nt-note-title-in"
        value={title}
        placeholder="Заголовок"
        aria-label="Заголовок заметки"
        autoFocus
        maxLength={200}
        onChange={(e) => setTitle(e.target.value)}
      />
      <AutoTextarea
        className="nt-note-body-in"
        value={body}
        placeholder="Текст заметки"
        aria-label="Текст заметки"
        onChange={(e) => setBody(e.target.value)}
      />
      <div className="nt-note-edit-foot">
        <Swatches value={colorKey(color) === "custom" ? "" : color} onChange={setColor} />
        <div className="nt-edit-btns">
          <Button size="sm" variant="ghost" onClick={() => onDone()}>
            Отмена
          </Button>
          <Button size="sm" variant="primary" onClick={save} loading={busy}>
            Готово
          </Button>
        </div>
      </div>
    </article>
  );
}

function NoteCard({
  note,
  onEdit,
  onPatch,
  onDelete,
}: {
  note: NoteItem;
  onEdit: () => void;
  onPatch: (patch: Partial<NoteItem>) => void;
  onDelete: () => void;
}) {
  const [palette, setPalette] = useState(false);
  const paletteBtn = useRef<HTMLButtonElement>(null);
  const pinned = truthy(note.pinned);
  const key = colorKey(note.color);
  const lines = (note.body ?? "").trim();
  const title = note.title?.trim() || lines.split("\n")[0] || "Без названия";
  const body = note.title?.trim() ? lines : lines.split("\n").slice(1).join("\n");
  return (
    <article
      className={cx("nt-note", pinned && "pinned")}
      data-color={key}
      style={key === "custom" ? ({ "--note": note.color } as React.CSSProperties) : undefined}
      tabIndex={0}
      aria-label={`Заметка «${title}»`}
      onClick={onEdit}
      onKeyDown={(e) => {
        if (e.key === "Enter" && e.target === e.currentTarget) onEdit();
      }}
    >
      <div className="nt-note-head">
        <span className="nt-dot" aria-hidden />
        <span className="nt-note-title">{title}</span>
        {pinned && <Pin className="nt-pin" aria-label="Закреплена" />}
      </div>
      {body && <div className="nt-note-body">{body}</div>}
      <div className="nt-note-foot" onClick={(e) => e.stopPropagation()}>
        <span className="faint tiny">{timeAgo(note.updated || note.created)}</span>
        <span className="spacer" />
        <div className="nt-hover-tools">
          <button
            type="button"
            className="nt-tool"
            aria-label={pinned ? "Открепить" : "Закрепить"}
            title={pinned ? "Открепить" : "Закрепить"}
            onClick={() => onPatch({ pinned: !pinned })}
          >
            {pinned ? <PinOff /> : <Pin />}
          </button>
          <button
            ref={paletteBtn}
            type="button"
            className="nt-tool"
            aria-label="Цвет"
            title="Цвет"
            aria-haspopup="menu"
            onClick={() => setPalette(!palette)}
          >
            <Palette />
          </button>
          <DeleteButton onConfirm={onDelete} label="Удалить заметку" />
        </div>
      </div>
      {palette && (
        <Menu
          anchor={paletteBtn.current}
          placement="bottom-end"
          width={200}
          label="Цвет заметки"
          onClose={() => setPalette(false)}
          items={NOTE_COLORS.map((c) => ({
            key: c.key || "none",
            label: c.label,
            checked: note.color === c.key,
            icon: <span className="nt-swatch sm" data-color={c.key} />,
            onSelect: () => onPatch({ color: c.key }),
          }))}
        />
      )}
    </article>
  );
}

export function NotesTab() {
  const { items, setItems, error, loading, reload } = useList<NoteItem>("/api/notes", ["notes.changed"]);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null); // id или "new"

  // правка заметки, которую удалили в другом окне, — закрыть
  useEffect(() => {
    if (editing && editing !== "new" && items && !items.some((n) => n.id === editing)) setEditing(null);
  }, [items, editing]);

  const list = useMemo(() => {
    const s = q.trim().toLowerCase();
    return [...(items ?? [])]
      .filter((n) => !s || `${n.title}\n${n.body}`.toLowerCase().includes(s))
      .sort(
        (a, b) =>
          Number(truthy(b.pinned)) - Number(truthy(a.pinned)) || (b.updated || b.created) - (a.updated || a.created),
      );
  }, [items, q]);

  const upsert = (n: NoteItem) =>
    setItems((cur) => (cur?.some((x) => x.id === n.id) ? cur.map((x) => (x.id === n.id ? n : x)) : [n, ...(cur ?? [])]));

  const patch = async (note: NoteItem, p: Partial<NoteItem>) => {
    upsert({ ...note, ...p });
    try {
      upsert(await api.patch<NoteItem>(`/api/notes/${encodeURIComponent(note.id)}`, p));
    } catch (e) {
      upsert(note);
      notifyError(e);
    }
  };

  const remove = async (note: NoteItem) => {
    setItems((cur) => cur?.filter((x) => x.id !== note.id) ?? null);
    try {
      await api.del(`/api/notes/${encodeURIComponent(note.id)}`);
    } catch (e) {
      upsert(note);
      notifyError(e);
    }
  };

  const total = items?.length ?? 0;

  return (
    <div className="nt-pane">
      <div className="nt-toolbar">
        <TextInput value={q} onChange={setQ} placeholder="Поиск по заметкам" icon={<Search />} className="nt-search" />
        <span className="spacer" />
        {total > 0 && (
          <span className="faint small">
            {total} {plural(total, "заметка", "заметки", "заметок")}
          </span>
        )}
        <Button variant="primary" size="sm" icon={<Plus />} onClick={() => setEditing("new")} disabled={editing === "new"}>
          Новая заметка
        </Button>
      </div>
      <ListState
        loading={loading && !items}
        error={items ? "" : error}
        onRetry={reload}
        empty={
          items && !list.length && editing !== "new" ? (
            q ? (
              <>Ничего не нашлось по запросу «{q}»</>
            ) : (
              <>
                <StickyNote />
                <div>Заметок пока нет</div>
                <div className="faint small">Скажите «Джарвис, запиши: купить хлеб» или создайте заметку вручную</div>
                <Button size="sm" icon={<Plus />} onClick={() => setEditing("new")}>
                  Новая заметка
                </Button>
              </>
            )
          ) : null
        }
      >
        <div className="nt-grid">
          {editing === "new" && (
            <NoteEditor
              note={null}
              onDone={(saved) => {
                if (saved) upsert(saved);
                setEditing(null);
              }}
            />
          )}
          {list.map((n) =>
            editing === n.id ? (
              <NoteEditor
                key={n.id}
                note={n}
                onDone={(saved) => {
                  if (saved) upsert(saved);
                  setEditing(null);
                }}
              />
            ) : (
              <NoteCard
                key={n.id}
                note={n}
                onEdit={() => setEditing(n.id)}
                onPatch={(p) => patch(n, p)}
                onDelete={() => remove(n)}
              />
            ),
          )}
        </div>
      </ListState>
    </div>
  );
}
