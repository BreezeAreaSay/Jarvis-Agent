// Память: что Jarvis знает о человеке. Факты с пометкой «ИИ» (запомнил сам) или «Вы» (добавлено вручную),
// добавить факт, удалить, выключатель «Запоминать факты из разговоров».
import { Brain, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { MemoryItem } from "@/api/types";
import { useApp } from "@/store/app";
import { Button, cx, notifyError, Setting, timeAgo, Toggle } from "@/ui";
import { plural } from "../util";
import { DeleteButton, ListState } from "./common";
import { useList } from "./useList";

export function MemoryTab() {
  const { items, setItems, error, loading, reload } = useList<MemoryItem>("/api/memory", []);
  const enabled = useApp((s) => s.settings?.ai.memory ?? true);
  const patchSettings = useApp((s) => s.patchSettings);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<string | null>(null);

  useEffect(() => {
    const offAdd = onEvent<{ item: MemoryItem }>("memory.added", (e) => {
      const item = e.data.item;
      if (!item?.id) return;
      // тот же факт ядро не дублирует, а обновляет — заменяем запись
      setItems((cur) =>
        cur?.some((x) => x.id === item.id) ? cur.map((x) => (x.id === item.id ? item : x)) : [item, ...(cur ?? [])],
      );
      setFresh(item.id);
    });
    const offDel = onEvent<{ id: string }>("memory.deleted", (e) =>
      setItems((cur) => cur?.filter((x) => x.id !== e.data.id) ?? null),
    );
    return () => {
      offAdd();
      offDel();
    };
  }, [setItems]);

  const list = useMemo(() => [...(items ?? [])].sort((a, b) => b.created - a.created), [items]);

  const add = async () => {
    const t = text.trim();
    if (!t) return;
    setBusy(true);
    try {
      const item = await api.post<MemoryItem>("/api/memory", { text: t });
      if (item?.id) {
        setItems((cur) =>
          cur?.some((x) => x.id === item.id) ? cur.map((x) => (x.id === item.id ? item : x)) : [item, ...(cur ?? [])],
        );
        setFresh(item.id);
      }
      setText("");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
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

  return (
    <div className="nt-pane">
      <Setting
        name="Запоминать факты из разговоров"
        desc="Jarvis сам отмечает важное: имя, привычки, предпочтения — и учитывает это в ответах"
      >
        <Toggle
          checked={enabled}
          label="Запоминать факты из разговоров"
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
          aria-label="Новый факт"
          maxLength={500}
          onChange={(e) => setText(e.target.value)}
        />
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
              <div className="faint small">Скажите «Джарвис, запомни, что я не ем мясо» — факт появится здесь</div>
            </>
          ) : null
        }
      >
        <div className="faint small nt-count">
          {list.length} {plural(list.length, "факт", "факта", "фактов")}
        </div>
        <ul className="nt-memory" aria-label="Факты">
          {list.map((m) => {
            const ai = m.source !== "user";
            return (
              <li key={m.id} className={cx("nt-fact", fresh === m.id && "fresh")}>
                <span className="nt-fact-icon" aria-hidden>
                  <Brain />
                </span>
                <span className="grow nt-fact-text">{m.text}</span>
                <span className={cx("chip", ai && "accent")} title={ai ? "Запомнил сам из разговора" : "Добавлено вами"}>
                  {ai ? "ИИ" : "Вы"}
                </span>
                <span className="faint tiny nt-fact-time">{timeAgo(m.created)}</span>
                <DeleteButton onConfirm={() => remove(m)} label="Забыть" />
              </li>
            );
          })}
        </ul>
      </ListState>
    </div>
  );
}
