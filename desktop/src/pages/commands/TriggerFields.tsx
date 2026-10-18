// Конструктор «Когда» в редакторе сценария: меню «Добавить триггер», строки триггеров со своими формами
// (время и дни, минуты, программа из «Пуска», фразы, заряд, минуты до события).
import { Check as CheckIcon, ChevronDown, CirclePlus, Clock3, RefreshCw, Search, X } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { AppEntry, ScenarioTrigger, ScenarioTriggerType } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { Check, cx } from "@/ui";
import { findApp, norm } from "./model";
import { Popover } from "./Overlays";
import { WeekdayChips } from "./ScenarioParts";
import { TRIGGER_ORDER, TRIGGERS, triggerLabel } from "./scenarioModel";
import { useApps } from "./store";

export type DraftTrigger = ScenarioTrigger & { key: string };

// --- меню «Добавить триггер» -------------------------------------------------------------------------------
export function AddTriggerMenu({
  onAdd,
  children,
  className,
}: {
  onAdd: (type: ScenarioTriggerType) => void;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className={cx("btn", className)}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {children}
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} width={330} maxHeight={430}>
        <div className="pop-list" role="menu" aria-label="Тип триггера">
          <div className="pop-group">Когда запускать</div>
          {TRIGGER_ORDER.map((type) => {
            const meta = TRIGGERS[type];
            const Icon = meta.icon;
            return (
              <button
                key={type}
                type="button"
                role="menuitem"
                className="pop-item sc-trig-option"
                onClick={() => {
                  setOpen(false);
                  onAdd(type);
                }}
              >
                <span className={cx("sc-trig-ico", `t-${type}`)}>
                  <Icon />
                </span>
                <span className="grow">
                  <span className="pop-title">{meta.title}</span>
                  <span className="pop-sub">{meta.hint}</span>
                </span>
              </button>
            );
          })}
        </div>
      </Popover>
    </>
  );
}

/** Пусто в «Когда»: самые частые триггеры — одной кнопкой. */
export function TriggerQuickStart({ onAdd }: { onAdd: (type: ScenarioTriggerType) => void }) {
  const quick: ScenarioTriggerType[] = ["time", "phrase", "app_open", "idle_return"];
  return (
    <div className="sc-when-empty">
      <div className="sc-when-empty-text">Когда Jarvis должен запускать сценарий?</div>
      <div className="ed-quick">
        {quick.map((type) => {
          const Icon = TRIGGERS[type].icon;
          return (
            <button key={type} type="button" className="ed-quick-btn" onClick={() => onAdd(type)}>
              <Icon />
              {TRIGGERS[type].title}
            </button>
          );
        })}
        <AddTriggerMenu onAdd={onAdd} className="ed-quick-btn sc-more-btn">
          Ещё…
          <ChevronDown />
        </AddTriggerMenu>
      </div>
    </div>
  );
}

// --- строка триггера -------------------------------------------------------------------------------------
export function TriggerRow({
  trigger,
  error,
  autoFocus,
  onChange,
  onRemove,
}: {
  trigger: DraftTrigger;
  error: string;
  autoFocus?: boolean;
  onChange: (t: DraftTrigger) => void;
  onRemove: () => void;
}) {
  const meta = TRIGGERS[trigger.type];
  const Icon = meta.icon;
  return (
    <div className={cx("sc-trig", error && "invalid")} data-trigger={trigger.key}>
      <div className="sc-trig-head">
        <span className={cx("sc-trig-ico", `t-${trigger.type}`)}>
          <Icon />
        </span>
        <span className="sc-trig-title">{meta.title}</span>
        <span className="sc-trig-label ellipsis">· {triggerLabel(trigger)}</span>
        <span className="spacer" />
        <button type="button" className="sc-trig-x" aria-label={`Убрать «${meta.title}»`} title="Убрать" onClick={onRemove}>
          <X />
        </button>
      </div>
      <div className="sc-trig-form">
        <TriggerForm trigger={trigger} autoFocus={autoFocus} onChange={onChange} />
      </div>
      {error && <div className="sc-trig-err">{error}</div>}
    </div>
  );
}

function TriggerForm({
  trigger: t,
  autoFocus,
  onChange,
}: {
  trigger: DraftTrigger;
  autoFocus?: boolean;
  onChange: (t: DraftTrigger) => void;
}) {
  switch (t.type) {
    case "time":
      return (
        <>
          <TimeInput value={t.at} label="Время запуска" autoFocus={autoFocus} onChange={(at) => onChange({ ...t, at })} />
          <WeekdayChips value={t.weekdays} onChange={(weekdays) => onChange({ ...t, weekdays })} />
          <span className="sc-presets">
            {(
              [
                ["Будни", [0, 1, 2, 3, 4]],
                ["Выходные", [5, 6]],
                ["Каждый день", []],
              ] as [string, number[]][]
            ).map(([label, days]) => (
              <button
                key={label}
                type="button"
                className={cx("sc-preset", t.weekdays.join() === days.join() && "on")}
                onClick={() => onChange({ ...t, weekdays: days })}
              >
                {label}
              </button>
            ))}
          </span>
        </>
      );
    case "interval":
      return (
        <>
          <span className="sc-form-text">Каждые</span>
          <NumberBox value={t.minutes} min={1} max={1440} suffix="мин" autoFocus={autoFocus} onChange={(minutes) => onChange({ ...t, minutes })} />
          <Check chip checked={t.only_active} onChange={(only_active) => onChange({ ...t, only_active })}>
            Считать только время за компьютером
          </Check>
        </>
      );
    case "phrase":
      return <PhraseTags value={t.phrases} autoFocus={autoFocus} onChange={(phrases) => onChange({ ...t, phrases })} />;
    case "jarvis_start":
      return <span className="sc-form-note">Сработает сразу после запуска Jarvis — например, когда включился компьютер.</span>;
    case "app_open":
    case "app_close":
      return (
        <>
          <span className="sc-form-text">{t.type === "app_open" ? "Когда открывается" : "Когда закрывается"}</span>
          <AppPicker value={t.app} autoOpen={autoFocus} onChange={(app) => onChange({ ...t, app })} />
        </>
      );
    case "idle_return":
      return (
        <>
          <span className="sc-form-text">Вас не было за компьютером дольше</span>
          <NumberBox value={t.after_min} min={1} max={1440} suffix="мин" autoFocus={autoFocus} onChange={(after_min) => onChange({ ...t, after_min })} />
        </>
      );
    case "battery_low":
      return (
        <>
          <span className="sc-form-text">Заряд опустился до</span>
          <NumberBox value={t.percent} min={1} max={99} suffix="%" autoFocus={autoFocus} onChange={(percent) => onChange({ ...t, percent })} />
          <span className="sc-form-note">один раз за разрядку</span>
        </>
      );
    case "calendar_event":
      return (
        <>
          <span className="sc-form-text">За</span>
          <NumberBox value={t.before_min} min={0} max={1440} suffix="мин" autoFocus={autoFocus} onChange={(before_min) => onChange({ ...t, before_min })} />
          <span className="sc-form-text">до начала события календаря</span>
        </>
      );
  }
}

// --- поля ----------------------------------------------------------------------------------------------
/** «930» → «09:30», «18» → «18:00», «7.5» → null (неверно). */
export function normalizeTime(raw: string): string | null {
  const s = raw.trim().replace(/[.,]/g, ":");
  let h: number;
  let m: number;
  const parts = /^(\d{1,2}):(\d{1,2})$/.exec(s);
  if (parts) [h, m] = [Number(parts[1]), Number(parts[2])];
  else if (/^\d{1,2}$/.test(s)) [h, m] = [Number(s), 0];
  else if (/^\d{3,4}$/.test(s)) [h, m] = [Number(s.slice(0, -2)), Number(s.slice(-2))];
  else return null;
  if (h > 23 || m > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Время «ЧЧ:ММ» всегда в 24-часовом виде (у type="time" вид зависит от языка системы). ↑ ↓ — час или 5 минут. */
export function TimeInput({
  value,
  onChange,
  label,
  optional,
  autoFocus,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  /** можно оставить пустым (условия) */
  optional?: boolean;
  autoFocus?: boolean;
}) {
  const [text, setText] = useState(value);
  const [focused, setFocused] = useState(false);
  useEffect(() => {
    if (!focused) setText(value);
  }, [value, focused]);
  const commit = () => {
    const t = normalizeTime(text);
    if (t) {
      setText(t);
      if (t !== value) onChange(t);
    } else if (optional && !text.trim()) {
      if (value) onChange("");
    } else setText(value);
  };
  const step = (dir: 1 | -1, minutes: boolean) => {
    const [h, m] = (normalizeTime(text) ?? (value || "09:00")).split(":").map(Number);
    const total = (h * 60 + (minutes ? Math.round(m / 5) * 5 + dir * 5 : m + dir * 60) + 1440) % 1440;
    const t = `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
    setText(t);
    onChange(t);
  };
  return (
    <span className={cx("sc-timebox", !optional && !normalizeTime(text) && "invalid")}>
      <Clock3 />
      <input
        className="sc-time-input"
        value={text}
        placeholder={optional ? "--:--" : "09:00"}
        inputMode="numeric"
        maxLength={5}
        spellCheck={false}
        autoFocus={autoFocus}
        aria-label={label}
        onFocus={() => setFocused(true)}
        onBlur={() => {
          setFocused(false);
          commit();
        }}
        onChange={(e) => {
          const next = e.target.value.replace(/[^\d:.,]/g, "").slice(0, 5);
          setText(next);
          // готовое «ЧЧ:ММ» сразу попадает в черновик — Ctrl+S не потеряет его
          const t = /^\d{1,2}[:.,]\d{2}$/.test(next) ? normalizeTime(next) : null;
          if (t && t !== value) onChange(t);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
            e.preventDefault();
            step(e.key === "ArrowUp" ? 1 : -1, (e.currentTarget.selectionStart ?? 0) > 2);
          }
        }}
      />
    </span>
  );
}

export function NumberBox({
  value,
  onChange,
  min,
  max,
  suffix,
  autoFocus,
}: {
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  suffix?: string;
  autoFocus?: boolean;
}) {
  const bad = !Number.isInteger(value) || value < min || value > max;
  return (
    <span className={cx("sc-num", bad && "invalid")}>
      <input
        className="sc-num-input"
        inputMode="numeric"
        value={Number.isFinite(value) ? String(value) : ""}
        autoFocus={autoFocus}
        aria-label={suffix ? `Значение, ${suffix}` : "Значение"}
        onChange={(e) => {
          const digits = e.target.value.replace(/[^\d]/g, "").slice(0, 4);
          onChange(digits ? Number(digits) : Number.NaN);
        }}
      />
      {suffix && <span className="sc-num-suffix">{suffix}</span>}
    </span>
  );
}

function PhraseTags({
  value,
  onChange,
  autoFocus,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  autoFocus?: boolean;
}) {
  const [text, setText] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const commit = (raw = text) => {
    const parts = raw
      .split(/[\n;]+/)
      .map((p) => p.trim().replace(/\s+/g, " "))
      .filter(Boolean);
    if (!parts.length) return;
    const seen = new Set(value.map(norm));
    const next = [...value];
    for (const p of parts) {
      if (!seen.has(norm(p))) {
        seen.add(norm(p));
        next.push(p);
      }
    }
    onChange(next);
    setText("");
  };
  return (
    <div className="sc-tags" onClick={() => input.current?.focus()}>
      {value.map((p, i) => (
        <span key={`${p}-${i}`} className="sc-tag">
          <span className="ellipsis">{p}</span>
          <button
            type="button"
            aria-label={`Убрать фразу «${p}»`}
            onClick={(e) => {
              e.stopPropagation();
              onChange(value.filter((_, j) => j !== i));
            }}
          >
            <X />
          </button>
        </span>
      ))}
      <input
        ref={input}
        className="sc-tags-input"
        value={text}
        autoFocus={autoFocus}
        spellCheck={false}
        placeholder={value.length ? "Ещё фраза — Enter" : "Например: доброе утро — и Enter"}
        aria-label="Фраза для запуска"
        onChange={(e) => setText(e.target.value)}
        onBlur={() => commit()}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Backspace" && !text && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onPaste={(e) => {
          const pasted = e.clipboardData.getData("text");
          if (/[\n;]/.test(pasted)) {
            e.preventDefault();
            commit(text + pasted);
          }
        }}
      />
    </div>
  );
}

/** Программа из «Пуска» (или своё имя процесса: «game.exe»). */
export function AppPicker({
  value,
  onChange,
  autoOpen,
}: {
  value: string;
  onChange: (v: string) => void;
  autoOpen?: boolean;
}) {
  const apps = useApps((s) => s.apps);
  const status = useApps((s) => s.status);
  const [open, setOpen] = useState(!!autoOpen && !value);
  const [q, setQ] = useState("");
  const btn = useRef<HTMLButtonElement>(null);
  const matched = findApp(apps, value);
  const list = useMemo(() => {
    const words = norm(q).split(/\s+/).filter(Boolean);
    const pool = words.length ? apps.filter((a) => words.every((w) => norm(`${a.name} ${a.path}`).includes(w))) : apps;
    return pool.slice(0, 80);
  }, [apps, q]);
  const pick = (name: string) => {
    onChange(name);
    setOpen(false);
    setQ("");
    btn.current?.focus();
  };
  const custom = q.trim();
  return (
    <>
      <button
        ref={btn}
        type="button"
        className={cx("sc-app-btn", !value && "placeholder")}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {value ? <AppIcon name={matched?.name ?? value} size={22} radius={6} /> : null}
        <span className="ellipsis">{value ? (matched?.name ?? value) : "Выберите программу"}</span>
        <ChevronDown className="sc-app-chev" />
      </button>
      <Popover anchor={btn} open={open} onClose={() => setOpen(false)} width={340} maxHeight={380} className="act-menu">
        <div className="pop-search">
          <Search />
          <input
            autoFocus
            value={q}
            placeholder="Найти программу или ввести exe"
            spellCheck={false}
            aria-label="Найти программу"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (list[0]) pick(list[0].name);
                else if (custom) pick(custom);
              }
            }}
          />
          <button
            type="button"
            className={cx("sc-pop-refresh", status === "loading" && "spin")}
            title="Обновить список программ"
            aria-label="Обновить список программ"
            onClick={() => useApps.getState().load(true)}
          >
            <RefreshCw />
          </button>
        </div>
        <div className="pop-list" role="listbox" aria-label="Программы">
          {custom && !list.some((a) => norm(a.name) === norm(custom)) && (
            <button type="button" role="option" aria-selected={false} className="pop-item compact" onClick={() => pick(custom)}>
              <span className="pop-plus">
                <CirclePlus />
              </span>
              <span className="grow pop-title ellipsis">Использовать «{custom}»</span>
            </button>
          )}
          <div className="pop-group">Программы из меню «Пуск»</div>
          {list.map((app: AppEntry, i) => (
            <button
              key={`${app.app_id}-${i}`}
              type="button"
              role="option"
              aria-selected={app.name === value}
              className={cx("pop-item", app.name === value && "selected")}
              onClick={() => pick(app.name)}
            >
              <AppIcon name={app.name} size={26} radius={8} />
              <span className="grow">
                <span className="pop-title ellipsis">{app.name}</span>
                <span className="pop-sub ellipsis">{app.path || app.app_id}</span>
              </span>
              {app.name === value && <CheckIcon className="pop-check" />}
            </button>
          ))}
          {!list.length && !custom && <div className="pop-empty">{status === "loading" ? "Загружаю…" : "Программ не нашлось"}</div>}
        </div>
      </Popover>
    </>
  );
}
