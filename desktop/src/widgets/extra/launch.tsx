// Запуск: «Программы» (плитки закреплённых программ), «Быстрые команды» (кнопки-фразы и сайты),
// «Недавние» (последние файлы со временем).
import {
  AppWindow,
  Check,
  CircleAlert,
  File as FileIcon,
  FileArchive,
  FileClock,
  FileCode,
  FileImage,
  FileMusic,
  FileSpreadsheet,
  FileText,
  FileVideoCamera,
  Plus,
  Presentation,
  Search,
  X,
  Zap,
} from "lucide-react";
import { type CSSProperties, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { ExecResult } from "@/api/types";
import { AppIcon, resolveIcon } from "@/components/AppIcon";
import { Button, cx, IconButton, notify, notifyError, TextInput } from "@/ui";
import { useWidgetData, WidgetFrame, type WidgetProps } from "../common";
import { CfgButton, shortWhen, useBox, WidgetSettingsModal } from "./shared";

// --- значок программы без лишней рамки -----------------------------------------------------------------
const LETTER_COLORS = ["#60A5FA", "#A78BFA", "#F472B6", "#34D399", "#FBBF24", "#F87171", "#22D3EE", "#FB923C"];

export function AppGlyph({ name, size }: { name: string; size: number }) {
  const r = resolveIcon(name);
  if (r.brand) {
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden>
        <path d={r.brand.path} fill={r.brand.hex} />
      </svg>
    );
  }
  if (r.tile || r.minecraft) return <AppIcon name={name} size={size} />;
  // неизвестная программа — буква в цветном кружке
  const letter = (name.trim()[0] ?? "?").toUpperCase();
  const color = LETTER_COLORS[[...name].reduce((n, ch) => n + ch.charCodeAt(0), 0) % LETTER_COLORS.length];
  return (
    <span
      className="wx-letter"
      style={{ width: size, height: size, fontSize: size * 0.48, background: `color-mix(in srgb, ${color} 24%, transparent)`, color }}
    >
      {letter}
    </span>
  );
}

// --- программы ------------------------------------------------------------------------------------------
/** Раскладка плиток, заполняющая виджет: плитки чуть шире, чем выше (как у Luxify), без лишних пустот. */
function appGrid(n: number, W: number, H: number): { cols: number; rows: number; h: number; icon: number } {
  const gap = 8;
  const MIN = 40;
  let best = { cols: 1, rows: 1, h: Math.max(MIN, H), icon: 26, score: Infinity };
  if (!n || W <= 0 || H <= 0) return best;
  const maxRows = Math.max(1, Math.floor((H + gap) / (MIN + gap)));
  const maxCols = Math.max(1, Math.floor((W + gap) / (MIN + gap)));
  for (let rows = 1; rows <= maxRows; rows++) {
    for (let cols = 1; cols <= maxCols; cols++) {
      const fits = Math.min(n, cols * rows);
      if (rows > 1 && (rows - 1) * cols >= n) continue; // лишняя пустая строка
      const w = (W - gap * (cols - 1)) / cols;
      const h = (H - gap * (rows - 1)) / rows;
      if (w < MIN || h < MIN) continue;
      const ratio = Math.abs(Math.log(w / h / 1.3));
      const empty = (cols * rows - fits) / (cols * rows);
      const hidden = (n - fits) / n;
      const big = Math.max(0, Math.min(w, h) - 96) / 60; // не раздувать плитки
      const score = ratio + empty * 1.2 + hidden * 3 + big;
      if (score < best.score) best = { cols, rows, h, icon: Math.round(Math.min(40, Math.max(20, Math.min(w, h) * 0.46))), score };
    }
  }
  return best;
}

interface AppItem {
  name: string;
  app_id: string;
  path?: string;
  source?: string;
  icon?: string;
  missing?: boolean;
}

export function AppsWidget({ config, setConfig }: WidgetProps) {
  const pinned = Array.isArray(config.pinned) ? (config.pinned as string[]) : [];
  const { data, error } = useWidgetData<{ pinned?: AppItem[]; items?: AppItem[]; error?: string }>(
    "apps",
    { pinned: pinned.join(",") || undefined },
    {},
  );
  const [ref, box] = useBox<HTMLDivElement>();
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const apps = data?.pinned ?? [];
  const grid = useMemo(() => appGrid(apps.length, box.width, box.height), [apps.length, box.width, box.height]);
  const shown = box.width ? apps.slice(0, grid.cols * grid.rows) : [];

  const launch = async (a: AppItem) => {
    if (a.missing) {
      notify(`Программа «${a.name}» не найдена — выберите другую в настройках виджета`, "error");
      return;
    }
    setBusy(a.name);
    try {
      await api.post("/api/widgets/apps/launch", { app_id: a.app_id, name: a.name });
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
        await api.post("/api/dispatch", { text: `открой ${a.name}`, source: "ui" }).catch(notifyError);
      } else notifyError(e);
    } finally {
      setTimeout(() => setBusy(""), 900);
    }
  };

  const all = data?.items ?? [];
  const filtered = useMemo(
    () => all.filter((a) => a.name.toLowerCase().includes(q.trim().toLowerCase())).slice(0, 80),
    [all, q],
  );

  return (
    <WidgetFrame
      icon={<AppWindow />}
      title={(config.title as string) || "Программы"}
      actions={
        <CfgButton
          onClick={() => {
            setPick(pinned.length ? pinned : apps.filter((a) => !a.missing).map((a) => a.name));
            setQ("");
            setOpen(true);
          }}
        />
      }
    >
      <div ref={ref} className="wx-apps-box">
        {!apps.length ? (
          <div className="sub">{error || (data ? "Нет закреплённых программ — добавьте их в настройках" : "Загружаю…")}</div>
        ) : (
          <div
            className="wx-apps"
            style={{ gridTemplateColumns: `repeat(${grid.cols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${grid.rows}, ${grid.h}px)` } as CSSProperties}
          >
            {shown.map((a) => (
              <button
                key={a.name}
                className={cx("wx-app", a.missing && "missing", busy === a.name && "busy")}
                onClick={() => launch(a)}
                title={a.missing ? `${a.name} — не найдена` : a.name}
              >
                <AppGlyph name={a.icon || a.name} size={grid.icon} />
              </button>
            ))}
          </div>
        )}
      </div>
      <WidgetSettingsModal open={open} title="Программы на виджете" width={520} onClose={() => setOpen(false)} onSave={() => setConfig({ ...config, pinned: pick })}>
        <div className="wx-chosen">
          {pick.length === 0 && <span className="faint small">Ничего не выбрано — покажу популярные</span>}
          {pick.map((n) => (
            <span key={n} className="chip">
              {n}
              <button aria-label="Убрать" onClick={() => setPick((p) => p.filter((x) => x !== n))}>
                <X />
              </button>
            </span>
          ))}
        </div>
        <TextInput value={q} onChange={setQ} placeholder="Найти программу" icon={<Search />} />
        <div className="wx-app-list">
          {filtered.map((a) => {
            const on = pick.includes(a.name);
            return (
              <button
                key={a.app_id || a.name}
                className={cx("wx-app-row", on && "on")}
                onClick={() => setPick((p) => (on ? p.filter((x) => x !== a.name) : [...p, a.name]))}
              >
                <span className="wx-app-row-icon">
                  <AppGlyph name={a.icon || a.name} size={20} />
                </span>
                <span className="grow ellipsis">{a.name}</span>
                {on && <Check />}
              </button>
            );
          })}
          {!filtered.length && <div className="faint small" style={{ padding: 10 }}>{all.length ? "Ничего не нашлось" : "Список программ пока недоступен"}</div>}
        </div>
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}

// --- быстрые команды ----------------------------------------------------------------------------------
export interface QuickItem {
  title: string;
  icon?: string;
  text?: string;
  url?: string;
  command_id?: string;
}

const DEFAULT_QUICK: QuickItem[] = [
  { title: "Игровой режим", icon: "steam", text: "включи игровой режим" },
  { title: "Тише", icon: "volume", text: "сделай тише" },
  { title: "Скриншот", icon: "screenshot", text: "сделай скриншот" },
  { title: "Сверни всё", icon: "window", text: "сверни все окна" },
];

function normItems(v: unknown): QuickItem[] {
  if (!Array.isArray(v)) return [];
  return v
    .map((x): QuickItem | null => {
      if (typeof x === "string") return x.trim() ? { title: x.trim(), text: x.trim() } : null;
      if (x && typeof x === "object") {
        const o = x as Record<string, unknown>;
        const title = String(o.title ?? o.name ?? o.text ?? o.url ?? o.command_id ?? "").trim();
        if (!title) return null;
        return {
          title,
          icon: o.icon ? String(o.icon) : undefined,
          text: o.text ? String(o.text) : o.phrase ? String(o.phrase) : undefined,
          url: o.url ? String(o.url) : undefined,
          command_id: o.command_id ? String(o.command_id) : o.id ? String(o.id) : undefined,
        };
      }
      return null;
    })
    .filter((x): x is QuickItem => !!x);
}

type DispatchResult = { kind?: string; result?: ExecResult; text?: string; ok?: boolean; message?: string; title?: string };

async function runQuick(item: QuickItem): Promise<DispatchResult> {
  if (item.command_id) return api.post(`/api/commands/${encodeURIComponent(item.command_id)}/run`, {});
  try {
    return await api.post("/api/widgets/quick/run", item.url ? { url: item.url } : { text: item.text || item.title });
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405) && !item.url) {
      return api.post("/api/dispatch", { text: item.text || item.title, source: "ui" });
    }
    throw e;
  }
}

function QuickIcon({ name }: { name?: string }) {
  if (!name) return null;
  const r = resolveIcon(name);
  if (r.brand)
    return (
      <svg viewBox="0 0 24 24" className="wx-chip-icon" aria-hidden>
        <path d={r.brand.path} fill="currentColor" />
      </svg>
    );
  if (r.glyph) return <span className="wx-chip-icon glyph">{r.glyph}</span>;
  return null;
}

export function QuickWidget({ config, setConfig }: WidgetProps) {
  const configured = normItems(config.items);
  const items = configured.length ? configured : DEFAULT_QUICK;
  const [state, setState] = useState<Record<number, "busy" | "ok" | "err">>({});
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<{ title: string; value: string; icon?: string; command_id?: string }[]>([]);

  const run = async (i: number, item: QuickItem) => {
    setState((s) => ({ ...s, [i]: "busy" }));
    let ok = true;
    try {
      const res = await runQuick(item);
      if (res?.kind === "unknown") {
        ok = false;
        notify(`Не нашёл команду «${item.text || item.title}»`, "error");
      } else if (res?.kind === "command" && res.result && !res.result.ok) {
        ok = false;
        notify(res.result.message || "Не получилось", "error");
      } else if (res?.ok === false) {
        ok = false;
        notify(res.message || "Не получилось", "error");
      }
    } catch (e) {
      ok = false;
      notifyError(e);
    }
    setState((s) => ({ ...s, [i]: ok ? "ok" : "err" }));
    setTimeout(
      () =>
        setState((s) => {
          const next = { ...s };
          delete next[i];
          return next;
        }),
      1400,
    );
  };

  return (
    <WidgetFrame
      icon={<Zap />}
      title={(config.title as string) || "Быстрые команды"}
      actions={
        <CfgButton
          onClick={() => {
            setDraft(items.map((it) => ({ title: it.title, value: it.url || it.text || "", icon: it.icon, command_id: it.command_id })));
            setOpen(true);
          }}
        />
      }
    >
      <div className="wx-chips">
        {items.map((it, i) => (
          <button
            key={`${it.title}-${i}`}
            className={cx("wx-chip", state[i])}
            onClick={() => run(i, it)}
            disabled={state[i] === "busy"}
            title={it.url ? it.url : it.text ? `«${it.text}»` : it.title}
          >
            {state[i] === "busy" ? (
              <span className="spinner" />
            ) : state[i] === "ok" ? (
              <Check className="wx-chip-icon" />
            ) : state[i] === "err" ? (
              <CircleAlert className="wx-chip-icon" />
            ) : (
              <QuickIcon name={it.icon} />
            )}
            {it.title}
          </button>
        ))}
      </div>
      <WidgetSettingsModal
        open={open}
        title="Быстрые команды"
        width={560}
        onClose={() => setOpen(false)}
        onSave={() =>
          setConfig({
            ...config,
            items: draft
              .filter((d) => d.title.trim() && (d.value.trim() || d.command_id))
              .map((d) => {
                const v = d.value.trim();
                const base: Record<string, string> = { title: d.title.trim() };
                if (d.icon) base.icon = d.icon;
                if (d.command_id && !v) base.command_id = d.command_id;
                else if (/^https?:\/\//i.test(v)) base.url = v;
                else base.text = v;
                return base;
              }),
          })
        }
      >
        <p className="muted small" style={{ marginTop: 0 }}>
          Кнопка говорит за вас фразу («сделай тише») или открывает сайт (ссылка с https://).
        </p>
        <div className="wx-quick-edit">
          {draft.map((d, i) => (
            <div key={i} className="wx-quick-row">
              <input
                className="input"
                value={d.title}
                placeholder="Надпись"
                onChange={(e) => setDraft((x) => x.map((r, j) => (j === i ? { ...r, title: e.target.value } : r)))}
              />
              <input
                className="input"
                value={d.value}
                placeholder={d.command_id ? "Команда" : "Фраза или ссылка"}
                onChange={(e) => setDraft((x) => x.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))}
              />
              <IconButton icon={<X />} label="Убрать" size="sm" variant="ghost" onClick={() => setDraft((x) => x.filter((_, j) => j !== i))} />
            </div>
          ))}
        </div>
        <Button size="sm" variant="ghost" icon={<Plus />} onClick={() => setDraft((x) => [...x, { title: "", value: "" }])}>
          Добавить кнопку
        </Button>
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}

// --- недавние файлы -----------------------------------------------------------------------------------
interface FileItem {
  name: string;
  path: string;
  modified: number;
  kind?: string;
}

const KINDS: [RegExp, typeof FileIcon, string][] = [
  [/^(doc|docx|odt|rtf)$/, FileText, "#60A5FA"],
  [/^(xls|xlsx|xlsm|ods|csv)$/, FileSpreadsheet, "#34D399"],
  [/^(ppt|pptx|odp|key)$/, Presentation, "#FB923C"],
  [/^pdf$/, FileText, "#F87171"],
  [/^(png|jpe?g|gif|webp|bmp|svg|heic|psd)$/, FileImage, "#C084FC"],
  [/^(mp3|wav|flac|ogg|m4a|aac)$/, FileMusic, "#F472B6"],
  [/^(mp4|mkv|avi|mov|webm|wmv)$/, FileVideoCamera, "#FBBF24"],
  [/^(zip|rar|7z|tar|gz)$/, FileArchive, "#FCD34D"],
  [/^(js|ts|tsx|py|json|html|css|cpp|c|cs|java|go|rs|sh|bat|ps1|xml|yml|yaml)$/, FileCode, "#22D3EE"],
  [/^(txt|md|log|ini)$/, FileText, "#CBD5E1"],
];

function FileGlyph({ kind, name }: { kind?: string; name: string }) {
  const ext = (kind || name.split(".").pop() || "").toLowerCase();
  const hit = KINDS.find(([re]) => re.test(ext));
  const Icon = hit?.[1] ?? FileIcon;
  return <Icon className="wx-file-icon" style={{ color: hit?.[2] ?? "var(--text-3)" }} />;
}

export function RecentWidget({ h, config }: WidgetProps) {
  const limit = Number(config.limit) || 8;
  const { data, error } = useWidgetData<{ items?: FileItem[]; error?: string }>("recent", { limit }, { refreshMs: 60_000 });
  const items = data?.items ?? [];
  const open = async (f: FileItem) => {
    try {
      await api.post("/api/widgets/recent/open", { path: f.path });
    } catch (e) {
      notifyError(e);
    }
  };
  return (
    <WidgetFrame icon={<FileClock />} title={(config.title as string) || "Недавние"}>
      {!items.length ? (
        <div className="sub">{error || (data ? "Недавних файлов нет" : "Загружаю…")}</div>
      ) : (
        <div className="wlist wx-files">
          {items.slice(0, h <= 1 ? 1 : 20).map((f) => (
            <button key={f.path} className="wx-file" onClick={() => open(f)} title={f.path}>
              <FileGlyph kind={f.kind} name={f.name} />
              <span className="grow ellipsis">{f.name}</span>
              <span className="when">{shortWhen(f.modified)}</span>
            </button>
          ))}
        </div>
      )}
    </WidgetFrame>
  );
}
