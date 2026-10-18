// «Заметки и память»: вкладки [Заметки] [Задачи] [Календарь] [Память] [Напоминания].
import { navigate } from "@/lib/router";
import { cx } from "@/ui";
import { CalendarTab } from "./CalendarTab";
import { MemoryTab } from "./MemoryTab";
import { NotesTab } from "./NotesTab";
import { RemindersTab } from "./RemindersTab";
import { NOTE_TABS, type NoteTab } from "./tabs";
import { TasksTab } from "./TasksTab";

export function NotesView({ tab }: { tab: NoteTab }) {
  return (
    <section className="nt-shell" aria-label="Заметки и память">
      <div className="tabs nt-tabs" role="tablist" aria-label="Разделы">
        {NOTE_TABS.map((t) => (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={tab === t.value}
            className={cx("tab", tab === t.value && "active")}
            onClick={() => navigate("ai", "notes", t.value)}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>
      <div className="nt-body" role="tabpanel">
        {tab === "notes" && <NotesTab />}
        {tab === "tasks" && <TasksTab />}
        {tab === "calendar" && <CalendarTab />}
        {tab === "memory" && <MemoryTab />}
        {tab === "reminders" && <RemindersTab />}
      </div>
    </section>
  );
}
