// Правая часть: шапка чата с живой сферой («Погода и дела · GPT-5.4 Mini • Всё вместе»), лента сообщений с
// автопрокруткой, приветствие и подсказки в пустом чате, карточка «Подключите ИИ», агент над полем ввода и
// само поле. Справа в широком окне — большая сфера Jarvis: слушает, думает, отвечает.
import { AppWindow, ArrowDown, Brain, CloudSun, Cpu, MessageSquareDashed, Palette, Plug, Sparkles, Table2 } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { JarvisOrb, type OrbState } from "@/components/JarvisOrb";
import { onEvent } from "@/api/events";
import { levels } from "@/lib/levels";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { Button, cx, notifyError } from "@/ui";
import { AgentPanel } from "./AgentPanel";
import { ImageViewer } from "./Images";
import { Composer, type ComposerHandle } from "./Composer";
import { AssistantMessage, ErrorBubble, PendingMessage, UserMessage } from "./Message";
import { useAi } from "./store";
import { DRAFT, isTerminal, MODE_LABEL, type Mode, type Thread } from "./types";
import { fmtTokens, fmtUsd, prettyModel } from "./util";

const SUGGESTIONS = [
  { text: "Какая погода завтра?", icon: <CloudSun /> },
  { text: "Сверни все окна и включи музыку", icon: <AppWindow /> },
  { text: "Сравни Петра I и Екатерину II таблицей", icon: <Table2 /> },
  { text: "Нарисуй неоновый город будущего", icon: <Palette /> },
  { text: "Запомни, что я не ем мясо", icon: <Brain /> },
  { text: "Как загружен компьютер?", icon: <Cpu /> },
];

const ORB_TEXT: Record<OrbState, string> = {
  idle: "На связи",
  listening: "Слушаю…",
  thinking: "Думаю…",
  speaking: "Отвечаю…",
};

/** Состояние сферы: голос Jarvis (слушает, говорит) важнее; иначе — чат, который сейчас отвечает. */
function useOrbState(chat: OrbState | undefined): OrbState {
  const voice = useApp((s) => s.voiceState);
  const [speaking, setSpeaking] = useState(levels.speaking);
  useEffect(() => onEvent<{ speaking: boolean }>("tts.state", (e) => setSpeaking(!!e.data.speaking)), []);
  if (speaking || voice === "speaking") return "speaking";
  if (voice === "listening") return "listening";
  return chat ?? (voice === "processing" ? "thinking" : "idle");
}

function Greeting({ onPick }: { onPick: (text: string) => void }) {
  const profile = useApp((s) => s.settings?.profile);
  const who = profile?.address?.trim() || profile?.name?.trim();
  return (
    <div className="ai-hello fade-in">
      <div className="ai-hello-orb" aria-hidden>
        <JarvisOrb scale={0.3} />
      </div>
      <h2 className="ai-hello-title">Чем могу помочь{who ? `, ${who}` : ""}?</h2>
      <p className="ai-hello-sub">Спросите что угодно, попросите выполнить команду на компьютере или поручите задачу агенту.</p>
      <div className="ai-suggest">
        {SUGGESTIONS.map((s) => (
          <button key={s.text} type="button" className="ai-suggest-chip" onClick={() => onPick(s.text)}>
            {s.icon}
            {s.text}
          </button>
        ))}
      </div>
    </div>
  );
}

/** Ответ чата: пока пишется текст — «отвечает», до первого слова — «думает». */
export function chatOrbState(t: Thread | undefined): OrbState | undefined {
  if (!t?.pending && !t?.sending) return undefined;
  return t.pending?.text ? "speaking" : "thinking";
}

/** Большая сфера справа (только в широком окне): состояние словами и подсказка, как позвать голосом. */
export function OrbRail({ chatKey }: { chatKey: string }) {
  const thread = useAi((s) => s.threads[chatKey]);
  const state = useOrbState(chatOrbState(thread));
  const wake = useApp((s) => s.settings?.profile.assistant_name?.trim() || "Джарвис");
  const voiceOn = useApp((s) => s.voiceState !== "off" && s.voiceState !== "error");
  return (
    <aside className={cx("ai-rail", `is-${state}`)} aria-label="Jarvis">
      <div className="ai-rail-orb">
        <JarvisOrb state={state} scale={0.335} />
      </div>
      <div className="ai-rail-name">JARVIS</div>
      <div className="ai-rail-state" role="status">
        <span className="ai-rail-dot" />
        {ORB_TEXT[state]}
      </div>
      <p className="ai-rail-hint">
        {voiceOn ? `Скажите «${wake}» — отвечу голосом` : "Пишите — отвечу здесь; голос включается в настройках"}
      </p>
    </aside>
  );
}

function ConnectCard() {
  return (
    <div className="ai-connect fade-in">
      <span className="ai-connect-icon">
        <Plug />
      </span>
      <h2>Подключите ИИ</h2>
      <p>
        Jarvis AI отвечает на вопросы, ищет в интернете, помнит важное о вас и сам выполняет задачи на компьютере.
        Выберите провайдера и вставьте ключ — это займёт минуту.
      </p>
      <Button variant="primary" size="lg" icon={<Sparkles />} onClick={() => navigate("settings", "ai")}>
        Открыть настройки ИИ
      </Button>
    </div>
  );
}

function UsageLine() {
  const usage = useAi((s) => s.usage);
  const budgetSetting = useApp((s) => s.settings?.ai.monthly_budget ?? 0);
  if (!usage) return null;
  const budget = usage.budget || budgetSetting;
  const cost = usage.month?.cost ?? 0;
  const tokens = (usage.month?.prompt_tokens ?? 0) + (usage.month?.completion_tokens ?? 0);
  const text = budget > 0 ? `${fmtUsd(cost)} из ${fmtUsd(budget)}` : tokens > 0 ? `${fmtTokens(tokens)} за месяц` : "";
  if (!text) return null;
  const over = budget > 0 && cost >= budget;
  return (
    <div className={cx("ai-usage", over && "over")} title="Расходы на ИИ в этом месяце">
      {text}
    </div>
  );
}

/** Лента сообщений: держится у нижнего края, пока человек сам не прокрутил вверх. */
function ThreadView({
  chatKey,
  thread,
  agentActive,
  onRetry,
  onResend,
}: {
  chatKey: string;
  thread: Thread;
  agentActive: boolean;
  onRetry: () => void;
  onResend: (text: string, images: string[]) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [away, setAway] = useState(false);

  useEffect(() => {
    stick.current = true;
    setAway(false);
  }, [chatKey]);

  const toBottom = (smooth = false) => {
    const el = box.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  };

  useLayoutEffect(() => {
    if (stick.current) toBottom();
  });

  useEffect(() => {
    const el = inner.current;
    if (!el) return;
    const ro = new ResizeObserver(() => stick.current && toBottom());
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // своё новое сообщение — всегда показать
  const count = thread.messages.length;
  const last = thread.messages[count - 1];
  useEffect(() => {
    if (last?.role === "user") {
      stick.current = true;
      toBottom(true);
    }
  }, [count, last?.role]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    stick.current = atBottom;
    setAway(!atBottom);
  };

  const lastUserBefore = (index: number) => {
    for (let i = index - 1; i >= 0; i--) if (thread.messages[i].role === "user") return thread.messages[i];
    return null;
  };

  return (
    <>
      <div className="ai-scroll" ref={box} onScroll={onScroll} role="log" aria-busy={!!thread.pending} aria-label="Сообщения">
        <div className="ai-msgs" ref={inner}>
          {thread.messages.map((m, i) =>
            m.role === "user" ? (
              <UserMessage key={m.id} m={m} />
            ) : (
              <AssistantMessage
                key={m.id}
                m={m}
                onRetry={
                  m.meta?.error && !m.local && i === thread.messages.length - 1 && lastUserBefore(i)
                    ? () => {
                        const u = lastUserBefore(i);
                        if (u) onResend(u.content, u.meta?.images ?? []);
                      }
                    : undefined
                }
              />
            ),
          )}
          {thread.pending && <PendingMessage p={thread.pending} agentActive={agentActive} />}
          {thread.failure && (
            <div className="ai-msg assistant">
              <span className="ai-avatar err" aria-hidden>
                <Sparkles />
              </span>
              <div className="ai-msg-body">
                <ErrorBubble error={thread.failure.error} onRetry={thread.failure.retryText ? onRetry : undefined} />
              </div>
            </div>
          )}
        </div>
      </div>
      {away && (
        <button type="button" className="ai-down pop-in" onClick={() => toBottom(true)} aria-label="К последнему сообщению">
          <ArrowDown />
        </button>
      )}
    </>
  );
}

export function ChatView({ chatKey }: { chatKey: string }) {
  const chat = useAi((s) => (chatKey === DRAFT ? null : (s.chats.find((c) => c.id === chatKey) ?? null)));
  const thread = useAi((s) => s.threads[chatKey]);
  const agent = useAi((s) => s.agent);
  const draftMode = useAi((s) => s.draftMode);
  const loadThread = useAi((s) => s.loadThread);
  const send = useAi((s) => s.send);
  const stop = useAi((s) => s.stop);
  const retry = useAi((s) => s.retry);
  const setMode = useAi((s) => s.setMode);
  const setChatModel = useAi((s) => s.setChatModel);
  const settingsAi = useApp((s) => s.settings?.ai);
  const patchSettings = useApp((s) => s.patchSettings);
  const configured = useApp((s) => s.aiConfigured);
  const composer = useRef<ComposerHandle>(null);

  useEffect(() => {
    if (chatKey !== DRAFT) loadThread(chatKey);
  }, [chatKey, loadThread]);

  const mode: Mode = (chatKey === DRAFT ? draftMode : chat?.mode) ?? settingsAi?.mode ?? "all";
  const model = chat?.model || settingsAi?.model || "";
  const t: Thread = thread ?? {
    messages: [],
    loaded: chatKey === DRAFT,
    loading: chatKey !== DRAFT,
    error: "",
    notFound: false,
    pending: null,
    sending: false,
    stopping: false,
    failure: null,
  };
  const agentActive = !!agent && !isTerminal(agent.status);
  // агент работает над задачей из этого чата — сообщение ему: поправка или ответ на его вопрос
  const toAgent = agentActive && !!agent?.chatId && agent.chatId === chatKey;
  const busy = t.sending || !!t.pending;
  const empty = t.loaded && !t.messages.length && !t.pending && !t.failure;

  const title = chat?.title || (chatKey === DRAFT ? "Новый чат" : t.loading ? "" : "Чат");
  const orbState = useOrbState(chatOrbState(thread));
  const headMeta = useMemo(() => `${prettyModel(model)} • ${MODE_LABEL[mode]}`, [model, mode]);

  const onSend = (text: string, images: string[]) => {
    if (!configured) return;
    send(chatKey, text, { mode, images });
  };

  const onModel = async (m: string) => {
    try {
      await patchSettings({ ai: { model: m } });
      if (chat?.model) await setChatModel(chat.id, m);
    } catch (e) {
      notifyError(e);
    }
  };

  let body: React.ReactNode;
  if (!configured) body = <ConnectCard />;
  else if (t.notFound)
    body = (
      <div className="ai-center">
        <div className="empty">
          <MessageSquareDashed />
          <div>Такого чата больше нет</div>
          <Button size="sm" onClick={() => navigate("ai", DRAFT)}>
            Начать новый
          </Button>
        </div>
      </div>
    );
  else if (!t.loaded && t.error)
    body = (
      <div className="ai-center">
        <div className="empty">
          <div>Не удалось открыть чат: {t.error}</div>
          <Button size="sm" onClick={() => loadThread(chatKey)}>
            Повторить
          </Button>
        </div>
      </div>
    );
  else if (!t.loaded)
    body = (
      <div className="ai-center">
        <span className="spinner" style={{ width: 22, height: 22 }} />
      </div>
    );
  else if (empty) body = <Greeting onPick={(text) => onSend(text, [])} />;
  else
    body = (
      <ThreadView
        chatKey={chatKey}
        thread={t}
        agentActive={agentActive}
        onRetry={() => retry(chatKey, mode)}
        onResend={(text, images) => send(chatKey, text, { mode, images })}
      />
    );

  return (
    <section className="ai-main" aria-label="Чат">
      <header className="ai-head">
        <span className="ai-head-orb" aria-hidden>
          <JarvisOrb state={orbState} rings={false} scale={0.44} />
        </span>
        <span className="ai-head-title ellipsis">{title}</span>
        {chat?.kind === "voice" && <span className="chip">Голосом</span>}
        <span className="spacer" />
        {configured && <span className="ai-head-meta ellipsis">{headMeta}</span>}
      </header>
      <div className="ai-thread">
        {body}
        {configured && <UsageLine />}
      </div>
      <AgentPanel />
      <Composer
        ref={composer}
        chatKey={chatKey}
        mode={mode}
        model={model}
        busy={busy}
        disabled={!configured}
        toAgent={toAgent}
        onSend={onSend}
        onStop={() => stop(chatKey)}
        onMode={(m) => {
          setMode(chatKey, m);
          composer.current?.focus();
        }}
        onModel={onModel}
      />
      <ImageViewer />
    </section>
  );
}
