// Напоминания: ближайшие и прошедшие, добавить (текст, дата и время, повтор), удалить.
import { Bell, BellRing, ChevronDown, Plus, Repeat } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { ReminderItem } from "@/api/types";
import { Button, cx, notifyError, Select } from "@/ui";
import { fmtRelative, fmtWhen, plural, toLocalInput } from "../util";
import { DeleteButton, ListState, truthy } from "./common";
import { useList } from "./useList";

type RepeatValue = "once" | "daily" | "weekly";

const REPEAT: { value: RepeatValue; label: string }[] = [
  { value: "once", label: "Один раз" },
  { value: "daily", label: "Каждый день" },
  { value: "weekly", label: "Каждую неделю" },
];

const REPEAT_TEXT: Record<string, string> = { daily: "каждый день", weekly: "каждую неделю" };

function defaultAt(): string {
  const d = new Date(Date.now() + 60 * 60 * 1000);
  d.setMinutes(d.getMinutes() < 30 ? 30 : 60, 0, 0);
  return toLocalInput(d);
}

function ReminderRow({ r, past, onDelete }: { r: ReminderItem; past: boolean; onDelete: () => void }) {
  return (
    <li className={cx("nt-rem", past && "past")}>
      <span className="nt-rem-icon" aria-hidden>
        {past ? <Bell /> : <BellRing />}
      </span>
      <span className="grow">
        <span className="nt-rem-text">{r.text}</span>
        <span className="nt-rem-when">
          {fmtWhen(r.at)}
          {!past && <span className="faint"> · {fmtRelative(r.at)}</span>}
        </span>
      </span>
      {r.repeat && REPEAT_TEXT[r.repeat] && (
        <span className="chip">
          <Repeat /> {REPEAT_TEXT[r.repeat]}
        </span>
      )}
      <DeleteButton onConfirm={onDelete} label="Удалить напоминание" />
    </li>
  );
}

export function RemindersTab() {
  const { items, setItems, error, loading, reload } = useList<ReminderItem>("/api/reminders", [
    "reminders.changed",
    "reminder.fired",
  ]);
  const [text, setText] = useState("");
  const [at, setAt] = useState(defaultAt);
  const [repeat, setRepeat] = useState<RepeatValue>("once");
  const [busy, setBusy] = useState(false);
  const [showPast, setShowPast] = useState(false);
  const [, tick] = useState(0);

  // «через 5 мин» обновляется само
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => onEvent("reminder.fired", () => tick((n) => n + 1)), []);

  const now = Date.now() / 1000;
  const upcoming = useMemo(
    () => (items ?? []).filter((r) => !truthy(r.done) && (r.at >= now || r.repeat)).sort((a, b) => a.at - b.at),
    [items, now],
  );
  const past = useMemo(
    () => (items ?? []).filter((r) => !upcoming.includes(r)).sort((a, b) => b.at - a.at),
    [items, upcoming],
  );

  const add = async () => {
    const t = text.trim();
    const when = new Date(at);
    if (!t || Number.isNaN(when.getTime())) return;
    if (when.getTime() < Date.now() - 60_000 && repeat === "once") {
      notifyError(new Error("Это время уже прошло — выберите позже"));
      return;
    }
    setBusy(true);
    try {
      const item = await api.post<ReminderItem>("/api/reminders", {
        text: t,
        at: when.getTime() / 1000,
        repeat: repeat === "once" ? null : repeat,
      });
      if (item?.id) setItems((cur) => (cur?.some((x) => x.id === item.id) ? cur : [...(cur ?? []), item]));
      setText("");
      setAt(defaultAt());
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (r: ReminderItem) => {
    setItems((cur) => cur?.filter((x) => x.id !== r.id) ?? null);
    try {
      await api.del(`/api/reminders/${encodeURIComponent(r.id)}`);
    } catch (e) {
      setItems((cur) => [...(cur ?? []), r]);
      notifyError(e);
    }
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
          value={text}
          placeholder="Что напомнить? Например, «Выпить воды»"
          aria-label="Текст напоминания"
          onChange={(e) => setText(e.target.value)}
        />
        <input
          type="datetime-local"
          className="input nt-datetime"
          value={at}
          aria-label="Когда"
          onChange={(e) => setAt(e.target.value)}
        />
        <Select<RepeatValue> value={repeat} options={REPEAT} onChange={setRepeat} width={160} />
        <Button type="submit" variant="primary" icon={<Plus />} loading={busy} disabled={!text.trim() || !at}>
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
              <Bell />
              <div>Напоминаний нет</div>
              <div className="faint small">Скажите «Джарвис, напомни через час выпить воды»</div>
            </>
          ) : null
        }
      >
        {upcoming.length > 0 ? (
          <ul className="nt-rems" aria-label="Ближайшие напоминания">
            {upcoming.map((r) => (
              <ReminderRow key={r.id} r={r} past={false} onDelete={() => remove(r)} />
            ))}
          </ul>
        ) : (
          <div className="nt-empty small">Ближайших напоминаний нет</div>
        )}
        {past.length > 0 && (
          <div className="nt-done">
            <button type="button" className="nt-section-toggle" onClick={() => setShowPast(!showPast)} aria-expanded={showPast}>
              <ChevronDown className={cx(showPast && "open")} />
              Прошедшие · {past.length} {plural(past.length, "напоминание", "напоминания", "напоминаний")}
            </button>
            {showPast && (
              <ul className="nt-rems">
                {past.map((r) => (
                  <ReminderRow key={r.id} r={r} past onDelete={() => remove(r)} />
                ))}
              </ul>
            )}
          </div>
        )}
      </ListState>
    </div>
  );
}
