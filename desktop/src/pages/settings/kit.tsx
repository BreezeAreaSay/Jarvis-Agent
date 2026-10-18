// Общие детали страниц «Настройки», «Профиль» и мастера первого запуска: загрузка данных ядра,
// поля ключей, захват клавиш, редактор слов, волна голоса, индикатор микрофона, модели голоса.
import { Check, ChevronDown, Copy, Eye, EyeOff, KeyRound, Trash2, X } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { DeepPartial, SecretName, Settings, VoiceModel, VoiceState } from "@/api/types";
import { levels } from "@/lib/levels";
import { useApp } from "@/store/app";
import { Button, cx, IconButton, notify, notifyError, useDebounced } from "@/ui";

// --- данные ядра ----------------------------------------------------------------------------------------

export interface ApiState<T> {
  data: T | undefined;
  error: ApiError | null;
  loading: boolean;
  /** Маршрута ещё нет в ядре (404) — модуль не готов, показываем запасной вид. */
  missing: boolean;
  reload: () => Promise<void>;
  setData: React.Dispatch<React.SetStateAction<T | undefined>>;
}

/** GET-запрос к ядру с перезагрузкой; null — не загружать. Прежние данные живут, пока идёт новая загрузка. */
export function useApi<T>(path: string | null): ApiState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(!!path);
  const current = useRef(path);
  current.current = path;
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const res = await api.get<T>(path);
      if (current.current !== path) return;
      setData(res);
      setError(null);
    } catch (e) {
      if (current.current !== path) return;
      setError(e instanceof ApiError ? e : new ApiError(String(e), 0));
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, missing: error?.status === 404, reload, setData };
}

/** Настройки и запись части настроек с показом ошибки. */
export function useSettingsPatch(): [Settings | null, (patch: DeepPartial<Settings>) => void] {
  const settings = useApp((s) => s.settings);
  const patchSettings = useApp((s) => s.patchSettings);
  const patch = useCallback((p: DeepPartial<Settings>) => void patchSettings(p).catch(notifyError), [patchSettings]);
  return [settings, patch];
}

export interface AudioDevice {
  id: string;
  name: string;
  default: boolean;
}

export function useDevices() {
  return useApi<{ inputs: AudioDevice[]; outputs: AudioDevice[]; error?: string }>("/api/voice/devices");
}

/** Jarvis говорит (tts.state). */
export function useSpeaking(): boolean {
  const [speaking, setSpeaking] = useState(levels.speaking);
  useEffect(() => onEvent<{ speaking: boolean }>("tts.state", (e) => setSpeaking(!!e.data.speaking)), []);
  return speaking;
}

export interface VoiceStatus {
  state: VoiceState;
  reason?: string;
  enabled: boolean;
  error?: string;
  wake_words: string[];
  models_ready: boolean;
  input_device: string;
  ptt_error?: string;
}

/** GET /api/voice: ошибка голоса («модели не скачаны»), ошибка клавиши «держи и говори», готовность моделей. */
export function useVoiceStatus() {
  const q = useApi<VoiceStatus>("/api/voice");
  const { reload } = q;
  const v = useApp((s) => s.settings?.voice);
  const key = v ? [v.enabled, v.input_device, v.ptt_key, v.asr_model].join("|") : "";
  const soon = useDebounced(() => void reload(), 500);
  useEffect(() => onEvent("voice.state", () => soon()), [soon]);
  useEffect(() => onEvent<{ done: boolean }>("model.download", (e) => e.data.done && soon()), [soon]);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    soon();
  }, [key, soon]);
  return q;
}

// --- модели голоса ----------------------------------------------------------------------------------------

export type ModelItem = VoiceModel & { description?: string; error?: string };

interface DownloadEvent {
  id: string;
  progress: number;
  done: boolean;
  error?: string;
}

/** Список моделей голоса с живым прогрессом скачивания (события model.download). */
export function useVoiceModels() {
  const q = useApi<{ items: ModelItem[] }>("/api/voice/models");
  const { setData, reload } = q;
  useEffect(
    () =>
      onEvent<DownloadEvent>("model.download", (e) => {
        const d = e.data;
        setData((prev) => {
          if (!prev) return prev;
          return {
            items: prev.items.map((m) =>
              m.id !== d.id
                ? m
                : {
                    ...m,
                    downloading: !d.done,
                    progress: d.progress,
                    installed: d.done && !d.error ? true : m.installed,
                    error: d.error ?? "",
                  },
            ),
          };
        });
        if (d.done) {
          if (d.error) notify(`Модель не скачалась: ${d.error}`, "error");
          setTimeout(() => void reload(), 500);
        }
      }),
    [setData, reload],
  );
  const download = useCallback(
    async (id: string) => {
      setData((prev) =>
        prev && { items: prev.items.map((m) => (m.id === id ? { ...m, downloading: true, progress: 0, error: "" } : m)) },
      );
      try {
        await api.post(`/api/voice/models/${encodeURIComponent(id)}/download`);
      } catch (e) {
        notifyError(e);
        void reload();
      }
    },
    [setData, reload],
  );
  const remove = useCallback(
    async (id: string) => {
      try {
        await api.del(`/api/voice/models/${encodeURIComponent(id)}`);
      } catch (e) {
        notifyError(e);
      }
      void reload();
    },
    [reload],
  );
  return { ...q, items: q.data?.items ?? [], download, remove };
}

export function fmtMb(mb: number): string {
  if (!mb) return "";
  if (mb >= 1000) return `${(mb / 1000).toFixed(1).replace(".", ",")} ГБ`;
  if (mb < 1) return `${Math.max(1, Math.round(mb * 1000))} КБ`;
  return `${Math.round(mb)} МБ`;
}

// --- мелкие элементы --------------------------------------------------------------------------------------

export function ProgressBar({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, value * 100));
  return (
    <div className={cx("set-progress", className)} role="progressbar" aria-valuenow={Math.round(pct)}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

/** Строка модели: название, описание, размер, состояние и кнопки «Скачать» / «Удалить». */
export function ModelRow({
  model,
  icon,
  onDownload,
  onRemove,
  compact,
}: {
  model: ModelItem;
  icon?: ReactNode;
  onDownload: () => void;
  onRemove?: () => void;
  compact?: boolean;
}) {
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 3000);
    return () => clearTimeout(t);
  }, [confirm]);
  return (
    <div className={cx("model-row", model.installed && "installed", compact && "compact")}>
      {icon && <span className="model-ico">{icon}</span>}
      <div className="grow">
        <div className="model-title">
          <span className="ellipsis">{model.title}</span>
          {model.required && !model.installed && <span className="chip accent tiny-chip">нужна</span>}
        </div>
        {model.downloading ? (
          <div className="model-dl">
            <ProgressBar value={model.progress} />
            <span className="faint tiny">{Math.round(model.progress * 100)}%</span>
          </div>
        ) : model.error ? (
          <div className="model-desc err-text">{model.error}</div>
        ) : (
          !compact && model.description && <div className="model-desc">{model.description}</div>
        )}
      </div>
      <span className="model-size">{fmtMb(model.size_mb)}</span>
      {model.installed ? (
        <>
          <span className="chip ok">
            <Check /> Готова
          </span>
          {onRemove &&
            (confirm ? (
              <Button size="sm" variant="danger" onClick={onRemove}>
                Удалить?
              </Button>
            ) : (
              <IconButton icon={<Trash2 />} label="Удалить модель" size="sm" variant="ghost" onClick={() => setConfirm(true)} />
            ))}
        </>
      ) : model.downloading ? (
        <span className="spinner" />
      ) : (
        <Button size="sm" variant={model.required ? "primary" : "soft"} onClick={onDownload}>
          {model.error ? "Повторить" : "Скачать"}
        </Button>
      )}
    </div>
  );
}

/** Значок состояния голоса и его подпись. */
export function voiceStateText(state: VoiceState, wake: string, reason = ""): string {
  switch (state) {
    case "off":
      return "Микрофон выключен";
    case "loading":
      return reason || "Загружаю модели…";
    case "idle":
      return `Жду «${wake}»`;
    case "listening":
      return "Слушаю…";
    case "processing":
      return "Распознаю…";
    case "speaking":
      return "Говорю…";
    case "error":
      return reason ? `Ошибка: ${reason}` : "Голос недоступен";
  }
}

export function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Индикатор громкости микрофона (voice.level) — сегменты, перерисовка вне React. */
export function MicMeter({ segments = 22, className }: { segments?: number; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const bars = Array.from(el.children) as HTMLElement[];
    let raf = 0;
    let shown = 0;
    let lit = -1;
    const tick = () => {
      const target = Math.min(1, levels.mic * 1.5);
      shown = target > shown ? target : shown + (target - shown) * 0.12;
      const n = Math.round(shown * segments);
      if (n !== lit) {
        lit = n;
        bars.forEach((b, i) => b.classList.toggle("on", i < n));
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [segments]);
  return (
    <div className={cx("mic-meter", className)} ref={ref} aria-hidden>
      {Array.from({ length: segments }, (_, i) => (
        <i key={i} />
      ))}
    </div>
  );
}

/** Волна голоса Jarvis: спокойно «дышит», пока тихо, и пляшет в такт речи (tts.level). */
export function Waveform({ bars = 56, height = 64, live }: { bars?: number; height?: number; live?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const animated = useApp((s) => s.settings?.interface.animations ?? true);
  const speaking = useSpeaking();
  const active = live || speaking;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const items = Array.from(el.children) as HTMLElement[];
    const n = items.length;
    const seed = items.map((_, i) => 0.55 + 0.45 * Math.abs(Math.sin(i * 12.9898) * 0.5 + Math.sin(i * 4.1) * 0.5));
    const env = items.map((_, i) => {
      const x = (i - (n - 1) / 2) / (n / 2);
      return Math.exp(-x * x * 2.6);
    });
    const min = 5;
    let raf = 0;
    let level = 0;
    let t = 0;
    let last = performance.now();
    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      t += dt;
      const target = levels.speaking ? Math.min(1, levels.tts * 1.7) : 0;
      level += (target - level) * Math.min(1, dt * (target > level ? 18 : 5));
      for (let i = 0; i < n; i++) {
        const idle = 0.16 + 0.1 * Math.sin(t * 1.6 + i * 0.42) + 0.05 * Math.sin(t * 3.1 - i * 0.9);
        const voice = level * seed[i] * (0.6 + 0.4 * Math.sin(t * 13 + i * 1.7));
        const amp = Math.max(idle * (1 - level), voice);
        const h = min + (height - min) * env[i] * Math.min(1, amp);
        items[i].style.height = `${h.toFixed(1)}px`;
      }
      raf = animated || levels.speaking ? requestAnimationFrame(draw) : 0;
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [height, animated, active]);
  return (
    <div className={cx("wave", active && "live")} ref={ref} style={{ height }} aria-hidden>
      {Array.from({ length: bars }, (_, i) => (
        <i key={i} />
      ))}
    </div>
  );
}

// --- ключи API --------------------------------------------------------------------------------------------

/** Поле секрета: пароль с «глазом»; сохранённый ключ не показывается — только «ключ сохранён». */
export function SecretField({
  name,
  placeholder = "Вставьте ключ",
  savedText = "Ключ сохранён",
  onSaved,
}: {
  name: SecretName;
  placeholder?: string;
  savedText?: string;
  onSaved?: () => void;
}) {
  const has = useApp((s) => !!s.secrets[name]);
  const setSecret = useApp((s) => s.setSecret);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    const v = value.trim();
    if (!v) return;
    setBusy(true);
    try {
      await setSecret(name, v);
      setValue("");
      setEditing(false);
      notify("Ключ сохранён", "ok");
      onSaved?.();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    try {
      await setSecret(name, "");
      notify("Ключ удалён", "info");
    } catch (e) {
      notifyError(e);
    }
  };

  if (has && !editing) {
    return (
      <div className="secret-saved">
        <span className="chip ok">
          <Check /> {savedText}
        </span>
        <Button size="sm" onClick={() => setEditing(true)}>
          Заменить
        </Button>
        <IconButton icon={<Trash2 />} label="Удалить ключ" size="sm" variant="ghost" onClick={remove} />
      </div>
    );
  }
  return (
    <div className="secret-field">
      <div className="secret-input">
        <KeyRound />
        <input
          className="input"
          type={show ? "text" : "password"}
          value={value}
          placeholder={placeholder}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            if (e.key === "Escape" && has) setEditing(false);
          }}
        />
        <button type="button" className="eye" aria-label={show ? "Скрыть" : "Показать"} onClick={() => setShow(!show)}>
          {show ? <EyeOff /> : <Eye />}
        </button>
      </div>
      <Button size="sm" variant="primary" disabled={!value.trim()} loading={busy} onClick={save}>
        Сохранить
      </Button>
      {has && (
        <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
          Отмена
        </Button>
      )}
    </div>
  );
}

// --- захват клавиш ----------------------------------------------------------------------------------------

const MODIFIER_CODES = new Set([
  "ControlLeft",
  "ControlRight",
  "AltLeft",
  "AltRight",
  "ShiftLeft",
  "ShiftRight",
  "MetaLeft",
  "MetaRight",
]);

const ACCEL_KEYS: Record<string, string> = {
  Space: "Space",
  Enter: "Enter",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  Insert: "Insert",
  Home: "Home",
  End: "End",
  PageUp: "PageUp",
  PageDown: "PageDown",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Minus: "-",
  Equal: "=",
  BracketLeft: "[",
  BracketRight: "]",
  Backslash: "\\",
  Semicolon: ";",
  Quote: "'",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Backquote: "`",
  PrintScreen: "PrintScreen",
  Pause: "Pause",
  ScrollLock: "Scrolllock",
  NumpadAdd: "numadd",
  NumpadSubtract: "numsub",
  NumpadMultiply: "nummult",
  NumpadDivide: "numdiv",
  NumpadDecimal: "numdec",
  NumpadEnter: "Enter",
};

/** Клавиша для сочетания Electron (Ctrl+Alt+J): KeyJ → J, F5 → F5; модификатор или неизвестная — null. */
function accelKey(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit\d$/.test(code)) return code.slice(5);
  if (/^F\d{1,2}$/.test(code)) return code;
  if (/^Numpad\d$/.test(code)) return `num${code.slice(6)}`;
  return ACCEL_KEYS[code] ?? null;
}

const PTT_KEYS: Record<string, string> = {
  ControlRight: "right ctrl",
  ControlLeft: "left ctrl",
  ShiftRight: "right shift",
  ShiftLeft: "left shift",
  AltRight: "right alt",
  AltLeft: "left alt",
  MetaRight: "right win",
  MetaLeft: "left win",
  CapsLock: "caps lock",
  ScrollLock: "scroll lock",
  Pause: "pause",
  Insert: "insert",
  ContextMenu: "menu",
  NumLock: "num lock",
};

/** Клавиша «держи и говори» в формате настроек ядра (right ctrl, f9, caps lock, x). */
function pttKey(code: string): string | null {
  if (PTT_KEYS[code]) return PTT_KEYS[code];
  if (/^F\d{1,2}$/.test(code)) return code.toLowerCase();
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit\d$/.test(code)) return code.slice(5);
  return null;
}

const PTT_LABELS: Record<string, string> = {
  "right ctrl": "Правый Ctrl",
  "left ctrl": "Левый Ctrl",
  "right shift": "Правый Shift",
  "left shift": "Левый Shift",
  "right alt": "Правый Alt",
  "left alt": "Левый Alt",
  "alt gr": "AltGr",
  "right win": "Правая Win",
  "left win": "Левая Win",
  "caps lock": "Caps Lock",
  "scroll lock": "Scroll Lock",
  pause: "Pause",
  insert: "Insert",
  menu: "Menu",
  "num lock": "Num Lock",
};

const KEY_LABELS: Record<string, string> = {
  Space: "Пробел",
  Up: "↑",
  Down: "↓",
  Left: "←",
  Right: "→",
  Super: "Win",
  Meta: "Win",
  CommandOrControl: "Ctrl",
  CmdOrCtrl: "Ctrl",
  Control: "Ctrl",
  Esc: "Esc",
};

export function keyParts(value: string, mode: "accelerator" | "key"): string[] {
  if (!value) return [];
  if (mode === "key") {
    const v = value.trim().toLowerCase();
    return [PTT_LABELS[v] ?? (v.length === 1 ? v.toUpperCase() : cap(v))];
  }
  return value.split("+").map((p) => KEY_LABELS[p] ?? (p.length === 1 ? p.toUpperCase() : p));
}

/** Кнопка захвата клавиши: нажмите — и зажмите нужное сочетание. Esc — отмена, Backspace — очистить. */
export function KeyCapture({
  value,
  onChange,
  mode = "accelerator",
  clearable,
  emptyText = "Не задана",
}: {
  value: string;
  onChange: (v: string) => void;
  mode?: "accelerator" | "key";
  clearable?: boolean;
  emptyText?: string;
}) {
  const [capturing, setCapturing] = useState(false);
  const [held, setHeld] = useState<string[]>([]);
  const btn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!capturing) return;
    const finish = (v: string | null) => {
      setCapturing(false);
      setHeld([]);
      if (v !== null && v !== value) onChange(v);
    };
    const down = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;
      const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Super"].filter(
        (m): m is string => !!m,
      );
      if (e.code === "Escape" && !mods.length) return finish(null);
      if ((e.code === "Backspace" || e.code === "Delete") && !mods.length && clearable) return finish("");
      if (mode === "key") {
        const k = pttKey(e.code);
        if (k) finish(k);
        return;
      }
      if (MODIFIER_CODES.has(e.code)) {
        setHeld(mods);
        return;
      }
      const key = accelKey(e.code);
      if (!key) return;
      if (!mods.length && !/^F\d{1,2}$/.test(key)) {
        notify("Нужен хотя бы один модификатор: Ctrl, Alt, Shift или Win", "info");
        return;
      }
      finish([...mods, key].join("+"));
    };
    const up = (e: KeyboardEvent) => {
      if (mode !== "accelerator") return;
      setHeld([e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Super"].filter((m): m is string => !!m));
    };
    const cancel = (e: MouseEvent) => {
      if (!btn.current?.contains(e.target as Node)) finish(null);
    };
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up, true);
    window.addEventListener("mousedown", cancel, true);
    return () => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
      window.removeEventListener("mousedown", cancel, true);
    };
  }, [capturing, mode, clearable, onChange, value]);

  const parts = capturing ? held.map((p) => KEY_LABELS[p] ?? p) : keyParts(value, mode);
  return (
    <div className="key-capture-wrap">
      <button
        ref={btn}
        type="button"
        className={cx("key-capture", capturing && "capturing")}
        onClick={() => setCapturing(!capturing)}
        title={capturing ? "Esc — отмена" : "Нажмите, чтобы задать"}
      >
        {parts.length ? (
          parts.map((p, i) => (
            <span key={i} className="row" style={{ gap: 4 }}>
              {i > 0 && <span className="faint">+</span>}
              <span className="kbd">{p}</span>
            </span>
          ))
        ) : (
          <span className={capturing ? "accent-text" : "faint"}>{capturing ? "" : emptyText}</span>
        )}
        {capturing && <span className="accent-text small">{mode === "key" ? "Нажмите клавишу…" : "Нажмите сочетание…"}</span>}
      </button>
      {clearable && value && !capturing && (
        <IconButton icon={<X />} label="Очистить" size="sm" variant="ghost" onClick={() => onChange("")} />
      )}
    </div>
  );
}

// --- редактор слов ------------------------------------------------------------------------------------------

/** Чипы со словами и поле «добавить» (слова-активаторы). */
export function ChipsEditor({
  value,
  onChange,
  placeholder = "Добавить…",
  min = 0,
  max = 10,
  normalize = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " "),
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  min?: number;
  max?: number;
  normalize?: (s: string) => string;
}) {
  const [text, setText] = useState("");
  const add = () => {
    const w = normalize(text).slice(0, 32);
    if (!w) return;
    if (value.includes(w)) {
      setText("");
      return;
    }
    if (value.length >= max) {
      notify(`Не больше ${max}`, "info");
      return;
    }
    onChange([...value, w]);
    setText("");
  };
  return (
    <div className="chips-edit">
      {value.map((w) => (
        <span key={w} className="chip word-chip">
          {w}
          <button
            type="button"
            aria-label={`Убрать «${w}»`}
            disabled={value.length <= min}
            title={value.length <= min ? "Нужно хотя бы одно слово" : undefined}
            onClick={() => onChange(value.filter((x) => x !== w))}
          >
            <X />
          </button>
        </span>
      ))}
      <input
        className="chips-input"
        value={text}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
      />
    </div>
  );
}

// --- поле с подсказками -------------------------------------------------------------------------------------

/** Поле ввода со списком подсказок (модель ИИ: из списка или своя). */
export function Combo({
  value,
  options,
  onCommit,
  placeholder,
  width,
}: {
  value: string;
  options: { value: string; hint?: string }[];
  onCommit: (v: string) => void;
  placeholder?: string;
  width?: number | string;
}) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const commit = (v: string) => {
    const t = v.trim();
    setText(t);
    setOpen(false);
    setTyped(false);
    if (t !== value) onCommit(t);
  };
  const q = text.trim().toLowerCase();
  const list = typed && q ? options.filter((o) => o.value.toLowerCase().includes(q)) : options;
  return (
    <div className="select combo" ref={ref} style={{ width }}>
      <input
        className="input"
        value={text}
        placeholder={placeholder}
        spellCheck={false}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setText(e.target.value);
          setTyped(true);
          setOpen(true);
        }}
        onBlur={() => setTimeout(() => ref.current && !ref.current.contains(document.activeElement) && commit(text), 120)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit(text);
          if (e.key === "Escape") {
            setText(value);
            setOpen(false);
          }
        }}
      />
      <button type="button" className="combo-arrow" tabIndex={-1} aria-label="Список" onMouseDown={(e) => e.preventDefault()} onClick={() => setOpen(!open)}>
        <ChevronDown />
      </button>
      {open && list.length > 0 && (
        <div className="select-menu" role="listbox">
          {list.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === value}
              className={cx("select-option", o.value === value && "selected")}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => commit(o.value)}
            >
              <span className="ellipsis">{o.value}</span>
              {o.hint && <span className="hint">{o.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Число с подтверждением по Enter/уходу из поля (порт, бюджет). */
export function NumberField({
  value,
  onCommit,
  min,
  max,
  step = 1,
  width = 110,
  suffix,
  prefix,
}: {
  value: number;
  onCommit: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  width?: number;
  suffix?: string;
  prefix?: string;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const commit = () => {
    const n = Number(text.replace(",", "."));
    if (!Number.isFinite(n) || (min !== undefined && n < min) || (max !== undefined && n > max)) {
      notify(`Нужно число${min !== undefined ? ` от ${min}` : ""}${max !== undefined ? ` до ${max}` : ""}`, "error");
      setText(String(value));
      return;
    }
    if (n !== value) onCommit(n);
  };
  return (
    <div className="num-field" style={{ width }}>
      {prefix && <span className="affix">{prefix}</span>}
      <input
        className="input"
        inputMode="decimal"
        value={text}
        step={step}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {suffix && <span className="affix">{suffix}</span>}
    </div>
  );
}

/** Текст с подтверждением по уходу из поля (для настроек, чтобы не слать PATCH на каждую букву). */
export function LazyText({
  value,
  onCommit,
  placeholder,
  width,
  icon,
  multiline,
  rows = 3,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  width?: number | string;
  icon?: ReactNode;
  multiline?: boolean;
  rows?: number;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    if (text !== value) onCommit(multiline ? text : text.trim());
  };
  if (multiline) {
    return (
      <textarea
        className="textarea"
        value={text}
        rows={rows}
        placeholder={placeholder}
        style={{ width }}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
      />
    );
  }
  const input = (
    <input
      className="input"
      value={text}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
  return (
    <div className={cx(icon ? "input-wrap" : undefined)} style={{ width }}>
      {icon}
      {input}
    </div>
  );
}

export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

export function CopyButton({ text, label = "Скопировать" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => setDone(false), 1500);
    return () => clearTimeout(t);
  }, [done]);
  return (
    <IconButton
      icon={done ? <Check /> : <Copy />}
      label={label}
      size="sm"
      active={done}
      onClick={() =>
        copyText(text)
          .then(() => setDone(true))
          .catch(notifyError)
      }
    />
  );
}

/** Заметка внутри карточки: модуль ядра ещё не готов / подсказка. */
export function Note({ icon, children, tone = "info" }: { icon?: ReactNode; children: ReactNode; tone?: "info" | "warn" | "err" }) {
  return (
    <div className={cx("set-note", tone)}>
      {icon}
      <div>{children}</div>
    </div>
  );
}

/** Приветствие по времени суток — для фразы проверки голоса. */
export function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "Доброй ночи";
  if (h < 12) return "Доброе утро";
  if (h < 18) return "Добрый день";
  return "Добрый вечер";
}
