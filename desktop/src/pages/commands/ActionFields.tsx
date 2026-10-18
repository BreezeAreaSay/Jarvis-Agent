// Поля значений действий: по виду параметра — текст, число, клавиши (с записью сочетания), путь (с выбором
// файла), программа из «Пуска», адрес, список, координаты; плюс «значение из фразы» ({number} …).
import {
  Check,
  ChevronDown,
  Crosshair,
  ExternalLink,
  FolderOpen,
  Keyboard,
  RefreshCw,
  Search,
  TriangleAlert,
} from "lucide-react";
import { type KeyboardEvent as ReactKeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { ActionParam, ActionType, AppEntry } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { bridge, isElectron } from "@/lib/bridge";
import { cx, notifyError } from "@/ui";
import {
  basename,
  comboFromEvent,
  findApp,
  formatKeys,
  groupCatalog,
  isModifierEvent,
  norm,
  normalizeKeys,
  SLOTS,
  slotLabel,
  slotOf,
} from "./model";
import { Popover } from "./Overlays";
import { useApps } from "./store";

// --- выбор типа действия -------------------------------------------------------------------------------
export function ActionTypeSelect({
  value,
  catalog,
  onChange,
  autoOpen,
  onAutoOpened,
}: {
  value: string;
  catalog: ActionType[];
  onChange: (type: string) => void;
  autoOpen?: boolean;
  onAutoOpened?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const btn = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const current = catalog.find((c) => c.type === value);

  const groups = useMemo(() => {
    const words = norm(q).split(/\s+/).filter(Boolean);
    if (!words.length) return groupCatalog(catalog);
    // сначала совпадения в названии, потом в типе и категории, в конце — в описании
    const rank = (c: ActionType): number => {
      const title = norm(c.title);
      const type = norm(c.type);
      const cat = norm(c.category);
      const desc = norm(c.description);
      let worst = 0;
      for (const w of words) {
        const r = title.startsWith(w) || title.includes(` ${w}`)
          ? 0
          : title.includes(w)
            ? 1
            : type.includes(w)
              ? 2
              : cat.includes(w)
                ? 3
                : desc.includes(w)
                  ? 4
                  : 9;
        worst = Math.max(worst, r);
      }
      return worst;
    };
    const ranked = catalog
      .map((c, i) => ({ c, i, r: rank(c) }))
      .filter((x) => x.r < 9)
      .sort((x, y) => x.r - y.r || x.i - y.i)
      .map((x) => x.c);
    return groupCatalog(ranked);
  }, [catalog, q]);
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  useEffect(() => {
    if (!autoOpen) return;
    setOpen(true);
    onAutoOpened?.();
  }, [autoOpen, onAutoOpened]);
  useEffect(() => {
    if (!open) return;
    setQ("");
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const i = q ? 0 : Math.max(0, flat.findIndex((c) => c.type === value));
    setActive(i);
    // при открытии выбранный пункт — посередине списка
    if (!q)
      requestAnimationFrame(() =>
        list.current?.querySelector<HTMLElement>(`[data-idx="${i}"]`)?.scrollIntoView({ block: "center" }),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, q]);
  useEffect(() => {
    if (!open) return;
    list.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const pick = (type: string) => {
    setOpen(false);
    onChange(type);
    btn.current?.focus();
  };
  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(flat.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (flat[active]) pick(flat[active].type);
    }
  };

  return (
    <>
      <button
        ref={btn}
        type="button"
        className={cx("act-type", !value && "placeholder")}
        title={current ? `${current.title}${current.description ? ` — ${current.description}` : ""}` : "Выберите действие"}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span className="ellipsis">{value || "Действие"}</span>
        <ChevronDown />
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} width={340} maxHeight={380} className="act-menu">
        <div className="pop-search">
          <Search />
          <input
            autoFocus
            value={q}
            placeholder="Найти действие"
            spellCheck={false}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKey}
            aria-label="Найти действие"
          />
        </div>
        <div className="pop-list" ref={list} role="listbox" aria-label="Действия">
          {groups.map((g) => (
            <div key={g.category} role="group" aria-label={g.category}>
              <div className="pop-group">{g.category}</div>
              {g.items.map((it) => {
                const idx = flat.indexOf(it);
                return (
                  <button
                    key={it.type}
                    type="button"
                    role="option"
                    data-idx={idx}
                    aria-selected={it.type === value}
                    className={cx("pop-item", idx === active && "focus", it.type === value && "selected")}
                    onMouseMove={() => idx !== active && setActive(idx)}
                    onClick={() => pick(it.type)}
                  >
                    <AppIcon name={it.icon} size={26} radius={8} />
                    <span className="grow">
                      <span className="pop-title ellipsis">{it.title}</span>
                      <span className="pop-sub ellipsis">{it.type}</span>
                    </span>
                    {it.type === value && <Check className="pop-check" />}
                  </button>
                );
              })}
            </div>
          ))}
          {!flat.length && <div className="pop-empty">Такого действия нет</div>}
        </div>
      </Popover>
    </>
  );
}

// --- поля значения ------------------------------------------------------------------------------------
export interface FieldProps {
  value: string;
  onChange: (v: string) => void;
  param?: ActionParam;
  at: ActionType;
  invalid?: boolean;
}

/** Ячейки «значение» и «инструмент» строки действия. */
export function ValueCells({
  value,
  onChange,
  param,
  at,
  linked,
  slotMissing,
  invalid,
}: FieldProps & { linked: boolean; slotMissing: boolean }) {
  const [capturing, setCapturing] = useState(false);
  useEffect(() => setCapturing(false), [param?.kind, linked]);
  let field: ReactNode;
  let tool: ReactNode = null;

  if (!param) {
    field = <span className="act-none ellipsis">{at.description || "Без настроек — просто выполняется"}</span>;
  } else if (linked) {
    field = <SlotField value={value} onChange={onChange} />;
    if (slotMissing) {
      const s = slotOf(value) ?? "text";
      tool = (
        <span
          className="act-tool warn"
          title={`Во фразах активации нет {${s}} — добавьте его, например: «громкость {${s}}»`}
          aria-label="Нет слота во фразах"
        >
          <TriangleAlert />
        </span>
      );
    }
  } else {
    switch (param.kind) {
      case "keys":
        field = (
          <KeysField
            value={value}
            onChange={onChange}
            placeholder={param.placeholder}
            capturing={capturing}
            setCapturing={setCapturing}
          />
        );
        tool = (
          <button
            type="button"
            className={cx("act-tool", capturing && "on")}
            title={capturing ? "Идёт запись сочетания — нажмите клавиши" : "Записать сочетание клавиш"}
            aria-label="Записать сочетание клавиш"
            aria-pressed={capturing}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setCapturing((c) => !c)}
          >
            <Keyboard />
          </button>
        );
        break;
      case "path":
        field = <AppCombo mode="path" value={value} onChange={onChange} placeholder={param.placeholder} />;
        tool = <PickFileButton folder={at.type === "Launch.Folder" || /папк/i.test(param.label)} onPick={onChange} />;
        break;
      case "app":
        field = (
          <AppCombo
            mode={at.type === "App.Kill" ? "process" : "app"}
            value={value}
            onChange={onChange}
            placeholder={param.placeholder}
          />
        );
        tool = <RefreshAppsButton />;
        break;
      case "url":
        field = (
          <PlainInput value={value} onChange={onChange} placeholder={param.placeholder || "https://…"} type="url" />
        );
        tool = (
          <button
            type="button"
            className="act-tool"
            title="Открыть адрес в браузере"
            aria-label="Открыть адрес в браузере"
            disabled={!value.trim()}
            onClick={() => bridge.openExternal(/^[a-z]+:\/\//i.test(value) ? value : `https://${value}`)}
          >
            <ExternalLink />
          </button>
        );
        break;
      case "select":
        field = <OptionsField value={value} onChange={onChange} options={param.options ?? []} />;
        break;
      case "coords":
        field = (
          <PlainInput
            value={value}
            onChange={onChange}
            placeholder={param.placeholder || "960 540"}
            normalize={(v) => v.replace(/[,;x×]+/gi, " ").replace(/\s+/g, " ").trim()}
          />
        );
        tool = <PickPointButton onPick={onChange} />;
        break;
      case "number":
        field = (
          <PlainInput
            value={value}
            onChange={onChange}
            placeholder={param.placeholder}
            inputMode="decimal"
            filter={(v) => v.replace(/[^\d.,\-+ ]/g, "")}
          />
        );
        break;
      default:
        field = <PlainInput value={value} onChange={onChange} placeholder={param.placeholder} />;
    }
  }
  return (
    <>
      <div className={cx("act-cell act-value", invalid && "invalid", linked && "linked", capturing && "capturing")}>
        {field}
      </div>
      <div className="act-cell act-tool-cell">{tool}</div>
    </>
  );
}

function PlainInput({
  value,
  onChange,
  placeholder,
  type = "text",
  inputMode,
  filter,
  normalize,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  inputMode?: "decimal" | "numeric" | "text";
  filter?: (v: string) => string;
  normalize?: (v: string) => string;
}) {
  return (
    <input
      className="act-input"
      type={type}
      value={value}
      inputMode={inputMode}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(e) => onChange(filter ? filter(e.target.value) : e.target.value)}
      onBlur={() => normalize && onChange(normalize(value))}
    />
  );
}

// --- клавиши -----------------------------------------------------------------------------------------
function KeysField({
  value,
  onChange,
  placeholder,
  capturing,
  setCapturing,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  capturing: boolean;
  setCapturing: (v: boolean) => void;
}) {
  const [text, setText] = useState(formatKeys(value));
  const [focused, setFocused] = useState(false);
  const [held, setHeld] = useState("");
  const pressedMain = useRef(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!focused || capturing) setText(formatKeys(value));
  }, [value, focused, capturing]);
  useEffect(() => {
    if (capturing) {
      setHeld("");
      pressedMain.current = false;
      input.current?.focus();
    }
  }, [capturing]);

  const finish = (combo: string) => {
    if (combo) {
      onChange(combo);
      setText(formatKeys(combo));
    }
    setHeld("");
    setCapturing(false);
  };

  return (
    <input
      ref={input}
      className="act-input"
      value={capturing ? (held ? `${formatKeys(held)} + …` : "") : text}
      readOnly={capturing}
      placeholder={capturing ? "Нажмите сочетание клавиш… (Esc — отмена)" : placeholder || "ctrl + shift + s"}
      spellCheck={false}
      aria-label={capturing ? "Нажмите сочетание клавиш" : "Сочетание клавиш"}
      onFocus={() => setFocused(true)}
      onBlur={() => {
        setFocused(false);
        if (capturing) {
          setCapturing(false);
          setHeld("");
        } else if (normalizeKeys(text) !== value) onChange(normalizeKeys(text));
      }}
      onChange={(e) => setText(e.target.value)}
      onKeyDown={(e) => {
        if (!capturing) {
          if (e.key === "Enter") onChange(normalizeKeys(text));
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        if (e.repeat) return;
        const plain = !e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey;
        if (e.key === "Escape" && plain) {
          setCapturing(false);
          setHeld("");
          return;
        }
        if (isModifierEvent(e)) {
          setHeld(comboFromEvent(e));
          return;
        }
        pressedMain.current = true;
        finish(comboFromEvent(e));
      }}
      onKeyUp={(e) => {
        if (!capturing) return;
        e.preventDefault();
        // отпустили одни модификаторы — записываем их как есть («win»)
        if (!pressedMain.current && held && isModifierEvent(e)) finish(held);
      }}
    />
  );
}

// --- программа или путь --------------------------------------------------------------------------------
function AppCombo({
  mode,
  value,
  onChange,
  placeholder,
}: {
  /** app — название из «Пуска», path — путь к файлу, process — имя процесса (chrome.exe) */
  mode: "app" | "path" | "process";
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const apps = useApps((s) => s.apps);
  const [focused, setFocused] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrap = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const matched = findApp(apps, value);

  const options = useMemo(() => {
    const pool = mode === "app" ? apps : apps.filter((a) => a.path || a.app_id);
    const words = norm(value).split(/\s+/).filter(Boolean);
    const exact = matched && (mode === "app" ? norm(matched.name) === norm(value) : true);
    if (!words.length || exact) return pool.slice(0, 60);
    return pool
      .filter((a) => {
        const hay = norm(`${a.name} ${a.path} ${a.app_id}`);
        return words.every((w) => hay.includes(w));
      })
      .slice(0, 60);
  }, [apps, value, mode, matched]);

  useEffect(() => setActive(0), [value]);
  useEffect(() => {
    if (open) list.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const pick = (app: AppEntry) => {
    onChange(mode === "app" ? app.name : mode === "process" ? basename(app.path) || app.app_id : app.path || app.app_id);
    setOpen(false);
    input.current?.blur();
  };

  const showDisplay = !focused && !!value && (!!matched || mode !== "path");
  return (
    <div className="act-combo" ref={wrap}>
      {showDisplay ? (
        <button
          type="button"
          className="act-display"
          title={value}
          onClick={() => {
            setFocused(true);
            requestAnimationFrame(() => {
              input.current?.focus();
              input.current?.select();
            });
          }}
        >
          <AppIcon name={matched?.name ?? value} size={20} radius={6} />
          <span className="ellipsis">{matched?.name ?? value}</span>
          {mode !== "app" && matched && <span className="act-display-sub ellipsis">{basename(value)}</span>}
        </button>
      ) : (
        <input
          ref={input}
          className="act-input"
          value={value}
          placeholder={placeholder || (mode === "path" ? "Путь к файлу" : "Название программы")}
          spellCheck={false}
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          onFocus={() => {
            setFocused(true);
            setOpen(true);
          }}
          onBlur={() => {
            setFocused(false);
            // список закрывает Popover по щелчку мимо; по Tab — закрываем сами
            setTimeout(() => {
              if (!wrap.current?.contains(document.activeElement)) setOpen(false);
            }, 120);
          }}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setActive((i) => Math.min(options.length - 1, i + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => Math.max(0, i - 1));
            } else if (e.key === "Enter" && open && options[active]) {
              e.preventDefault();
              pick(options[active]);
            }
          }}
        />
      )}
      <Popover
        anchor={wrap}
        open={open && focused && options.length > 0}
        onClose={() => setOpen(false)}
        minWidth={300}
        maxHeight={300}
        restoreFocus={false}
        className="act-menu"
      >
        <div
          className="pop-list"
          ref={list}
          role="listbox"
          aria-label="Программы"
          onMouseDown={(e) => e.preventDefault()}
        >
          <div className="pop-group">{mode === "app" ? "Программы из меню «Пуск»" : "Установленные программы"}</div>
          {options.map((app, idx) => (
            <button
              key={`${app.app_id}-${idx}`}
              type="button"
              role="option"
              data-idx={idx}
              aria-selected={idx === active}
              className={cx("pop-item", idx === active && "focus")}
              onMouseDown={(e) => e.preventDefault()}
              onMouseMove={() => idx !== active && setActive(idx)}
              onClick={() => pick(app)}
            >
              <AppIcon name={app.name} size={26} radius={8} />
              <span className="grow">
                <span className="pop-title ellipsis">{app.name}</span>
                <span className="pop-sub ellipsis">{app.path || app.app_id}</span>
              </span>
            </button>
          ))}
        </div>
      </Popover>
    </div>
  );
}

function RefreshAppsButton() {
  const status = useApps((s) => s.status);
  return (
    <button
      type="button"
      className={cx("act-tool", status === "loading" && "spin")}
      title="Обновить список программ"
      aria-label="Обновить список программ"
      onClick={() => useApps.getState().load(true)}
    >
      <RefreshCw />
    </button>
  );
}

function PickFileButton({ folder, onPick }: { folder: boolean; onPick: (path: string) => void }) {
  return (
    <button
      type="button"
      className="act-tool"
      title={isElectron ? (folder ? "Выбрать папку" : "Выбрать файл") : "Выбор файла доступен в приложении Jarvis"}
      aria-label={folder ? "Выбрать папку" : "Выбрать файл"}
      disabled={!isElectron}
      onClick={async () => {
        try {
          const path = await bridge.pickFile(
            folder
              ? { title: "Выберите папку", folder: true }
              : {
                  title: "Выберите программу или файл",
                  filters: [
                    { name: "Программы", extensions: ["exe", "lnk", "bat", "cmd", "url"] },
                    { name: "Все файлы", extensions: ["*"] },
                  ],
                },
          );
          if (path) onPick(path);
        } catch (e) {
          notifyError(e);
        }
      }}
    >
      <FolderOpen />
    </button>
  );
}

type PointPicker = () => Promise<{ x: number; y: number } | null>;

function PickPointButton({ onPick }: { onPick: (v: string) => void }) {
  // мост пока не умеет выбирать точку на экране — кнопка оживёт, когда появится window.jarvis.pickPoint
  const pick = (window.jarvis as unknown as { pickPoint?: PointPicker } | undefined)?.pickPoint;
  return (
    <button
      type="button"
      className="act-tool"
      title={pick ? "Указать на экране" : "Указать на экране — доступно в приложении Jarvis"}
      aria-label="Указать на экране"
      disabled={!pick}
      onClick={async () => {
        try {
          const pt = await pick?.();
          if (pt) onPick(`${Math.round(pt.x)} ${Math.round(pt.y)}`);
        } catch (e) {
          notifyError(e);
        }
      }}
    >
      <Crosshair />
    </button>
  );
}

// --- список вариантов и слоты ---------------------------------------------------------------------------
function OptionsField({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const current = options.find((o) => o.value === value);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className="act-display"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={cx("ellipsis", !current && "faint")}>{current?.label ?? (value || "Выберите…")}</span>
        <ChevronDown className="act-display-chev" />
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} minWidth={220} className="act-menu">
        <div className="pop-list" role="listbox">
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === value}
              className={cx("pop-item compact", o.value === value && "selected")}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
                btn.current?.focus();
              }}
            >
              <span className="grow pop-title">{o.label}</span>
              {o.value === value && <Check className="pop-check" />}
            </button>
          ))}
        </div>
      </Popover>
    </>
  );
}

function SlotField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const slot = slotOf(value) ?? "text";
  return (
    <>
      <button
        ref={btn}
        type="button"
        className="act-display"
        title="Значение возьмётся из сказанной фразы"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="slot-tag">{`{${slot}}`}</span>
        <span className="ellipsis">{slotLabel(slot)} из фразы</span>
        <ChevronDown className="act-display-chev" />
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} minWidth={260} className="act-menu">
        <div className="pop-list" role="listbox">
          <div className="pop-group">Взять из фразы</div>
          {SLOTS.map((s) => (
            <button
              key={s.key}
              type="button"
              role="option"
              aria-selected={s.key === slot}
              className={cx("pop-item compact", s.key === slot && "selected")}
              onClick={() => {
                onChange(`{${s.key}}`);
                setOpen(false);
                btn.current?.focus();
              }}
            >
              <span className="slot-tag">{`{${s.key}}`}</span>
              <span className="grow">
                <span className="pop-title">{s.label}</span>
                <span className="pop-sub">{s.hint}</span>
              </span>
              {s.key === slot && <Check className="pop-check" />}
            </button>
          ))}
        </div>
      </Popover>
    </>
  );
}

/** Слот по умолчанию для вида параметра. */
export function defaultSlot(kind: ActionParam["kind"] | undefined): string {
  switch (kind) {
    case "number":
      return "{number}";
    case "app":
      return "{app}";
    case "url":
      return "{site}";
    default:
      return "{text}";
  }
}
