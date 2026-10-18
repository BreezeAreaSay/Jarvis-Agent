// Календарь: месяц сеткой с событиями, ближайшее событие, день — список и форма «добавить событие»
// со временем и «напомнить за N минут».
import { Bell, CalendarDays, CalendarPlus, ChevronLeft, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";
import { api, qs } from "@/api/client";
import type { EventItem } from "@/api/types";
import { Button, Check, cx, notifyError, Select } from "@/ui";
import { addDays, capitalize, fmtTime, plural, sameDay, startOfDay } from "../util";
import { DeleteButton, truthy } from "./common";
import { useList } from "./useList";

const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const REMIND = [
  { value: "none", label: "Не напоминать" },
  { value: "0", label: "В момент начала" },
  { value: "5", label: "За 5 минут" },
  { value: "10", label: "За 10 минут" },
  { value: "15", label: "За 15 минут" },
  { value: "30", label: "За 30 минут" },
  { value: "60", label: "За 1 час" },
  { value: "1440", label: "За 1 день" },
] as const;

type RemindValue = (typeof REMIND)[number]["value"];

function remindText(min: number | null): string {
  if (min === null || min === undefined) return "";
  if (min === 0) return "в момент начала";
  if (min % 1440 === 0) {
    const d = min / 1440;
    return d === 1 ? "за день" : `за ${d} ${plural(d, "день", "дня", "дней")}`;
  }
  if (min % 60 === 0) {
    const h = min / 60;
    return h === 1 ? "за час" : `за ${h} ${plural(h, "час", "часа", "часов")}`;
  }
  return `за ${min} мин`;
}

function monthGrid(month: Date): Date[] {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const shift = (first.getDay() + 6) % 7; // понедельник — первый
  const start = addDays(first, -shift);
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

function AddEvent({ day, onAdded }: { day: Date; onAdded: (e: EventItem) => void }) {
  const [title, setTitle] = useState("");
  const [time, setTime] = useState("10:00");
  const [allDay, setAllDay] = useState(false);
  const [remind, setRemind] = useState<RemindValue>("15");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    const t = title.trim();
    if (!t) return;
    const [h, m] = (time || "10:00").split(":").map(Number);
    const start = allDay
      ? new Date(day.getFullYear(), day.getMonth(), day.getDate())
      : new Date(day.getFullYear(), day.getMonth(), day.getDate(), h || 0, m || 0);
    setBusy(true);
    try {
      const item = await api.post<EventItem>("/api/events", {
        title: t,
        start: start.getTime() / 1000,
        end: null,
        all_day: allDay,
        notes: "",
        remind_min: remind === "none" ? null : Number(remind),
      });
      onAdded(item);
      setTitle("");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="nt-cal-add"
      onSubmit={(e) => {
        e.preventDefault();
        add();
      }}
    >
      <div className="nt-cal-add-title">
        <CalendarPlus /> Новое событие
      </div>
      <input
        className="input"
        value={title}
        placeholder="Что запланировано?"
        aria-label="Название события"
        onChange={(e) => setTitle(e.target.value)}
      />
      <div className="row nt-cal-add-row">
        <input
          type="time"
          className="input nt-time"
          value={time}
          disabled={allDay}
          aria-label="Время"
          onChange={(e) => setTime(e.target.value)}
        />
        <Check checked={allDay} onChange={setAllDay} chip>
          Весь день
        </Check>
      </div>
      <div className="row nt-cal-add-row">
        <Select<RemindValue>
          value={remind}
          options={REMIND.map((r) => ({ value: r.value, label: r.label }))}
          onChange={setRemind}
          up
        />
        <Button type="submit" variant="primary" loading={busy} disabled={!title.trim()}>
          Добавить
        </Button>
      </div>
    </form>
  );
}

export function CalendarTab() {
  const today = startOfDay(new Date());
  const [month, setMonth] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const [selected, setSelected] = useState(today);
  const days = useMemo(() => monthGrid(month), [month]);
  const range = useMemo(
    () => ({ start: days[0].getTime() / 1000, end: addDays(days[41], 1).getTime() / 1000 }),
    [days],
  );
  const { items, setItems, error, loading, reload } = useList<EventItem>(`/api/events${qs(range)}`, ["events.changed"]);

  const byDay = useMemo(() => {
    const map = new Map<string, EventItem[]>();
    for (const e of items ?? []) {
      const k = startOfDay(new Date(e.start * 1000)).toDateString();
      map.set(k, [...(map.get(k) ?? []), e]);
    }
    for (const list of map.values()) list.sort((a, b) => Number(truthy(b.all_day)) - Number(truthy(a.all_day)) || a.start - b.start);
    return map;
  }, [items]);

  const next = useMemo(() => {
    const now = Date.now() / 1000;
    return [...(items ?? [])]
      .filter((e) => (truthy(e.all_day) ? addDays(new Date(e.start * 1000), 1).getTime() / 1000 : e.start) >= now)
      .sort((a, b) => a.start - b.start)[0];
  }, [items]);

  const dayEvents = byDay.get(selected.toDateString()) ?? [];
  const monthTitle = capitalize(month.toLocaleDateString("ru-RU", { month: "long", year: "numeric" }).replace(/\s*г\.$/, ""));

  const go = (delta: number) => setMonth((m) => new Date(m.getFullYear(), m.getMonth() + delta, 1));

  const remove = async (ev: EventItem) => {
    setItems((cur) => cur?.filter((x) => x.id !== ev.id) ?? null);
    try {
      await api.del(`/api/events/${encodeURIComponent(ev.id)}`);
    } catch (e) {
      setItems((cur) => [...(cur ?? []), ev]);
      notifyError(e);
    }
  };

  return (
    <div className="nt-pane nt-cal">
      <div className="nt-cal-main">
        <div className="nt-cal-head">
          <CalendarDays className="accent-icon" />
          <h3>{monthTitle}</h3>
          <div className="row" style={{ gap: 4 }}>
            <button type="button" className="icon-btn ghost sm" aria-label="Предыдущий месяц" onClick={() => go(-1)}>
              <ChevronLeft />
            </button>
            <button type="button" className="icon-btn ghost sm" aria-label="Следующий месяц" onClick={() => go(1)}>
              <ChevronRight />
            </button>
          </div>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setMonth(new Date(today.getFullYear(), today.getMonth(), 1));
              setSelected(today);
            }}
          >
            Сегодня
          </Button>
          <span className="spacer" />
          {next && (
            <button
              type="button"
              className="nt-next"
              title="Ближайшее событие"
              onClick={() => setSelected(startOfDay(new Date(next.start * 1000)))}
            >
              <span className="ellipsis">
                {new Date(next.start * 1000).getDate()} · {next.title}
              </span>
            </button>
          )}
          {loading && items && <span className="spinner" aria-label="Обновляю" />}
        </div>
        {error && !items ? (
          <div className="nt-state err">
            Не удалось загрузить события: {error}
            <Button size="sm" onClick={reload}>
              Повторить
            </Button>
          </div>
        ) : (
          <div className="nt-cal-grid" role="grid" aria-label={monthTitle}>
            {WEEKDAYS.map((w) => (
              <div key={w} className="nt-wd" role="columnheader">
                {w}
              </div>
            ))}
            {days.map((d) => {
              const evs = byDay.get(d.toDateString()) ?? [];
              const out = d.getMonth() !== month.getMonth();
              return (
                <button
                  key={d.toISOString()}
                  type="button"
                  role="gridcell"
                  aria-selected={sameDay(d, selected)}
                  aria-label={`${d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" })}${evs.length ? `, событий: ${evs.length}` : ""}`}
                  className={cx(
                    "nt-day",
                    out && "out",
                    sameDay(d, today) && "today",
                    sameDay(d, selected) && "selected",
                    (d.getDay() === 0 || d.getDay() === 6) && "weekend",
                  )}
                  onClick={() => {
                    setSelected(d);
                    if (out) setMonth(new Date(d.getFullYear(), d.getMonth(), 1));
                  }}
                >
                  <span className="num">{d.getDate()}</span>
                  {evs.slice(0, 2).map((e) => (
                    <span key={e.id} className={cx("nt-day-ev", truthy(e.all_day) && "allday")}>
                      {e.title}
                    </span>
                  ))}
                  {evs.length > 2 && <span className="nt-day-more">ещё {evs.length - 2}</span>}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <aside className="nt-cal-side">
        <h4 className="nt-cal-day-title">
          {capitalize(selected.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" }))}
        </h4>
        <div className="nt-cal-events">
          {dayEvents.length === 0 && <div className="faint small nt-cal-none">Событий нет</div>}
          {dayEvents.map((e) => (
            <div key={e.id} className="nt-event">
              <span className="nt-event-time">{truthy(e.all_day) ? "весь день" : fmtTime(e.start)}</span>
              <span className="grow">
                <span className="nt-event-title">{e.title}</span>
                {e.remind_min !== null && e.remind_min !== undefined && (
                  <span className="nt-event-remind">
                    <Bell /> {remindText(e.remind_min)}
                  </span>
                )}
                {e.notes && <span className="nt-event-notes">{e.notes}</span>}
              </span>
              <DeleteButton onConfirm={() => remove(e)} label="Удалить событие" />
            </div>
          ))}
        </div>
        <AddEvent
          day={selected}
          onAdded={(item) => setItems((cur) => (cur?.some((x) => x.id === item.id) ? cur : [...(cur ?? []), item]))}
        />
      </aside>
    </div>
  );
}
