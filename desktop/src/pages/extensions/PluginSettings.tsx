// Настройки плагина по его схеме: text, number, bool, select, secret, hotkey (запись сочетания),
// device (микрофон/колонки из /api/voice/devices), list (список элементов со своими полями).
// Каждое изменение сразу уходит в PUT /api/plugins/{id} {config: {ключ: значение}}; секреты ядро кладёт
// в хранилище секретов (значение null — удалить, пустая строка — не менять).
import { Check, KeyRound, Plus, Trash2, X } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { Button, cx, IconButton, notify, notifyError, type Option, Select, Setting, Slider, TextInput, Toggle } from "@/ui";
import type { Plugin } from "./pluginKit";
import { usePlugins } from "./pluginStore";

export interface SchemaField {
  key: string;
  kind: "text" | "number" | "bool" | "select" | "secret" | "hotkey" | "device" | "list";
  label: string;
  options?: { value: string; label: string }[] | string[];
  secret?: string;
  set?: boolean;
  default?: unknown;
  hint?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  device?: string;
  fields?: SchemaField[];
}

function options(field: SchemaField): Option<string>[] {
  return (field.options ?? []).map((o) => (typeof o === "string" ? { value: o, label: o } : { value: String(o.value), label: o.label }));
}

function asText(v: unknown): string {
  if (v === null || v === undefined) return "";
  return typeof v === "string" ? v : String(v);
}

// --- текст и число -------------------------------------------------------------------------------------
function TextField({
  value,
  placeholder,
  type = "text",
  onSave,
  disabled,
}: {
  value: string;
  placeholder?: string;
  type?: string;
  onSave: (v: string) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(value);
  }, [value]);
  return (
    <div
      className="ext-input"
      onFocus={() => (focused.current = true)}
      onBlur={() => (focused.current = false)}
      style={disabled ? { opacity: 0.55, pointerEvents: "none" } : undefined}
    >
      <TextInput
        value={draft}
        type={type}
        placeholder={placeholder}
        onChange={setDraft}
        onCommit={(v) => {
          if (v !== value) onSave(v);
        }}
      />
    </div>
  );
}

function numberFormat(field: SchemaField) {
  const step = field.step ?? (field.integer ? 1 : 0.1);
  const digits = field.integer || Number.isInteger(step) ? 0 : Math.min(2, String(step).split(".")[1]?.length ?? 1);
  const speed = /speed|rate|скорост/i.test(field.key + field.label);
  const pct = field.min === 0 && field.max === 100;
  return (v: number) =>
    `${v.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits })}${speed ? "×" : pct ? "%" : ""}`;
}

function NumberField({ field, value, onSave }: { field: SchemaField; value: number; onSave: (v: number) => void }) {
  const ranged = field.min !== undefined && field.max !== undefined && field.max > field.min;
  if (ranged) {
    return (
      <div className="ext-slider">
        <Slider
          value={Number.isFinite(value) ? value : (field.min as number)}
          min={field.min}
          max={field.max}
          step={field.step ?? (field.integer ? 1 : (field.max! - field.min!) / 100)}
          format={numberFormat(field)}
          onCommit={(v) => v !== value && onSave(field.integer ? Math.round(v) : v)}
        />
      </div>
    );
  }
  return (
    <TextField
      type="number"
      value={Number.isFinite(value) ? String(value) : ""}
      placeholder={field.placeholder}
      onSave={(v) => {
        const n = Number(v.replace(",", "."));
        if (v.trim() === "" || !Number.isFinite(n)) {
          notify(`«${field.label}»: нужно число`, "error");
          return;
        }
        onSave(field.integer ? Math.round(n) : n);
      }}
    />
  );
}

// --- секрет ------------------------------------------------------------------------------------------
function SecretField({ field, onSave }: { field: SchemaField; onSave: (v: string | null) => Promise<void> }) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async (v: string | null) => {
    setBusy(true);
    try {
      await onSave(v);
      setDraft("");
      notify(v === null ? "Удалено" : "Сохранено", "ok");
    } catch {
      /* ошибку показал onSave */
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row" style={{ gap: 8 }}>
      <div className="ext-input secret">
        <TextInput
          className="secret-input"
          value={draft}
          onChange={setDraft}
          placeholder={field.set ? "••••••••  сохранён" : field.placeholder || "Вставьте ключ"}
          onEnter={() => draft.trim() && save(draft.trim())}
        />
      </div>
      <Button size="sm" variant="soft" disabled={!draft.trim()} loading={busy} onClick={() => save(draft.trim())}>
        Сохранить
      </Button>
      {field.set && (
        <IconButton icon={<Trash2 />} label="Удалить ключ" size="sm" variant="ghost" onClick={() => save(null)} />
      )}
    </div>
  );
}

// --- сочетание клавиш ---------------------------------------------------------------------------------
const KEY_NAMES: Record<string, string> = {
  " ": "Space",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Escape: "Esc",
  Enter: "Enter",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  Insert: "Insert",
  Home: "Home",
  End: "End",
  PageUp: "PageUp",
  PageDown: "PageDown",
  CapsLock: "CapsLock",
  Pause: "Pause",
  PrintScreen: "PrintScreen",
  ScrollLock: "ScrollLock",
};

function keyName(e: KeyboardEvent): string {
  if (["Control", "Alt", "Shift", "Meta", "AltGraph", "OS"].includes(e.key)) return "";
  // буквы и цифры — по физической клавише, чтобы русская раскладка давала «D», а не «В»
  if (/^Key[A-Z]$/.test(e.code)) return e.code.slice(3);
  if (/^Digit\d$/.test(e.code)) return e.code.slice(5);
  if (/^Numpad\d$/.test(e.code)) return `Num${e.code.slice(6)}`;
  if (/^F\d{1,2}$/.test(e.key)) return e.key;
  if (e.code === "Space") return "Space";
  if (KEY_NAMES[e.key]) return KEY_NAMES[e.key];
  return e.key.length === 1 ? e.key.toUpperCase() : e.key;
}

export function HotkeyInput({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [rec, setRec] = useState(false);
  const [mods, setMods] = useState("");
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  useEffect(() => {
    if (!rec) return;
    const down = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const plain = !e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey;
      if (e.key === "Escape" && plain) return setRec(false);
      if ((e.key === "Backspace" || e.key === "Delete") && plain) {
        saveRef.current("");
        return setRec(false);
      }
      const parts = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean) as string[];
      const key = keyName(e);
      if (!key) return setMods(parts.join("+"));
      saveRef.current([...parts, key].join("+"));
      setRec(false);
    };
    const up = (e: KeyboardEvent) => {
      const parts = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);
      setMods(parts.join("+"));
    };
    const stop = () => setRec(false);
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up, true);
    window.addEventListener("blur", stop);
    return () => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", stop);
      setMods("");
    };
  }, [rec]);
  const keys = value ? value.split("+").filter(Boolean) : [];
  return (
    <button
      type="button"
      className={cx("hotkey-input", rec && "rec")}
      onClick={() => setRec((r) => !r)}
      title={rec ? "Esc — отмена, Backspace — очистить" : "Нажмите, чтобы записать сочетание"}
    >
      {rec ? (
        <span className="hk-wait">{mods ? `${mods}+…` : "Нажмите сочетание…"}</span>
      ) : keys.length ? (
        keys.map((k) => (
          <span key={k} className="kbd">
            {k}
          </span>
        ))
      ) : (
        <span className="faint">Не задано</span>
      )}
      <KeyRound className="hk-icon" />
    </button>
  );
}

// --- устройство ---------------------------------------------------------------------------------------
interface Device {
  id: string;
  name: string;
  default?: boolean;
}
let devicesCache: Promise<{ inputs: Device[]; outputs: Device[] }> | null = null;
function loadDevices() {
  if (!devicesCache) {
    devicesCache = api.get<{ inputs: Device[]; outputs: Device[] }>("/api/voice/devices").catch((e) => {
      devicesCache = null;
      throw e;
    });
  }
  return devicesCache;
}

function DeviceField({ field, value, onSave }: { field: SchemaField; value: string; onSave: (v: string) => void }) {
  const [list, setList] = useState<Device[] | null>(null);
  const output = field.device === "output" || /output|speaker|колонк|динамик/i.test(field.device ?? "");
  useEffect(() => {
    let alive = true;
    loadDevices()
      .then((r) => alive && setList((output ? r.outputs : r.inputs) ?? []))
      .catch(() => alive && setList([]));
    return () => {
      alive = false;
    };
  }, [output]);
  const opts: Option<string>[] = [{ value: "", label: "Как в системе" }];
  for (const d of list ?? []) {
    const v = d.id || d.name;
    if (v && !opts.some((o) => o.value === v)) opts.push({ value: v, label: d.name, hint: d.default ? "по умолчанию" : undefined });
  }
  if (value && !opts.some((o) => o.value === value)) opts.push({ value, label: value, hint: "не найдено" });
  return <Select value={value} options={opts} onChange={onSave} width={260} />;
}

// --- список -------------------------------------------------------------------------------------------
type Item = Record<string, unknown>;

function ItemField({
  field,
  value,
  isSet,
  onChange,
  onCommit,
}: {
  field: SchemaField;
  value: unknown;
  isSet?: boolean;
  onChange: (v: unknown) => void;
  onCommit: () => void;
}) {
  switch (field.kind) {
    case "bool":
      return (
        <Toggle
          checked={!!value}
          onChange={(v) => {
            onChange(v);
            setTimeout(onCommit);
          }}
          label={field.label}
        />
      );
    case "select":
      return (
        <Select
          value={asText(value)}
          options={options(field)}
          onChange={(v) => {
            onChange(v);
            setTimeout(onCommit);
          }}
        />
      );
    default:
      return (
        <input
          className={cx("input", field.kind === "secret" && "secret-input")}
          type={field.kind === "number" ? "number" : "text"}
          autoComplete="off"
          value={asText(value)}
          placeholder={field.kind === "secret" ? (isSet ? "••••••  сохранён" : "Не задан") : field.placeholder ?? ""}
          spellCheck={false}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onCommit()}
        />
      );
  }
}

function ListField({ field, value, onSave }: { field: SchemaField; value: Item[]; onSave: (v: Item[]) => Promise<void> }) {
  const sub = field.fields ?? [];
  const [draft, setDraft] = useState<Item[]>(value);
  const dirty = useRef(false);
  useEffect(() => {
    if (!dirty.current) setDraft(value);
  }, [value]);
  const latest = useRef(draft);
  latest.current = draft;

  const clean = (items: Item[]) =>
    items.map((it) => {
      const out: Item = {};
      if (it.id) out.id = it.id;
      for (const f of sub) {
        const v = it[f.key];
        if (f.kind === "secret") {
          if (typeof v === "string" && v.trim()) out[f.key] = v.trim();
          continue; // пусто — плагин оставит прежний ключ
        }
        if (f.kind === "number") {
          const n = Number(String(v ?? "").replace(",", "."));
          out[f.key] = Number.isFinite(n) ? n : f.default ?? 0;
          continue;
        }
        out[f.key] = v ?? f.default ?? (f.kind === "bool" ? false : "");
      }
      return out;
    });

  const commit = async (items = latest.current) => {
    const next = clean(items);
    if (JSON.stringify(next) === JSON.stringify(clean(value))) {
      dirty.current = false;
      return;
    }
    try {
      await onSave(next);
      dirty.current = false;
    } catch {
      /* ошибку уже показали; черновик остаётся, можно поправить */
    }
  };

  const set = (i: number, key: string, v: unknown) => {
    dirty.current = true;
    setDraft((d) => d.map((it, j) => (j === i ? { ...it, [key]: v } : it)));
  };

  const blank = (): Item => Object.fromEntries(sub.map((f) => [f.key, f.default ?? (f.kind === "bool" ? false : f.kind === "number" ? 0 : "")]));

  return (
    <div className="plist">
      {draft.length === 0 && <div className="faint small">Пока пусто</div>}
      {draft.map((it, i) => (
        <div
          key={String(it.id ?? i)}
          className="plist-item"
          onBlur={(e) => {
            // сохраняем, когда фокус ушёл из элемента целиком, — а не после каждого поля
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) commit();
          }}
        >
          <div className="plist-fields" style={{ gridTemplateColumns: `repeat(${Math.min(3, Math.max(1, sub.length))}, minmax(0, 1fr))` }}>
            {sub.map((f) => (
              <label key={f.key} className="plist-field">
                <span>{f.label}</span>
                <ItemField
                  field={f}
                  value={it[f.key]}
                  isSet={it[`${f.key}_set`] === true}
                  onChange={(v) => set(i, f.key, v)}
                  onCommit={() => commit()}
                />
              </label>
            ))}
          </div>
          <IconButton
            icon={<X />}
            label="Убрать"
            size="sm"
            variant="ghost"
            onClick={() => {
              const next = draft.filter((_, j) => j !== i);
              setDraft(next);
              commit(next);
            }}
          />
        </div>
      ))}
      <div>
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus />}
          onClick={() => {
            dirty.current = true;
            setDraft((d) => [...d, blank()]);
          }}
        >
          Добавить
        </Button>
      </div>
    </div>
  );
}

// --- форма --------------------------------------------------------------------------------------------
export function PluginSettings({ plugin, readOnly }: { plugin: Plugin; readOnly?: boolean }) {
  const update = usePlugins((s) => s.update);
  const schema = useMemo(() => (plugin.schema ?? []) as unknown as SchemaField[], [plugin.schema]);
  const save = async (patch: Record<string, unknown>) => {
    try {
      await update(plugin.id, { config: patch });
    } catch (e) {
      notifyError(e);
      throw e;
    }
  };
  const quiet = (patch: Record<string, unknown>) => {
    save(patch).catch(() => undefined);
  };

  if (!schema.length) {
    return <div className="faint small" style={{ padding: "4px 2px" }}>У этого плагина нет настроек.</div>;
  }

  return (
    <div className={cx("pl-settings", readOnly && "readonly")}>
      {schema.map((f) => {
        const value = plugin.config?.[f.key] ?? f.default;
        let control: ReactNode;
        switch (f.kind) {
          case "bool":
            control = <Toggle checked={!!value} onChange={(v) => quiet({ [f.key]: v })} label={f.label} />;
            break;
          case "select":
            control = <Select value={asText(value)} options={options(f)} onChange={(v) => quiet({ [f.key]: v })} width={260} />;
            break;
          case "number":
            control = <NumberField field={f} value={Number(value)} onSave={(v) => quiet({ [f.key]: v })} />;
            break;
          case "secret":
            control = <SecretField field={f} onSave={(v) => save({ [f.key]: v })} />;
            break;
          case "hotkey":
            control = <HotkeyInput value={asText(value)} onSave={(v) => quiet({ [f.key]: v })} />;
            break;
          case "device":
            control = <DeviceField field={f} value={asText(value)} onSave={(v) => quiet({ [f.key]: v })} />;
            break;
          case "list":
            return (
              <div key={f.key} className="setting ext-setting-col">
                <div className="label">
                  <div className="name">{f.label}</div>
                  {f.hint && <div className="desc">{f.hint}</div>}
                </div>
                <ListField field={f} value={Array.isArray(value) ? (value as Item[]) : []} onSave={(v) => save({ [f.key]: v })} />
              </div>
            );
          default:
            control = <TextField value={asText(value)} placeholder={f.placeholder} onSave={(v) => quiet({ [f.key]: v })} />;
        }
        return (
          <Setting
            key={f.key}
            name={
              <span className="row" style={{ gap: 8 }}>
                {f.label}
                {f.kind === "secret" &&
                  (f.set ? (
                    <span className="chip ok mini">
                      <Check /> сохранён
                    </span>
                  ) : (
                    <span className="chip mini">не задан</span>
                  ))}
              </span>
            }
            desc={f.hint}
          >
            {control}
          </Setting>
        );
      })}
    </div>
  );
}
