// Вкладки «Заметки и память» (#/ai/notes/<tab>).
import { Bell, Brain, CalendarDays, ListChecks, StickyNote } from "lucide-react";
import type { ReactNode } from "react";

export type NoteTab = "notes" | "tasks" | "calendar" | "memory" | "reminders";

export const NOTE_TABS: { value: NoteTab; label: string; icon: ReactNode }[] = [
  { value: "notes", label: "Заметки", icon: <StickyNote /> },
  { value: "tasks", label: "Задачи", icon: <ListChecks /> },
  { value: "calendar", label: "Календарь", icon: <CalendarDays /> },
  { value: "memory", label: "Память", icon: <Brain /> },
  { value: "reminders", label: "Напоминания", icon: <Bell /> },
];
