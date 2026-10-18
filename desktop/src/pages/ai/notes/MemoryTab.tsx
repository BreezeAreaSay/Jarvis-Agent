// Память: что Jarvis знает о человеке. Jarvis запоминает только по просьбе («запомни, что…»); каждую запись
// можно поправить (текст и вид: факт, предположение, на сутки), удалить или очистить всю память разом.
import { Brain, Check as CheckIcon, Pencil, Plus, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { MemoryItem, MemoryKind } from "@/api/types";
import { useApp } from "@/store/app";
import { Button, cx, IconButton, notify, notifyError, type Option, Select, Setting, timeAgo, Toggle } from "@/ui";
import { plural } from "../util";
import { DeleteButton, ListState } from "./common";
import { useList } from "./useList";

const KINDS: Option<MemoryKind>[] = [
  { value: "fact", label: "Факт" },
  { value: "assumption", label: "Предположение" },
  { value: "temp", label: "На сутки" },
];

const KIND_HINTS: Record<MemoryKind, string> = {
  fact: "Факт: Jarvis на него опирается",
  assumption: "Предположение: Jarvis сначала уточнит, прежде чем на него опираться",
  temp: "Временная запись: забудется сама через сутки",
};

function kindLabel(m: MemoryItem): string {
  if (m.kind === "temp" && m.expires) {
    const d = new Date(m.expires * 1000);
    return `До ${d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
  }
  return m.kind === "assumption" ? "Предположение" : "Факт";
}

/** Новая или поправленная запись: та же запись (тот же id) заменяется, новая — сверху. */
function upsert(list: MemoryItem[] | null, item: MemoryItem): MemoryItem[] {
  return list?.some((x) => x.id === item.id) ? list.map((x) => (x.id === item.id ? item : x)) : [item, ...(list ?? [])];
}

function FactRow({
  item,
  fresh,
  onSave,
  onDelete,
}: {
  item: MemoryItem;
  fresh: boolean;
  onSave: (patch: { text?: string; kind?: MemoryKind }) => Promise<void>;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(item.text);
  const [kind, setKind] = useState<MemoryKind>(item.kind ?? "fact");
  const [busy, setBusy] = useState(false);
  const mine = item.source === "user";

  const start = () => {
    setText(item.text);
    setKind(item.kind ?? "fact");
    setEditing(true);
  };
  const save = async () => {
    const t = text.trim();
    if (!t) return;
    const patch: { text?: string; kind?: MemoryKind } = {};
    if (t !== item.text) patch.text = t;
    if (kind !== item.kind) patch.kind = kind;
    if (!patch.text && !patch.kind) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await onSave(patch);
      setEditing(false);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <li className="nt-fact editing">
        <span className="nt-fact-icon" aria-hidden>
          <Brain />
        </span>
        <input
          className="nt-inline-input"
          value={text}
          autoFocus
          maxLength={500}
          aria-label="Текст записи"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <Select value={kind} options={KINDS} onChange={setKind} width={150} />
        <IconButton icon={<CheckIcon />} label="Сохранить" size="sm" variant="primary" disabled={busy || !text.trim()} onClick={save} />
        <IconButton icon={<X />} label="Отмена" size="sm" variant="ghost" onClick={() => setEditing(false)} />
      </li>
    );
  }

  return (
    <li className={cx("nt-fact", fresh && "fresh", item.kind !== "fact" && "soft")}>
      <span className="nt-fact-icon" aria-hidden>
        <Brain />
      </span>
      <span className="grow nt-fact-text" onDoubleClick={start} title="Дважды щёлкните, чтобы поправить">
        {item.text}
      </span>
      <span className={cx("chip", item.kind === "fact" ? "" : "kind")} title={KIND_HINTS[item.kind ?? "fact"]}>
        {kindLabel(item)}
      </span>
      <span
        className={cx("chip", !mine && "accent")}
        title={mine ? "Добавлено или поправлено вами" : "Записал Jarvis, когда вы попросили запомнить"}
      >
        {mine ? "Вы" : "ИИ"}
      </span>
      <span className="faint tiny nt-fact-time">{timeAgo(item.created)}</span>
      <button type="button" className="nt-del nt-edit" aria-label="Поправить" title="Поправить" onClick={start}>
        <Pencil />
      </button>
      <DeleteButton onConfirm={onDelete} label="Забыть" />
    </li>
  );
}

export function MemoryTab() {
  const { items, setItems, error, loading, reload } = useList<MemoryItem>("/api/memory", []);
  const enabled = useApp((s) => s.settings?.ai.memory ?? true);
  const patchSettings = useApp((s) => s.patchSettings);
  const [text, setText] = useState("");
  const [kind, setKind] = useState<MemoryKind>("fact");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<string | null>(null);
  const [clearAsk, setClearAsk] = useState(false);

  useEffect(() => {
    const offAdd = onEvent<{ item: MemoryItem }>("memory.added", (e) => {
      const item = e.data.item;
      if (!item?.id) return;
      // тот же факт ядро не дублирует, а обновляет — заменяем запись
      setItems((cur) => upsert(cur, item));
      setFresh(item.id);
    });
    const offUpd = onEvent<{ item: MemoryItem }>("memory.updated", (e) => {
      const item = e.data.item;
      if (item?.id) setItems((cur) => upsert(cur, item));
    });
    const offDel = onEvent<{ id: string }>("memory.deleted", (e) =>
      setItems((cur) => cur?.filter((x) => x.id !== e.data.id) ?? null),
    );
    const offClear = onEvent("memory.cleared", () => setItems([]));
    return () => {
      offAdd();
      offUpd();
      offDel();
      offClear();
    };
  }, [setItems]);

  const list = useMemo(() => [...(items ?? [])].sort((a, b) => b.created - a.created), [items]);

  const add = async () => {
    const t = text.trim();
    if (!t) return;
    setBusy(true);
    try {
      const item = await api.post<MemoryItem>("/api/memory", { text: t, kind });
      if (item?.id) {
        setItems((cur) => upsert(cur, item));
        setFresh(item.id);
      }
      setText("");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  const save = async (m: MemoryItem, patch: { text?: string; kind?: MemoryKind }) => {
    const item = await api.patch<MemoryItem>(`/api/memory/${encodeURIComponent(m.id)}`, patch);
    if (item?.id) setItems((cur) => upsert(cur, item));
  };

  const remove = async (m: MemoryItem) => {
    setItems((cur) => cur?.filter((x) => x.id !== m.id) ?? null);
    try {
      await api.del(`/api/memory/${encodeURIComponent(m.id)}`);
    } catch (e) {
      setItems((cur) => [m, ...(cur ?? [])]);
      notifyError(e);
    }
  };

  const clearAll = async () => {
    setClearAsk(false);
    try {
      const res = await api.del<{ count: number }>("/api/memory");
      setItems([]);
      notify(`Память очищена: забыто ${res?.count ?? 0} ${plural(res?.count ?? 0, "запись", "записи", "записей")}`, "ok");
    } catch (e) {
      notifyError(e);
    }
  };

  return (
    <div className="nt-pane">
      <Setting
        name="Память Jarvis"
        desc="Jarvis запоминает, только когда вы попросите («запомни, что…»). Всё можно поправить или удалить"
      >
        <Toggle
          checked={enabled}
          label="Память Jarvis"
          onChange={(v) => patchSettings({ ai: { memory: v } }).catch(notifyError)}
        />
      </Setting>
      <form
        className="nt-add"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          className="input grow"
          value={text}
          placeholder="Например: «Я не ем мясо» или «Мой город — Казань»"
          aria-label="Новая запись"
          maxLength={500}
          onChange={(e) => setText(e.target.value)}
        />
        <Select value={kind} options={KINDS} onChange={setKind} width={160} />
        <Button type="submit" variant="primary" icon={<Plus />} loading={busy} disabled={!text.trim()}>
          Запомнить
        </Button>
      </form>
      <ListState
        loading={loading && !items}
        error={items ? "" : error}
        onRetry={reload}
        empty={
          items && !items.length ? (
            <>
              <Brain />
              <div>Jarvis пока ничего о вас не знает</div>
              <div className="faint small">Скажите «Джарвис, запомни, что я не ем мясо» — запись появится здесь</div>
            </>
          ) : null
        }
      >
        <div className="row nt-count">
          <span className="faint small">
            {list.length} {plural(list.length, "запись", "записи", "записей")} · дважды щёлкните, чтобы поправить
          </span>
          <span className="spacer" />
          {clearAsk ? (
            <>
              <Button size="sm" variant="danger" onClick={clearAll}>
                Да, забыть всё
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setClearAsk(false)}>
                Отмена
              </Button>
            </>
          ) : (
            <Button size="sm" variant="ghost" icon={<Trash2 />} onClick={() => setClearAsk(true)}>
              Очистить память
            </Button>
          )}
        </div>
        <ul className="nt-memory" aria-label="Память">
          {list.map((m) => (
            <FactRow
              key={m.id}
              item={m}
              fresh={fresh === m.id}
              onSave={(patch) => save(m, patch)}
              onDelete={() => remove(m)}
            />
          ))}
        </ul>
      </ListState>
    </div>
  );
}
