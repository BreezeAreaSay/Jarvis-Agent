// Задачи: добавить (со сроком), отметить, переименовать двойным щелчком, срок, удалить, порядок
// перетаскиванием. Выполненные — отдельным свёрнутым списком.
import { CalendarClock, ChevronDown, GripVertical, ListChecks, Plus } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { TaskItem } from "@/api/types";
import { Button, Check, cx, notifyError } from "@/ui";
import { fmtDayName, fromDateInput, plural, startOfDay, toDateInput } from "../util";
import { DeleteButton, ListState, truthy } from "./common";
import { useList } from "./useList";

function dueInfo(due: number | null): { text: string; tone: "" | "warn" | "err" } | null {
  if (!due) return null;
  const d = startOfDay(new Date(due * 1000)).getTime();
  const today = startOfDay(new Date()).getTime();
  if (d < today) return { text: `Просрочено · ${fmtDayName(due)}`, tone: "err" };
  if (d === today) return { text: "Сегодня", tone: "warn" };
  return { text: fmtDayName(due).replace(/^./, (c) => c.toUpperCase()), tone: "" };
}

/** Кнопка-срок: по щелчку открывает выбор даты. */
function DueButton({ due, onChange }: { due: number | null; onChange: (due: number | null) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const info = dueInfo(due);
  return (
    <span className="nt-due-wrap">
      <button
        type="button"
        className={cx("nt-due", info?.tone, !info && "empty")}
        title={info ? "Изменить срок" : "Добавить срок"}
        aria-label={info ? `Срок: ${info.text}. Изменить` : "Добавить срок"}
        onClick={(e) => {
          e.stopPropagation();
          const el = input.current;
          if (!el) return;
          try {
            el.showPicker();
          } catch {
            el.focus();
          }
        }}
      >
        <CalendarClock />
        {info && <span>{info.text}</span>}
      </button>
      <input
        ref={input}
        type="date"
        className="nt-hidden-date"
        tabIndex={-1}
        aria-hidden
        value={due ? toDateInput(new Date(due * 1000)) : ""}
        onChange={(e) => {
          const d = fromDateInput(e.target.value);
          onChange(d ? new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12).getTime() / 1000 : null);
        }}
      />
    </span>
  );
}

function TaskRow({
  task,
  onPatch,
  onDelete,
  drag,
}: {
  task: TaskItem;
  onPatch: (p: Partial<TaskItem>) => void;
  onDelete: () => void;
  drag?: {
    onDragStart: (e: React.DragEvent) => void;
    onDragOver: (e: React.DragEvent) => void;
    onDrop: (e: React.DragEvent) => void;
    onDragEnd: () => void;
    marker: "before" | "after" | null;
    dragging: boolean;
  };
}) {
  const done = truthy(task.done);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const commit = () => {
    setEditing(false);
    const t = title.trim();
    if (t && t !== task.title) onPatch({ title: t });
    else setTitle(task.title);
  };
  return (
    <li
      className={cx("nt-task", done && "done", drag?.dragging && "dragging", drag?.marker && `drop-${drag.marker}`)}
      draggable={!!drag && !editing}
      onDragStart={drag?.onDragStart}
      onDragOver={drag?.onDragOver}
      onDrop={drag?.onDrop}
      onDragEnd={drag?.onDragEnd}
    >
      {drag && <GripVertical className="nt-grip" aria-hidden />}
      <Check checked={done} onChange={(v) => onPatch({ done: v })}>
        <span className="ai-sr">{done ? "Вернуть в работу" : "Отметить выполненной"}</span>
      </Check>
      {editing ? (
        <input
          className="nt-inline-input"
          value={title}
          autoFocus
          aria-label="Название задачи"
          onChange={(e) => setTitle(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setTitle(task.title);
              setEditing(false);
            }
          }}
        />
      ) : (
        <span className="nt-task-title" onDoubleClick={() => !done && setEditing(true)} title="Дважды щёлкните, чтобы переименовать">
          {task.title}
        </span>
      )}
      <span className="spacer" />
      {!done && <DueButton due={task.due} onChange={(due) => onPatch({ due })} />}
      <DeleteButton onConfirm={onDelete} label="Удалить задачу" />
    </li>
  );
}

export function TasksTab() {
  const { items, setItems, error, loading, reload } = useList<TaskItem>("/api/tasks", ["tasks.changed"]);
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [adding, setAdding] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<{ id: string; pos: "before" | "after" } | null>(null);

  const active = useMemo(
    () => (items ?? []).filter((t) => !truthy(t.done)).sort((a, b) => a.position - b.position || a.created - b.created),
    [items],
  );
  const done = useMemo(
    () => (items ?? []).filter((t) => truthy(t.done)).sort((a, b) => (b.completed ?? 0) - (a.completed ?? 0)),
    [items],
  );

  const upsert = (t: TaskItem) => setItems((cur) => cur?.map((x) => (x.id === t.id ? t : x)) ?? null);

  const add = async () => {
    const t = title.trim();
    if (!t) return;
    setAdding(true);
    const d = fromDateInput(due);
    try {
      const item = await api.post<TaskItem>("/api/tasks", {
        title: t,
        done: false,
        due: d ? new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12).getTime() / 1000 : null,
      });
      setItems((cur) => (cur?.some((x) => x.id === item.id) ? cur : [...(cur ?? []), item]));
      setTitle("");
      setDue("");
    } catch (e) {
      notifyError(e);
    } finally {
      setAdding(false);
    }
  };

  const patch = async (task: TaskItem, p: Partial<TaskItem>) => {
    const optimistic = { ...task, ...p, ...(p.done !== undefined ? { completed: p.done ? Date.now() / 1000 : null } : {}) };
    upsert(optimistic);
    try {
      upsert(await api.patch<TaskItem>(`/api/tasks/${encodeURIComponent(task.id)}`, p));
    } catch (e) {
      upsert(task);
      notifyError(e);
    }
  };

  const remove = async (task: TaskItem) => {
    setItems((cur) => cur?.filter((x) => x.id !== task.id) ?? null);
    try {
      await api.del(`/api/tasks/${encodeURIComponent(task.id)}`);
    } catch (e) {
      setItems((cur) => [...(cur ?? []), task]);
      notifyError(e);
    }
  };

  const drop = (targetId: string, pos: "before" | "after") => {
    if (!dragId || dragId === targetId) return;
    const order = active.filter((t) => t.id !== dragId);
    const moved = active.find((t) => t.id === dragId);
    if (!moved) return;
    const i = order.findIndex((t) => t.id === targetId);
    order.splice(pos === "before" ? i : i + 1, 0, moved);
    const changed = order.map((t, index) => ({ t, index })).filter(({ t, index }) => t.position !== index);
    setItems((cur) => cur?.map((x) => {
      const j = order.findIndex((t) => t.id === x.id);
      return j >= 0 ? { ...x, position: j } : x;
    }) ?? null);
    Promise.all(
      changed.map(({ t, index }) => api.patch(`/api/tasks/${encodeURIComponent(t.id)}`, { position: index })),
    ).catch(() => reload()); // порядок не сохранился — показать, как на самом деле
  };

  return (
    <div className="nt-pane">
      <form
        className="nt-add"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <input
          className="input grow"
          value={title}
          placeholder="Новая задача — например, «Купить корм коту»"
          aria-label="Новая задача"
          onChange={(e) => setTitle(e.target.value)}
        />
        <input
          type="date"
          className="input nt-date"
          value={due}
          aria-label="Срок"
          title="Срок (необязательно)"
          onChange={(e) => setDue(e.target.value)}
        />
        <Button type="submit" variant="primary" icon={<Plus />} loading={adding} disabled={!title.trim()}>
          Добавить
        </Button>
      </form>
      <ListState
        loading={loading && !items}
        error={items ? "" : error}
        onRetry={reload}
        empty={
          items && !items.length ? (
            <>
              <ListChecks />
              <div>Задач нет — можно отдыхать</div>
              <div className="faint small">Или скажите «Джарвис, добавь задачу позвонить маме»</div>
            </>
          ) : null
        }
      >
        {active.length > 0 ? (
          <ul className="nt-tasks" aria-label="Задачи">
            {active.map((t) => (
              <TaskRow
                key={t.id}
                task={t}
                onPatch={(p) => patch(t, p)}
                onDelete={() => remove(t)}
                drag={
                  active.length > 1
                    ? {
                        dragging: dragId === t.id,
                        marker: over?.id === t.id && dragId !== t.id ? over.pos : null,
                        onDragStart: (e) => {
                          setDragId(t.id);
                          e.dataTransfer.effectAllowed = "move";
                          e.dataTransfer.setData("text/plain", t.title);
                        },
                        onDragOver: (e) => {
                          if (!dragId) return;
                          e.preventDefault();
                          const r = e.currentTarget.getBoundingClientRect();
                          const pos = e.clientY < r.top + r.height / 2 ? "before" : "after";
                          if (over?.id !== t.id || over.pos !== pos) setOver({ id: t.id, pos });
                        },
                        onDrop: (e) => {
                          e.preventDefault();
                          if (over) drop(t.id, over.pos);
                          setOver(null);
                          setDragId(null);
                        },
                        onDragEnd: () => {
                          setOver(null);
                          setDragId(null);
                        },
                      }
                    : undefined
                }
              />
            ))}
          </ul>
        ) : (
          <div className="nt-empty small">Все задачи выполнены</div>
        )}
        {done.length > 0 && (
          <div className="nt-done">
            <button type="button" className="nt-section-toggle" onClick={() => setShowDone(!showDone)} aria-expanded={showDone}>
              <ChevronDown className={cx(showDone && "open")} />
              Выполнено · {done.length} {plural(done.length, "задача", "задачи", "задач")}
            </button>
            {showDone && (
              <ul className="nt-tasks">
                {done.map((t) => (
                  <TaskRow key={t.id} task={t} onPatch={(p) => patch(t, p)} onDelete={() => remove(t)} />
                ))}
              </ul>
            )}
          </div>
        )}
      </ListState>
    </div>
  );
}
