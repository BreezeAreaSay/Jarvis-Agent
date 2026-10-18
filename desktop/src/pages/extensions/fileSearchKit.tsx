// «Поиск по ПК»: общее для живой панели на странице «Расширения» и для оверлея — типы файлов (значок, цвет,
// подпись), фильтры, размер и дата по-человечески, подпись «где лежит · когда · сколько весит».
import {
  File,
  FileArchive,
  FileImage,
  FileMusic,
  FileSpreadsheet,
  FileText,
  FileVideo,
  Folder,
  type LucideIcon,
  Presentation,
} from "lucide-react";
import type { CSSProperties } from "react";
import type { FileKind, FoundFile } from "@/api/types";
import { cx, timeAgo } from "@/ui";

export const FILE_SEARCH = "file_search";

// цвет типа — как у значков Проводника и Office: документ синий, таблица зелёная, презентация оранжевая
export const KIND_META: Record<FileKind, { label: string; Icon: LucideIcon; color: string }> = {
  document: { label: "Документы", Icon: FileText, color: "#60A5FA" },
  spreadsheet: { label: "Таблицы", Icon: FileSpreadsheet, color: "#34D399" },
  presentation: { label: "Презентации", Icon: Presentation, color: "#FB923C" },
  pdf: { label: "PDF", Icon: FileText, color: "#F87171" },
  image: { label: "Фото", Icon: FileImage, color: "#F472B6" },
  video: { label: "Видео", Icon: FileVideo, color: "#A78BFA" },
  audio: { label: "Музыка", Icon: FileMusic, color: "#2DD4BF" },
  archive: { label: "Архивы", Icon: FileArchive, color: "#FBBF24" },
  folder: { label: "Папки", Icon: Folder, color: "#93C5FD" },
  other: { label: "Файлы", Icon: File, color: "#94A3B8" },
};

/** Чипы фильтра по типу (ключи — как у ядра: kinds в действии search). */
export const KIND_FILTERS: FileKind[] = [
  "document",
  "spreadsheet",
  "presentation",
  "pdf",
  "image",
  "video",
  "audio",
  "archive",
  "folder",
];

/** Чипы фильтра по дате (ключи периода ядра: date в действии search). */
export const DATE_FILTERS: { id: string; label: string }[] = [
  { id: "", label: "Любое время" },
  { id: "today", label: "Сегодня" },
  { id: "yesterday", label: "Вчера" },
  { id: "7d", label: "Неделя" },
  { id: "30d", label: "Месяц" },
];

export function kindMeta(kind: string) {
  return KIND_META[kind as FileKind] ?? KIND_META.other;
}

/** 5 600 000 → «5,3 МБ»; у папок и пустых — "". */
export function fmtSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  const digits = i === 0 || value >= 10 ? 0 : 1;
  return `${value.toLocaleString("ru-RU", { maximumFractionDigits: digits })} ${units[i]}`;
}

/** «Документы › Проекты · вчера · 2,4 МБ». */
export function fileMeta(f: FoundFile, withWhere = true): string {
  const parts = [withWhere ? f.where : "", f.modified ? timeAgo(f.modified) : "", fmtSize(f.size)];
  return parts.filter(Boolean).join(" · ");
}

/** Значок типа файла в цветном квадрате. */
export function FileKindIcon({ kind, size = 34, className }: { kind: string; size?: number; className?: string }) {
  const meta = kindMeta(kind);
  const Icon = meta.Icon;
  return (
    <span className={cx("fs-kind", className)} style={{ "--k": meta.color, "--s": `${size}px` } as CSSProperties} aria-hidden>
      <Icon />
    </span>
  );
}
