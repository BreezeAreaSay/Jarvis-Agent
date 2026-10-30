// «ФОНОВЫЕ ЗАДАЧИ» (#/background/<задача>): большие задачи, которые агент делает сам, в фоне, без экрана
// человека — ищет, читает сайты, работает в своём браузере и пишет итог в заметки (Obsidian). Слева — задачи,
// справа — выбранная: вопрос агента (ответить), ход работы и план, «Браузер» (живая картинка его вкладки, можно
// взять управление), источники, сама задача; у повторяющейся — расписание, «Запуски» и «Замеры» (таблицы с
// графиком). #/background/browser — браузер агента без задачи: войти на сайты.
// Ядро — /api/background, события background.* (docs/CONTRACTS.md §8).
import {
  AppWindow,
  Ban,
  BookOpen,
  CalendarClock,
  ChartLine,
  Check,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleHelp,
  Clock,
  ExternalLink,
  Globe,
  History,
  ListChecks,
  Loader2,
  MessageCircleQuestion,
  Pause,
  Play,
  Plus,
  Repeat,
  RotateCcw,
  Send,
  Settings,
  Telescope,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import { PageHeader } from "@/components/PageHeader";
import { bridge } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import {
  ACTIVE,
  type TaskCard,
  type TaskFull,
  type TaskQuestion,
  type TaskRepeat,
  type TaskStatus,
  useBackground,
  wireBackground,
} from "@/store/background";
import { Button, Check as CheckBox, cx, Empty, IconButton, Modal, notify, notifyError, Tabs, timeAgo } from "@/ui";
import { AgentBrowserView } from "./AgentBrowserView";
import { MeasureView } from "./MeasureView";
import { fmtWhen, RepeatPicker } from "./repeat";
import "@/styles/background.css";

const STATUS: Record<TaskStatus, { label: string; icon: ReactNode; tone: string }> = {
  queued: { label: "В очереди", icon: <Clock />, tone: "muted" },
  running: { label: "Работает", icon: <Loader2 className="spin" />, tone: "accent" },
  waiting: { label: "Ждёт ответа", icon: <CircleHelp />, tone: "warn" },
  done: { label: "Готово", icon: <CircleCheck />, tone: "ok" },
  partial: { label: "Сделано не всё", icon: <TriangleAlert />, tone: "warn" },
  failed: { label: "Не получилось", icon: <CircleAlert />, tone: "err" },
  cancelled: { label: "Отменено", icon: <Ban />, tone: "muted" },
  scheduled: { label: "По расписанию", icon: <CalendarClock />, tone: "accent" },
  paused: { label: "На паузе", icon: <Pause />, tone: "muted" },
};

const EXAMPLES: { text: string; repeat?: TaskRepeat }[] = [
  { text: "Сравни 5 популярных ноутбуков до 100 000 ₽ для программирования: цены в трёх магазинах, плюсы и минусы — таблицей" },
  { text: "Собери главные новости про ИИ за неделю с источниками и коротким выводом" },
  { text: "Записывай курс доллара ЦБ в таблицу замеров", repeat: { kind: "daily", at: "20:00", weekdays: [] } },
  { text: "Проверяй цену на RTX 5070 в DNS и Ситилинке и записывай самую низкую", repeat: { kind: "hours", hours: 6 } },
];

function StatusChip({ status }: { status: TaskStatus }) {
  const s = STATUS[status];
  return (
    <span className={cx("chip", "bg-chip", s.tone)}>
      {s.icon}
      {s.label}
    </span>
  );
}

/** «Записал [[Jarvis/Исследования/Отчёт]]» → «Записал «Отчёт»»: ссылки Obsidian — названием заметки. */
function plain(text: string): string {
  return text.replace(/\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g, (_, path: string, alias?: string) => `«${alias || path.split("/").pop()}»`);
}

function openLink(url: string) {
  if (url) bridge.openExternal(url);
}

// --- список ------------------------------------------------------------------------------------------------
function rowNote(task: TaskCard): string {
  if (task.question) return `Вопрос: ${plain(task.question.text)}`;
  if (task.status === "scheduled" && task.next_run) return `Следующий запуск — ${fmtWhen(task.next_run)}`;
  if (task.status === "paused") return `Повторы на паузе · ${task.repeat_text}`;
  const last = task.progress[task.progress.length - 1]?.text ?? task.summary ?? "";
  return plain(last) || STATUS[task.status].label;
}

function TaskRow({ task, active }: { task: TaskCard; active: boolean }) {
  return (
    <button type="button" className={cx("bg-row", active && "active", task.status)} onClick={() => navigate("background", task.id)}>
      <span className={cx("bg-row-ico", STATUS[task.status].tone)}>{STATUS[task.status].icon}</span>
      <span className="bg-row-main">
        <span className="bg-row-title ellipsis">
          {task.repeat_text && <Repeat className="bg-row-repeat" aria-label={task.repeat_text} />}
          {task.title}
        </span>
        <span className="bg-row-sub ellipsis">{rowNote(task)}</span>
        {task.plan_total > 0 && ACTIVE.includes(task.status) && (
          <span className="bg-progress">
            <i style={{ width: `${(task.plan_done / task.plan_total) * 100}%` }} />
          </span>
        )}
      </span>
      <span className="bg-row-time faint tiny">{timeAgo(task.updated)}</span>
    </button>
  );
}

// --- новая задача -----------------------------------------------------------------------------------------
function NewTask({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [brief, setBrief] = useState("");
  const [deliverable, setDeliverable] = useState("");
  const [repeat, setRepeat] = useState<TaskRepeat>({});
  const [runNow, setRunNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const repeating = "kind" in repeat;
  const create = async () => {
    if (!brief.trim()) return;
    setBusy(true);
    try {
      // «Не повторять» — null: ядро само найдёт повтор в тексте («каждый вечер записывай …»)
      const r = await api.post<{ task: TaskCard }>("/api/background", {
        brief,
        deliverable,
        source: "ui",
        repeat: repeating ? repeat : null,
        run_now: runNow,
      });
      useBackground.getState().upsert(r.task);
      setBrief("");
      setDeliverable("");
      setRepeat({});
      setRunNow(true);
      onClose();
      navigate("background", r.task.id);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Новая фоновая задача"
      icon={<Telescope />}
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button variant="primary" icon={<Send />} loading={busy} disabled={!brief.trim()} onClick={create}>
            Поставить задачу
          </Button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        Опишите задачу подробно — агент возьмётся сам, в фоне, и запишет итог в заметки. Если что-то будет неясно
        или понадобится ваше решение, он спросит.
      </p>
      <textarea
        className="input bg-textarea"
        autoFocus
        rows={6}
        value={brief}
        placeholder="Что сделать: найти, сравнить, собрать, проследить…"
        onChange={(e) => setBrief(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void create();
        }}
      />
      <input
        className="input"
        style={{ marginTop: 10 }}
        value={deliverable}
        placeholder="Какой нужен результат (необязательно): отчёт, таблица, список…"
        onChange={(e) => setDeliverable(e.target.value)}
      />
      <div className="bg-new-repeat">
        <span className="small muted">Повторять</span>
        <RepeatPicker value={repeat} onChange={setRepeat} />
        {repeating && (
          <CheckBox checked={runNow} onChange={setRunNow}>
            Первый раз — сразу
          </CheckBox>
        )}
      </div>
      <div className="bg-examples">
        <span className="faint tiny">Например:</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex.text}
            type="button"
            className="bg-example"
            onClick={() => {
              setBrief(ex.text);
              setRepeat(ex.repeat ?? {});
            }}
          >
            {ex.repeat && <Repeat />}
            {ex.text}
          </button>
        ))}
      </div>
    </Modal>
  );
}

// --- вопрос агента ------------------------------------------------------------------------------------------
function QuestionCard({ taskId, question, site }: { taskId: string; question: TaskQuestion; site: boolean }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const openWindow = async () => {
    try {
      await api.post("/api/agent-browser/window", { task: taskId });
      notify("Браузер агента открыт окном: войдите на сайт, закройте окно и ответьте агенту", "ok");
    } catch (e) {
      notifyError(e);
    }
  };
  const answer = async (value: string) => {
    if (!value.trim()) return;
    setBusy(true);
    try {
      const r = await api.post<{ task: TaskCard }>(`/api/background/${taskId}/answer`, {
        text: value,
        question_id: question.id,
      });
      useBackground.getState().upsert(r.task);
      setText("");
      notify("Ответ отправлен агенту", "ok");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  const approval = question.kind === "approval";
  return (
    <section className={cx("card", "bg-question", approval && "approval")}>
      <div className="row">
        <MessageCircleQuestion />
        <strong>{approval ? "Агент просит разрешения" : "Агент спрашивает"}</strong>
        <span className="spacer" />
        <span className="faint tiny">{timeAgo(question.asked)}</span>
      </div>
      <p className="bg-question-text">{question.text}</p>
      {question.detail && <p className="muted small">{question.detail}</p>}
      {question.options.length > 0 && (
        <div className="row wrap">
          {question.options.map((o) => (
            <Button
              key={o}
              size="sm"
              variant={approval && /^разреш/i.test(o) ? "primary" : "soft"}
              disabled={busy}
              onClick={() => void answer(o)}
            >
              {o}
            </Button>
          ))}
        </div>
      )}
      <div className="row" style={{ marginTop: 10 }}>
        <input
          className="input grow"
          value={text}
          placeholder={approval ? "Или напишите, что сделать вместо этого…" : "Ваш ответ…"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void answer(text)}
        />
        <Button icon={<Send />} disabled={busy || !text.trim()} onClick={() => void answer(text)}>
          Ответить
        </Button>
      </div>
      {site && (
        <div className="bg-question-window faint small">
          Сайт не пускает без входа или браузер без окна?
          <Button size="sm" variant="ghost" icon={<AppWindow />} onClick={() => void openWindow()}>
            Открыть сайт окном
          </Button>
        </div>
      )}
    </section>
  );
}

// --- задача целиком ---------------------------------------------------------------------------------------
type DetailTab = "progress" | "runs" | "measures" | "browser" | "sources" | "task";

function fmtDuration(seconds: number): string {
  const m = Math.round(seconds / 60);
  if (m < 1) return "меньше минуты";
  if (m < 60) return `${m} мин`;
  return `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

function fmtMoment(ts: number): string {
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Расписание задачи: сменить, снять или сделать обычную задачу повторяющейся. */
function ScheduleModal({ task, open, onClose }: { task: TaskFull; open: boolean; onClose: () => void }) {
  const [repeat, setRepeat] = useState<TaskRepeat>(task.repeat ?? {});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) setRepeat(task.repeat ?? {});
  }, [open, task.repeat]);
  const save = async () => {
    setBusy(true);
    try {
      const r = await api.put<{ task: TaskCard }>(`/api/background/${task.id}/repeat`, { repeat });
      useBackground.getState().upsert(r.task);
      onClose();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Расписание задачи"
      icon={<CalendarClock />}
      width={520}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            Сохранить
          </Button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        Задача будет запускаться сама: каждый запуск — свежие данные (замер, обновление заметки), итог — в «Запусках».
        «Не повторять» — повторы снимаются, задача остаётся с итогом последнего запуска.
      </p>
      <RepeatPicker value={repeat} onChange={setRepeat} />
    </Modal>
  );
}

function Detail({ id }: { id: string }) {
  const card = useBackground((s) => s.tasks.find((t) => t.id === id));
  const [task, setTask] = useState<TaskFull | null>(null);
  const [tab, setTab] = useState<DetailTab>("progress");
  const [busy, setBusy] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const [browserTabs, setBrowserTabs] = useState<Record<string, string>>({});
  const load = useCallback(async () => {
    try {
      const r = await api.get<{ task: TaskFull }>(`/api/background/${id}`);
      setTask(r.task);
    } catch (e) {
      if ((e as { status?: number }).status === 404) navigate("background");
      else notifyError(e);
    }
  }, [id]);
  useEffect(() => {
    setTask(null);
    void load();
  }, [load]);
  // обновлять по событиям этой задачи (не чаще раза в секунду — так их и шлёт ядро)
  useEffect(() => onEvent<{ task: TaskCard }>("background.updated", (e) => e.data.task.id === id && void load()), [id, load]);
  // вкладки браузера агента: у задачи есть страница — её можно открыть окном (войти на сайт)
  useEffect(() => {
    api
      .get<{ tasks: Record<string, string> }>("/api/agent-browser")
      .then((r) => setBrowserTabs(r.tasks ?? {}))
      .catch(() => undefined);
    return onEvent<{ tasks?: Record<string, string> }>("agent_browser.state", (e) => setBrowserTabs(e.data.tasks ?? {}));
  }, []);

  if (!task || !card) {
    return (
      <div className="bg-detail-empty">
        <Loader2 className="spin" />
      </div>
    );
  }
  const active = ACTIVE.includes(card.status);
  const recurring = Boolean(card.repeat_text);
  const act = async (what: "cancel" | "retry" | "delete" | "pause" | "resume") => {
    setBusy(true);
    try {
      if (what === "delete") {
        await api.del(`/api/background/${id}`);
        navigate("background");
      } else {
        const r = await api.post<{ task: TaskCard }>(`/api/background/${id}/${what}`, {});
        useBackground.getState().upsert(r.task);
      }
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  const progress = task.log.filter((e) => e.kind !== "action");
  const lastRun = task.history[task.history.length - 1];
  const tabs: { value: DetailTab; label: string; icon: ReactNode }[] = [{ value: "progress", label: "Ход работы", icon: <ListChecks /> }];
  if (recurring || task.history.length) tabs.push({ value: "runs", label: `Запуски${task.history.length ? ` · ${task.history.length}` : ""}`, icon: <History /> });
  if (card.tables.length) tabs.push({ value: "measures", label: "Замеры", icon: <ChartLine /> });
  tabs.push(
    { value: "browser", label: "Браузер", icon: <Globe /> },
    { value: "sources", label: `Источники${task.sources.length ? ` · ${task.sources.length}` : ""}`, icon: <BookOpen /> },
    { value: "task", label: "Задача", icon: <Telescope /> },
  );
  const shown = tabs.some((t) => t.value === tab) ? tab : "progress";
  return (
    <div className="bg-detail">
      <div className="bg-detail-head">
        <div className="grow">
          <h2 className="bg-title">{task.title}</h2>
          <div className="row wrap small">
            <StatusChip status={card.status} />
            {recurring && (
              <span className="chip" title="Расписание">
                <Repeat /> {card.repeat_text}
              </span>
            )}
            {card.status === "scheduled" && card.next_run > 0 && (
              <span className="chip">
                <CalendarClock /> следующий — {fmtWhen(card.next_run)}
              </span>
            )}
            {task.plan.length > 0 && (
              <span className="chip">
                <ListChecks /> {task.plan.filter((s) => s.done).length} из {task.plan.length}
              </span>
            )}
            <span className="faint tiny">
              {task.steps} действий · {fmtDuration(task.worked_s)}
              {recurring && task.cycle ? ` · запусков: ${task.cycle}` : task.runs > 1 ? ` · заходов: ${task.runs}` : ""}
            </span>
          </div>
        </div>
        <div className="row">
          {task.note_link && (
            <Button size="sm" variant="soft" icon={<BookOpen />} onClick={() => openLink(task.note_link)}>
              Заметка
            </Button>
          )}
          {active ? (
            <Button
              size="sm"
              variant="ghost"
              icon={<Ban />}
              loading={busy}
              onClick={() => void act("cancel")}
              title={recurring ? "Остановить этот запуск и повторы (продолжить можно потом)" : undefined}
            >
              {recurring ? "Остановить" : "Отменить"}
            </Button>
          ) : recurring ? (
            <>
              <Button size="sm" variant="soft" icon={<Play />} loading={busy} onClick={() => void act("retry")}>
                Запустить сейчас
              </Button>
              {card.status === "scheduled" ? (
                <Button size="sm" variant="ghost" icon={<Pause />} onClick={() => void act("pause")}>
                  Пауза
                </Button>
              ) : (
                <Button size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => void act("resume")}>
                  Продолжить
                </Button>
              )}
              <IconButton icon={<CalendarClock />} label="Расписание" size="sm" variant="ghost" onClick={() => setScheduling(true)} />
              {card.status !== "cancelled" && (
                <IconButton icon={<Ban />} label="Остановить повторы" size="sm" variant="ghost" onClick={() => void act("cancel")} />
              )}
            </>
          ) : (
            <>
              <Button size="sm" variant="ghost" icon={<RotateCcw />} loading={busy} onClick={() => void act("retry")}>
                Ещё раз
              </Button>
              <IconButton icon={<Repeat />} label="Повторять по расписанию" size="sm" variant="ghost" onClick={() => setScheduling(true)} />
            </>
          )}
          {!active && (
            <IconButton icon={<Trash2 />} label="Удалить из списка (заметки останутся)" size="sm" variant="ghost" onClick={() => void act("delete")} />
          )}
        </div>
      </div>

      {card.question && <QuestionCard taskId={id} question={card.question} site={Boolean(browserTabs[id])} />}

      {(task.summary || task.error) && !active && (
        <section className={cx("card", "bg-result", recurring && lastRun ? lastRun.status : card.status)}>
          <div className="row">
            {(recurring && lastRun ? lastRun.status : card.status) === "done" ? <Check /> : <CircleAlert />}
            <strong>{recurring && lastRun ? `Последний запуск · ${fmtMoment(lastRun.ts)}` : "Итог"}</strong>
            <span className="spacer" />
            {task.result_link && (
              <Button size="sm" variant="primary" icon={<ExternalLink />} onClick={() => openLink(task.result_link)}>
                Открыть результат
              </Button>
            )}
          </div>
          <p>{task.summary || task.error}</p>
        </section>
      )}

      <Tabs<DetailTab> value={shown} onChange={setTab} items={tabs} />
      <div className="bg-tab">
        {shown === "progress" && (
          <div className="bg-progress-view">
            {task.plan.length > 0 && (
              <div className="bg-plan">
                {task.plan.map((s, i) => (
                  <div key={i} className={cx("bg-plan-step", s.done && "done")}>
                    {s.done ? <CircleCheck /> : <CircleDashed />}
                    <span>{s.text}</span>
                  </div>
                ))}
              </div>
            )}
            {progress.length ? (
              <ol className="bg-log">
                {[...progress].reverse().map((e, i) => (
                  <li key={i} className={e.kind}>
                    <span className="faint tiny">
                      {new Date(e.ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
                    </span>
                    <span>{plain(e.text)}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <Empty icon={<Clock />}>
                {card.status === "queued" ? "Задача ждёт очереди" : card.status === "scheduled" ? "Ждёт первого запуска" : "Агент только начинает"}
              </Empty>
            )}
          </div>
        )}
        {shown === "runs" &&
          (task.history.length ? (
            <ol className="bg-runs">
              {[...task.history].reverse().map((run) => (
                <li key={`${run.cycle}-${run.ts}`} className={run.status}>
                  <span className={cx("bg-row-ico", STATUS[run.status]?.tone ?? "muted")}>{STATUS[run.status]?.icon ?? <Clock />}</span>
                  <span className="faint tiny">
                    №{run.cycle} · {fmtMoment(run.ts)}
                  </span>
                  <span>{plain(run.summary) || STATUS[run.status]?.label}</span>
                </li>
              ))}
            </ol>
          ) : (
            <Empty icon={<History />}>
              {card.status === "scheduled" && card.next_run ? `Первый запуск — ${fmtWhen(card.next_run)}` : "Запусков пока не было"}
            </Empty>
          ))}
        {shown === "measures" && <MeasureView taskId={id} version={`${card.tables.length}:${card.last_run?.ts ?? 0}:${card.cycle}`} />}
        {shown === "browser" && <AgentBrowserView task={id} />}
        {shown === "sources" &&
          (task.sources.length ? (
            <ul className="bg-sources">
              {task.sources.map((s) => (
                <li key={s.url}>
                  <button type="button" onClick={() => openLink(s.url)} title={s.url}>
                    <Globe />
                    <span className="ellipsis">{s.title}</span>
                    <span className="faint tiny ellipsis">{s.url.replace(/^https?:\/\//, "")}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty icon={<BookOpen />}>Агент ещё не читал источники</Empty>
          ))}
        {shown === "task" && (
          <div className="bg-brief">
            <p>{task.brief}</p>
            {task.deliverable && (
              <p className="muted">
                <strong>Нужен результат:</strong> {task.deliverable}
              </p>
            )}
            <p className="faint tiny">
              Модель: {task.model || task.provider} · токены: {(task.usage.prompt_tokens ?? 0) + (task.usage.completion_tokens ?? 0)}
              {task.usage.cost ? ` · $${task.usage.cost.toFixed(3)}` : ""}
              {task.note ? ` · заметка: ${task.note}` : ""}
            </p>
          </div>
        )}
      </div>
      <ScheduleModal task={task} open={scheduling} onClose={() => setScheduling(false)} />
    </div>
  );
}

// --- страница ------------------------------------------------------------------------------------------------
export default function BackgroundPage({ route }: { route: string[] }) {
  const tasks = useBackground((s) => s.tasks);
  const loaded = useBackground((s) => s.loaded);
  const missing = useBackground((s) => s.missing);
  const aiConfigured = useApp((s) => s.aiConfigured);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    wireBackground();
    void useBackground.getState().load();
  }, []);
  const selected = route[0] && route[0] !== "browser" ? route[0] : "";
  const browser = route[0] === "browser";

  return (
    <div className="page bg-page">
      <PageHeader
        title="Фоновые задачи"
        actions={
          <>
            <Button variant="soft" icon={<Globe />} onClick={() => navigate("background", "browser")}>
              Браузер агента
            </Button>
            <Button variant="primary" icon={<Plus />} onClick={() => setCreating(true)} disabled={missing}>
              Новая задача
            </Button>
            <IconButton icon={<Settings />} label="Настройки фоновых задач" variant="ghost" onClick={() => navigate("settings", "background")} />
          </>
        }
      />
      <div className="page-body fill bg-layout">
        <aside className="bg-list">
          {!aiConfigured && (
            <div className="bg-hint small">
              Подключите Jarvis AI (Настройки → Jarvis AI) — без модели фоновые задачи не запустятся.
            </div>
          )}
          {tasks.length ? (
            tasks.map((t) => <TaskRow key={t.id} task={t} active={t.id === selected} />)
          ) : loaded ? (
            <Empty icon={<Telescope />}>
              Задач пока нет. Нажмите «Новая задача» или скажите: «Джарвис, в фоне собери…»
            </Empty>
          ) : (
            <div className="bg-detail-empty">
              <Loader2 className="spin" />
            </div>
          )}
        </aside>
        <section className="bg-main">
          {browser ? (
            <div className="bg-browser-page">
              <AgentBrowserView onClose={() => navigate("background")} />
            </div>
          ) : selected ? (
            <Detail key={selected} id={selected} />
          ) : (
            <div className="bg-intro">
              <Telescope className="bg-intro-ico" />
              <h2>Агент работает, пока вы заняты своим</h2>
              <p className="muted">
                Дайте большую задачу — найти, сравнить, собрать данные, следить за чем-то. Агент сам ищет, читает
                сайты, работает в своём браузере (его видно на вкладке «Браузер» у задачи) и записывает итог в ваши
                заметки — в папку «Jarvis» хранилища Obsidian. Экран, мышь и клавиатура остаются вашими.
              </p>
              <div className="row wrap" style={{ justifyContent: "center" }}>
                <Button variant="primary" icon={<Plus />} onClick={() => setCreating(true)} disabled={missing}>
                  Новая задача
                </Button>
                <Button variant="soft" icon={<Globe />} onClick={() => navigate("background", "browser")}>
                  Войти на сайты для агента
                </Button>
              </div>
              <p className="faint small">Голосом или в чате: «в фоне: собери главные новости про ИИ за неделю»</p>
            </div>
          )}
        </section>
      </div>
      <NewTask open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}
