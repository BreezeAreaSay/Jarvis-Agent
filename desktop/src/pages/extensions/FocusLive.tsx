// Живая панель «Фокуса» на карточке плагина: большое кольцо таймера с фазой, кнопки (начать, пауза,
// перерыв, закончить), заготовки 25/50/90 минут, минуты сегодня и столбики за неделю, список отвлекающих
// программ с быстрым редактированием и режимом «напоминать / закрывать».
import { CircleStop, Coffee, Flame, Pause, Play, Plus, ShieldCheck, SkipForward, Trophy, X } from "lucide-react";
import { useState } from "react";
import { AppIcon } from "@/components/AppIcon";
import { Button, cx, notifyError } from "@/ui";
import {
  daysText,
  FocusRing,
  focusClock,
  focusLabel,
  focusTone,
  fmtMinutes,
  leftFrac,
  sessionsText,
  useFocusLeft,
  useFocusState,
  WeekBars,
} from "./focusKit";
import { type Plugin, plural } from "./pluginKit";
import { usePlugins } from "./pluginStore";

const PRESETS = [25, 50, 90];
const GUARD_MODES = [
  { value: "remind", label: "Напоминать" },
  { value: "close", label: "Закрывать" },
  { value: "off", label: "Выкл." },
] as const;

function splitApps(value: unknown): string[] {
  const seen = new Set<string>();
  return String(value ?? "")
    .split(/[,;\n]/)
    .map((x) => x.trim())
    .filter((x) => x && !seen.has(x.toLowerCase()) && seen.add(x.toLowerCase()));
}

/** Кружки серии до длинного перерыва: ●●◐○ — сделано, текущий, впереди. */
function SeriesDots({ cycle, every, current }: { cycle: number; every: number; current: boolean }) {
  const n = Math.max(2, Math.min(8, every || 4));
  const pos = cycle > 0 ? ((cycle - 1) % n) + 1 : 0;
  const done = current ? pos - 1 : pos;
  return (
    <span className="fx-dots" aria-label={cycle ? `Фокус ${pos} из ${n} до длинного перерыва` : undefined}>
      {Array.from({ length: n }, (_, i) => (
        <i key={i} className={cx(i < done && "done", current && i === pos - 1 && "now")} />
      ))}
    </span>
  );
}

function GuardEditor({ plugin }: { plugin: Plugin }) {
  const update = usePlugins((s) => s.update);
  const apps = splitApps(plugin.config?.apps);
  const mode = String(plugin.config?.guard ?? "remind");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const save = (patch: Record<string, unknown>) => update(plugin.id, { config: patch }).catch(notifyError);
  const add = () => {
    const extra = splitApps(draft);
    setDraft("");
    setAdding(false);
    if (extra.length) save({ apps: splitApps([...apps, ...extra].join(", ")).join(", ") });
  };
  return (
    <div className="fx-guard">
      <div className="fx-guard-head">
        <ShieldCheck />
        <span>Отвлекающие программы</span>
        <span className="spacer" />
        <div className="fx-seg" role="radiogroup" aria-label="Что делать с отвлекающими программами">
          {GUARD_MODES.map((m) => (
            <button
              key={m.value}
              role="radio"
              aria-checked={mode === m.value}
              className={cx(mode === m.value && "on")}
              onClick={() => mode !== m.value && save({ guard: m.value })}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
      <div className="fx-apps">
        {apps.map((app) => (
          <span key={app} className="fx-app">
            <AppIcon name={app} size={18} radius={5} />
            <span className="ellipsis">{app}</span>
            <button
              className="fx-app-x"
              aria-label={`Убрать ${app}`}
              title="Убрать из списка"
              onClick={() => save({ apps: apps.filter((a) => a !== app).join(", ") })}
            >
              <X />
            </button>
          </span>
        ))}
        {adding ? (
          <input
            className="fx-app-input"
            autoFocus
            value={draft}
            placeholder="Например, WhatsApp"
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") add();
              if (e.key === "Escape") {
                setDraft("");
                setAdding(false);
              }
            }}
            onBlur={add}
          />
        ) : (
          <button className="fx-app-add" onClick={() => setAdding(true)}>
            <Plus /> Добавить
          </button>
        )}
        {!apps.length && !adding && <span className="faint small">Список пуст — Jarvis ни о чём не напомнит</span>}
      </div>
    </div>
  );
}

export function FocusLive({ plugin }: { plugin: Plugin }) {
  const { state, at } = useFocusState(plugin);
  const left = useFocusLeft(state, at);
  const [busy, setBusy] = useState("");
  const live = plugin.enabled && plugin.status?.running !== false;
  const phase = state?.phase ?? "idle";
  const tone = focusTone(state);
  const waiting = phase === "idle" && !!state?.waiting;
  const focusMin = Number(plugin.config?.focus_min ?? state?.focus_min ?? 25);
  const breakMin = Number(state?.break_min ?? plugin.config?.break_min ?? 5);
  const longMin = Number(state?.long_break_min ?? plugin.config?.long_break_min ?? 15);

  const act = async (action: string, args: Record<string, unknown> = {}) => {
    setBusy(action + JSON.stringify(args));
    try {
      await usePlugins.getState().action(plugin.id, action, args);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy("");
    }
  };
  const isBusy = (action: string, args: Record<string, unknown> = {}) => busy === action + JSON.stringify(args);

  let headline: string;
  let sub: string;
  switch (phase) {
    case "focus":
      headline = "Работаем — Jarvis не отвлекает";
      sub = state?.next === "long_break" ? `Потом длинный перерыв — ${fmtMinutes(longMin)}` : `Потом перерыв — ${fmtMinutes(breakMin)}`;
      break;
    case "break":
      headline = state?.long_break ? "Длинный перерыв — заслужили" : "Отдохните: встаньте, разомнитесь";
      sub = `Потом фокус — ${fmtMinutes(focusMin)}`;
      break;
    case "paused":
      headline = state?.reason === "idle" ? "Вас не было — фокус ждёт" : state?.paused_from === "break" ? "Перерыв на паузе" : "Фокус на паузе";
      sub = state?.paused_from === "break" ? "Перерыв продолжится, когда скажете" : `До перерыва — ${fmtMinutes(left / 60)}`;
      break;
    default:
      headline = waiting ? "Перерыв окончен — продолжим?" : "Глубокая работа без отвлечений";
      sub = waiting
        ? `Следующий фокус — ${fmtMinutes(focusMin)}`
        : `Jarvis притихнет и присмотрит за отвлечениями — ${fmtMinutes(focusMin)} фокуса`;
  }

  const controls = (() => {
    const dis = !live;
    const stop = (
      <Button size="sm" variant="ghost" icon={<CircleStop />} disabled={dis} loading={isBusy("stop")} onClick={() => act("stop")}>
        Закончить
      </Button>
    );
    switch (phase) {
      case "focus":
        return (
          <>
            <Button variant="primary" icon={<Pause />} disabled={dis} loading={isBusy("pause")} onClick={() => act("pause")}>
              Пауза
            </Button>
            <Button size="sm" icon={<Coffee />} disabled={dis} loading={isBusy("break")} onClick={() => act("break")}>
              Перерыв
            </Button>
            {stop}
          </>
        );
      case "break":
        return (
          <>
            <Button variant="primary" icon={<SkipForward />} disabled={dis} loading={isBusy("skip_break")} onClick={() => act("skip_break")}>
              Пропустить перерыв
            </Button>
            <Button size="sm" icon={<Pause />} disabled={dis} loading={isBusy("pause")} onClick={() => act("pause")}>
              Пауза
            </Button>
            {stop}
          </>
        );
      case "paused":
        return (
          <>
            <Button variant="primary" icon={<Play />} disabled={dis} loading={isBusy("resume")} onClick={() => act("resume")}>
              Продолжить
            </Button>
            {stop}
          </>
        );
      default:
        return waiting ? (
          <>
            <Button variant="primary" icon={<Play />} disabled={dis} loading={isBusy("resume")} onClick={() => act("resume")}>
              Следующий фокус
            </Button>
            {stop}
          </>
        ) : (
          <Button variant="primary" icon={<Play />} disabled={dis} loading={isBusy("start")} onClick={() => act("start")}>
            Начать фокус
          </Button>
        );
    }
  })();

  const status = state;
  const todayMin = status?.today_min ?? 0;
  const sessions = status?.today_sessions ?? 0;
  const streak = status?.streak ?? 0;
  const best = status?.best;
  const distractions = status?.distractions ?? 0;
  const timerText = phase === "idle" ? focusClock(focusMin * 60) : focusClock(left);

  return (
    <div className={cx("lv fx-live", `fxt-${tone}`)}>
      <div className="fx-live-top">
        <FocusRing size={176} frac={leftFrac(state, left)} tone={tone} stroke={6.5}>
          <span className="fx-ring-time">{timerText}</span>
          <span className="fx-ring-phase">{focusLabel(state)}</span>
        </FocusRing>
        <div className="fx-live-side">
          <div className="fx-eyebrow">
            <span className="fx-dot" />
            {phase === "idle" && !waiting ? "Помодоро" : `Фокус № ${Math.max(1, state?.cycle ?? 1)}`}
            <SeriesDots cycle={state?.cycle ?? 0} every={state?.long_every ?? 4} current={phase === "focus" || state?.paused_from === "focus"} />
          </div>
          <div className="fx-headline">{headline}</div>
          <div className="fx-sub">{plugin.enabled ? sub : "Включите плагин переключателем выше — и начните первый фокус"}</div>
          <div className="fx-controls">{controls}</div>
          <div className="fx-presets">
            <span className="faint small">Фокус на</span>
            {PRESETS.map((m) => (
              <button
                key={m}
                className={cx("fx-preset", m === focusMin && phase === "idle" && "on", phase === "focus" && state?.total_s === m * 60 && "on")}
                disabled={!live || isBusy("start", { minutes: m })}
                onClick={() => act("start", { minutes: m })}
                title={phase === "focus" ? `Сделать текущий фокус ${m}-минутным` : `Начать фокус на ${m} минут`}
              >
                {m}
              </button>
            ))}
            <span className="faint small">мин</span>
          </div>
        </div>
      </div>

      <div className="fx-live-stats">
        <div className="fx-today">
          <div className="fx-today-value">{fmtMinutes(todayMin)}</div>
          <div className="fx-today-label">в фокусе сегодня</div>
          <div className="fx-chips">
            <span className="chip mini">{sessions ? sessionsText(sessions) : "Сессий пока нет"}</span>
            {streak > 0 && (
              <span className="chip mini" title="Дней подряд хотя бы с одной сессией">
                <Flame /> {daysText(streak)} подряд
              </span>
            )}
            {best && best.min > 0 && (
              <span className="chip mini" title="Лучший день">
                <Trophy /> рекорд {fmtMinutes(best.min)}
              </span>
            )}
            {distractions > 0 && (
              <span className="chip mini" title="Сегодня Jarvis остановил отвлечения">
                <ShieldCheck /> {distractions} {plural(distractions, "отвлечение", "отвлечения", "отвлечений")}
              </span>
            )}
          </div>
        </div>
        <WeekBars days={status?.week} className="fx-week-panel" />
      </div>

      <GuardEditor plugin={plugin} />
    </div>
  );
}
