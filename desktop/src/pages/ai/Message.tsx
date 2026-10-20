// Сообщения чата: пузырь человека (акцент), ответ ассистента (аватар, Markdown, «Выполнено ✓», источники,
// «Запомнил: …», ошибка с «Повторить», у удачного дела агента — «Запомнить как сценарий», у любого — «Как он
// это делал» и что он запомнил о программе) и ответ, который пишется прямо сейчас.
import {
  Bot,
  Brain,
  Check,
  CircleAlert,
  CircleCheck,
  CircleX,
  Copy,
  Globe,
  History,
  Lightbulb,
  RotateCcw,
  Sparkles,
  Square,
  Workflow,
  Wrench,
} from "lucide-react";
import { memo, useState } from "react";
import { api } from "@/api/client";
import { bridge } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { Button, cx, Dots, notify, notifyError } from "@/ui";
import { AgentJournal } from "./AgentJournal";
import { Markdown } from "./Markdown";
import { LOOKUP_TOOL } from "./store";
import type { MetaExtra, PendingReply, ThreadMessage, ToolRow } from "./types";
import { fmtTime, hostOf } from "./util";

type Action = { title: string; ok: boolean; detail?: string };

export function AssistantAvatar() {
  return (
    <span className="ai-avatar" aria-hidden>
      <Sparkles />
    </span>
  );
}

// --- «Выполнено ✓ Свернуть все окна» --------------------------------------------------------------------
export function ToolCard({ rows }: { rows: (ToolRow | (Action & { status?: undefined }))[] }) {
  const norm = rows.map((r) => ({
    key: "call_id" in r ? r.call_id : r.title,
    title: r.title,
    detail: r.detail,
    status: "status" in r && r.status ? r.status : (r as Action).ok ? "ok" : "error",
  }));
  const running = norm.some((r) => r.status === "running");
  const failed = norm.filter((r) => r.status === "error").length;
  const title = running
    ? "Выполняю…"
    : failed === 0
      ? "Выполнено"
      : failed === norm.length
        ? "Не получилось"
        : "Выполнено частично";
  return (
    <div className={cx("ai-bubble ai-tools", failed > 0 && !running && "has-error")}>
      <div className="ai-tools-title">{title}</div>
      {norm.map((r) => (
        <div key={r.key} className={cx("ai-tool-row", r.status)}>
          {r.status === "running" ? (
            <span className="spinner" aria-label="Выполняется" />
          ) : r.status === "ok" ? (
            <CircleCheck aria-label="Готово" />
          ) : (
            <CircleX aria-label="Ошибка" />
          )}
          <span className="t">{r.title}</span>
          {r.detail && r.status === "error" && <span className="d">{r.detail}</span>}
        </div>
      ))}
    </div>
  );
}

// --- источники ------------------------------------------------------------------------------------------
function SourcesChip({ count, open, onToggle }: { count: number; open: boolean; onToggle: () => void }) {
  return (
    <button type="button" className={cx("ai-src-chip", open && "open")} onClick={onToggle} aria-expanded={open}>
      <Globe />
      Источники · {count}
    </button>
  );
}

/** «получено сегодня в 14:05» / «получено 18.10 в 14:05» — когда ИИ взял сведения из источника. */
function fetchedText(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const today = new Date().toDateString() === d.toDateString();
  return today ? `получено сегодня в ${time}` : `получено ${d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })} в ${time}`;
}

function SourcesList({ sources }: { sources: { title: string; url: string; fetched_at?: string }[] }) {
  const fetched = fetchedText(sources.find((s) => s.fetched_at)?.fetched_at);
  return (
    <div className="ai-sources pop-in">
      {fetched && <div className="ai-sources-time">Сведения {fetched}</div>}
      {sources.map((s, i) => (
        <a
          key={s.url + i}
          href={s.url}
          className="ai-source"
          onClick={(e) => {
            e.preventDefault();
            bridge.openExternal(s.url);
          }}
          title={s.url}
        >
          <span className="n">{i + 1}</span>
          <span className="grow">
            <span className="ellipsis st">{s.title || hostOf(s.url)}</span>
            <span className="ellipsis sh">{hostOf(s.url)}</span>
          </span>
        </a>
      ))}
    </div>
  );
}

// --- «Запомнил: …» --------------------------------------------------------------------------------------
export function MemoryChip({ text }: { text: string }) {
  return (
    <button
      type="button"
      className="ai-bubble ai-memory pop-in"
      onClick={() => navigate("ai", "notes", "memory")}
      title="Открыть память"
    >
      <span className="mem-icon">
        <Brain />
      </span>
      <span>Запомнил: {text}</span>
    </button>
  );
}

export function ErrorBubble({ error, onRetry, retrying }: { error: string; onRetry?: () => void; retrying?: boolean }) {
  return (
    <div className="ai-bubble ai-error" role="alert">
      <CircleAlert />
      <span className="grow">{error}</span>
      {onRetry && (
        <Button size="sm" icon={<RotateCcw />} onClick={onRetry} loading={retrying}>
          Повторить
        </Button>
      )}
    </div>
  );
}

function StatusLine({ kind, text }: { kind: "thinking" | "searching" | "tool" | "agent"; text: string }) {
  const label =
    (text || "").replace(/[.…]+$/, "") ||
    (kind === "searching"
      ? "Ищу актуальную информацию"
      : kind === "tool"
        ? "Выполняю команды"
        : kind === "agent"
          ? "Агент выполняет задачу"
          : "Думаю");
  return (
    <div className={cx("ai-bubble ai-status", kind)} role="status">
      {kind === "searching" ? <Globe /> : kind === "tool" ? <Wrench /> : kind === "agent" ? <Bot /> : <Sparkles />}
      <span>{label}</span>
      <Dots />
    </div>
  );
}

/** Статус хода агента в чате («Шаг 2 из 4: …», «Составляю план…») — подробности в карточке агента. */
const AGENT_STATUS = /^(Шаг \d+ из \d+|Составляю план|Приступаю|Жду вашего решения)/;

/** Текст ответа с чипом «Источники · N» в конце строки. */
function AnswerBody({
  text,
  sources,
  streaming,
}: {
  text: string;
  sources?: { title: string; url: string; fetched_at?: string }[];
  streaming?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const has = !!sources?.length;
  if (!text.trim() && !has) return null;
  return (
    <>
      <div className={cx("ai-bubble ai-answer", has && "with-chip")}>
        {text.trim() && <Markdown text={text} streaming={streaming} />}
        {has && <SourcesChip count={sources!.length} open={open} onToggle={() => setOpen(!open)} />}
      </div>
      {has && open && <SourcesList sources={sources!} />}
    </>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="ai-msg-tool"
      aria-label="Скопировать ответ"
      title="Скопировать ответ"
      onClick={() =>
        navigator.clipboard
          .writeText(text)
          .then(() => {
            setDone(true);
            setTimeout(() => setDone(false), 1500);
          })
          .catch(() => notify("Не удалось скопировать", "error"))
      }
    >
      {done ? <Check /> : <Copy />}
    </button>
  );
}

// --- сообщения ------------------------------------------------------------------------------------------
export const UserMessage = memo(function UserMessage({ m }: { m: ThreadMessage }) {
  const images = m.meta?.images ?? [];
  return (
    <div className={cx("ai-msg user", m.local && "local")} title={fmtTime(m.created)}>
      <div className="ai-bubble ai-user">
        {images.length > 0 && (
          <div className="ai-user-images">
            {images.map((src, i) => (
              <img key={i} src={src} alt={`Изображение ${i + 1}`} />
            ))}
          </div>
        )}
        {m.content && <div className="ai-user-text">{m.content}</div>}
      </div>
    </div>
  );
});

// --- «Как он это делал»: журнал работы агента — шаги и экран после каждого --------------------------------
function JournalButton({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" icon={<History />} onClick={() => setOpen(true)}>
        Как он это делал
      </Button>
      {open && <AgentJournal runId={runId} onClose={() => setOpen(false)} />}
    </>
  );
}

// --- «Запомнил о программе: …» — агент учтёт в следующий раз ----------------------------------------------
function LessonChip({ app, text }: { app: string; text: string }) {
  return (
    <div className="ai-bubble ai-memory ai-lesson" title="Агент учтёт это в следующий раз (Приватность — очистить)">
      <span className="mem-icon">
        <Lightbulb />
      </span>
      <span>
        Запомнил о «{app}»: {text}
      </span>
    </div>
  );
}

// --- «Запомнить как сценарий»: те же шаги в следующий раз — сразу, без ИИ --------------------------------
function RecipeButton({ m }: { m: ThreadMessage }) {
  const recipe = m.meta?.recipe;
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  if (!recipe) return null;
  const id = saved || recipe.scenario_id;
  if (id)
    return (
      <Button size="sm" variant="ghost" icon={<Check />} onClick={() => navigate("commands", "scenarios", id)}>
        Сохранено сценарием
      </Button>
    );
  const save = async () => {
    setBusy(true);
    try {
      const res = await api.post<{ scenario: { id: string; name: string } }>("/api/agent/scenario", {
        chat_id: m.chat_id,
        message_id: m.id,
      });
      setSaved(res.scenario.id);
      notify(`Сценарий «${res.scenario.name}»: в следующий раз сделаю сразу, без ИИ`, "ok");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button
      size="sm"
      icon={<Workflow />}
      loading={busy}
      title={`${recipe.actions.length} шаг(ов) — повторю по фразе «${recipe.request}»`}
      onClick={save}
    >
      Запомнить как сценарий
    </Button>
  );
}

export const AssistantMessage = memo(function AssistantMessage({
  m,
  onRetry,
}: {
  m: ThreadMessage;
  onRetry?: () => void;
}) {
  const meta = m.meta ?? {};
  const extra = meta as MetaExtra;
  const actions = meta.actions ?? [];
  const empty =
    !m.content?.trim() && !actions.length && !meta.sources?.length && !meta.memory?.length && !meta.error;
  if (empty && !extra.stopped) return null;
  return (
    <div className="ai-msg assistant">
      <AssistantAvatar />
      <div className="ai-msg-body">
        {actions.length > 0 && <ToolCard rows={actions} />}
        <AnswerBody text={m.content ?? ""} sources={meta.sources} />
        {(meta.memory ?? []).map((t) => (
          <MemoryChip key={t} text={t} />
        ))}
        {meta.lesson && <LessonChip app={meta.lesson.app} text={meta.lesson.text} />}
        {meta.error && <ErrorBubble error={meta.error} onRetry={onRetry} />}
        {extra.stopped && !meta.error && (
          <div className="ai-stopped">
            <Square /> Остановлено
          </div>
        )}
        {m.content?.trim() && (
          <div className="ai-msg-tools">
            <span className="faint tiny">{fmtTime(m.created)}</span>
            <CopyButton text={m.content} />
            <RecipeButton m={m} />
            {extra.agent?.journal && <JournalButton runId={extra.agent.run_id} />}
          </div>
        )}
      </div>
    </div>
  );
});

export function PendingMessage({ p, agentActive }: { p: PendingReply; agentActive?: boolean }) {
  const tools = p.tools.filter((t) => !LOOKUP_TOOL.test(t.name));
  const agentTurn = !!agentActive && p.status?.kind === "tool" && AGENT_STATUS.test(p.status.text);
  // пока идут команды, их карточка сама говорит «Выполняю…» — строка статуса не нужна
  const showStatus = agentTurn || (p.status ? !(p.status.kind === "tool" && tools.length > 0) : !p.text && !tools.length);
  return (
    <div className="ai-msg assistant pending" aria-busy>
      <AssistantAvatar />
      <div className="ai-msg-body">
        {tools.length > 0 && <ToolCard rows={tools} />}
        {showStatus &&
          (agentTurn ? (
            <StatusLine kind="agent" text="" />
          ) : (
            <StatusLine kind={p.status?.kind ?? "thinking"} text={p.status?.text ?? ""} />
          ))}
        {p.text && <AnswerBody text={p.text} streaming />}
        {p.memory.map((t) => (
          <MemoryChip key={t} text={t} />
        ))}
      </div>
    </div>
  );
}
