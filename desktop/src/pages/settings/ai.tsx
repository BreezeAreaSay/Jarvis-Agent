// Подключение Jarvis AI: карточки провайдеров, адрес, ключ, модель и проверка связи.
import { Check, Download, ExternalLink, LogIn, LogOut, Server, Smartphone, Zap } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { siLmstudio } from "simple-icons";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { AiProvider, ProviderPreset } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, cx, notify, notifyError, Setting } from "@/ui";
import { Combo, CopyButton, LazyText, Note, ProgressBar, SecretField, useApi, useSettingsPatch } from "./kit";

/** Провайдер: контракт + поля ядра сверх него (локальный, где взять ключ, вход в аккаунт, скачиваемый компонент). */
export type Provider = ProviderPreset & { local?: boolean; key_url?: string; login?: boolean; component?: boolean };

/** Подписка ChatGPT через Codex: без ключа API, вход в аккаунт. Рекомендуемый вариант — всегда первый в списке. */
const CHATGPT: Provider = {
  id: "chatgpt" as AiProvider,
  title: "ChatGPT (подписка)",
  base_url: "",
  needs_key: false,
  default_model: "",
  models: [],
  login: true,
  component: true,
};

export const FALLBACK_PROVIDERS: Provider[] = [
  CHATGPT,
  { id: "openai", title: "OpenAI", base_url: "https://api.openai.com/v1", needs_key: true, default_model: "gpt-5.4-mini", models: ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano"], key_url: "https://platform.openai.com/api-keys" },
  { id: "openrouter", title: "OpenRouter", base_url: "https://openrouter.ai/api/v1", needs_key: true, default_model: "openai/gpt-5.4-mini", models: ["openai/gpt-5.4-mini", "openai/gpt-5.4", "anthropic/claude-sonnet-5.5", "google/gemini-2.5-flash", "deepseek/deepseek-chat"], key_url: "https://openrouter.ai/keys" },
  { id: "deepseek", title: "DeepSeek", base_url: "https://api.deepseek.com/v1", needs_key: true, default_model: "deepseek-chat", models: ["deepseek-chat", "deepseek-reasoner"], key_url: "https://platform.deepseek.com/api_keys" },
  { id: "anthropic", title: "Anthropic (Claude)", base_url: "https://api.anthropic.com/v1/", needs_key: true, default_model: "claude-opus-5-5", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], key_url: "https://console.anthropic.com/settings/keys" },
  { id: "groq", title: "Groq", base_url: "https://api.groq.com/openai/v1", needs_key: true, default_model: "openai/gpt-oss-120b", models: ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"], key_url: "https://console.groq.com/keys" },
  { id: "ollama", title: "Ollama (на этом ПК)", base_url: "http://127.0.0.1:11434/v1", needs_key: false, default_model: "qwen3:8b", models: ["qwen3:8b", "qwen3:14b", "gemma3:12b", "llama3.1:8b"], local: true },
  { id: "lmstudio", title: "LM Studio (на этом ПК)", base_url: "http://127.0.0.1:1234/v1", needs_key: false, default_model: "qwen/qwen3-8b", models: ["qwen/qwen3-8b", "google/gemma-3-12b"], local: true },
  { id: "custom", title: "Свой сервер (OpenAI-совместимый)", base_url: "", needs_key: false, default_model: "", models: [] },
];

const SUBTITLES: Record<string, string> = {
  chatgpt: "по подписке, без ключа",
  openai: "GPT, нужен ключ API",
  openrouter: "сотни моделей, один ключ",
  deepseek: "умно и недорого",
  anthropic: "Claude, нужен ключ API",
  groq: "очень быстрые ответы",
  ollama: "на этом ПК, бесплатно",
  lmstudio: "на этом ПК, бесплатно",
  custom: "любой совместимый API",
};

const SHORT_TITLES: Record<string, string> = {
  chatgpt: "ChatGPT",
  anthropic: "Anthropic",
  ollama: "Ollama",
  lmstudio: "LM Studio",
  custom: "Свой сервер",
};

export function providerTitle(p: Provider, short = false): string {
  return (short && SHORT_TITLES[p.id]) || p.title;
}

/** Значок провайдера: бренды — из AppIcon, остальное — свои плитки. */
export function ProviderIcon({ id, size = 36 }: { id: string; size?: number }) {
  const r = Math.round(size * 0.28);
  const tile = (bg: string, child: ReactNode) => (
    <span className="brand-tile" style={{ width: size, height: size, borderRadius: r, background: bg }}>
      {child}
    </span>
  );
  if (id === "groq") return tile("#F55036", <b style={{ fontSize: size * 0.42 }}>g</b>);
  if (id === "lmstudio")
    return tile(
      "#4F46E5",
      <svg viewBox="0 0 24 24" width={size * 0.56} height={size * 0.56} aria-hidden>
        <path d={siLmstudio.path} fill="#fff" />
      </svg>,
    );
  if (id === "custom") return tile("rgba(255,255,255,0.06)", <Server style={{ width: size * 0.5, height: size * 0.5, color: "var(--accent-text)" }} />);
  return <AppIcon name={id === "chatgpt" ? "openai" : id} size={size} radius={r} />;
}

/** Перечитать «ИИ настроен» из /api/status (ядро не присылает событие ai.configured). */
export function refreshAiConfigured(): void {
  api
    .get<{ ai?: { configured?: boolean } }>("/api/status")
    .then((r) => useApp.setState({ aiConfigured: !!r.ai?.configured }))
    .catch(() => undefined);
}

/** «ИИ настроен» (ключ/адрес/вход на месте). Ядро не присылает ai.configured — перечитываем /api/status сами. */
export function useAiConfigured(): boolean {
  const configured = useApp((s) => s.aiConfigured);
  const key = useApp((s) =>
    [s.secrets.ai_api_key, s.settings?.ai.provider, s.settings?.ai.base_url, s.settings?.ai.enabled, s.settings?.ai.model].join("|"),
  );
  useEffect(() => {
    const t = setTimeout(refreshAiConfigured, 400);
    return () => clearTimeout(t);
  }, [key]);
  useEffect(() => onEvent("ai.chatgpt", () => setTimeout(refreshAiConfigured, 400)), []);
  return configured;
}

/** Провайдеры: GET /api/ai/providers или запасной список; ChatGPT (подписка) — первым. */
export function useProviders() {
  const q = useApi<{ items: Provider[] }>("/api/ai/providers");
  const list = q.data?.items?.length ? q.data.items : FALLBACK_PROVIDERS;
  const chatgpt = list.find((p) => p.id === "chatgpt") ?? CHATGPT;
  const items = [chatgpt, ...list.filter((p) => p.id !== "chatgpt")];
  return { ...q, items };
}

/** Выбор провайдера: модель — по умолчанию нового провайдера, адрес — его же. */
export function useChooseProvider() {
  const [settings, patch] = useSettingsPatch();
  return (p: Provider) => {
    if (!settings || settings.ai.provider === p.id) return;
    const keepModel = p.models.includes(settings.ai.model) || !p.default_model;
    patch({ ai: { provider: p.id as AiProvider, model: keepModel ? settings.ai.model : p.default_model, base_url: "" } });
  };
}

export function ProviderCards({ compact }: { compact?: boolean }) {
  const [settings] = useSettingsPatch();
  const { items } = useProviders();
  const choose = useChooseProvider();
  if (!settings) return null;
  return (
    <div className={cx("provider-grid", compact && "compact")} role="radiogroup" aria-label="Провайдер ИИ">
      {items.map((p) => {
        const selected = settings.ai.provider === p.id;
        return (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={selected}
            className={cx("radio-card provider-card", selected && "selected")}
            onClick={() => choose(p)}
          >
            <ProviderIcon id={p.id} size={compact ? 34 : 40} />
            <span className="grow" style={{ minWidth: 0 }}>
              <div className="title ellipsis">
                {providerTitle(p, true)}
                {p.id === "chatgpt" && !compact && <span className="chip accent tiny-chip rec-chip">рекомендуем</span>}
              </div>
              <div className={cx("sub ellipsis", p.id === "chatgpt" && compact && "accent-text")}>
                {p.id === "chatgpt" && compact ? "рекомендуем" : (SUBTITLES[p.id] ?? (p.needs_key ? "нужен ключ API" : "без ключа"))}
              </div>
            </span>
            <span className="radio-dot" />
          </button>
        );
      })}
    </div>
  );
}

/** Ссылки входа: браузер (auth_url; opened — ядро его уже открыло) или вход с другого устройства по коду. */
interface ChatGptLogin {
  auth_url?: string;
  verification_url?: string;
  user_code?: string;
  opened?: boolean;
}

interface ChatGptState extends ChatGptLogin {
  installed: boolean;
  installing: boolean;
  logged_in: boolean;
  account: string;
  error: string;
  progress?: number;
  login_pending?: boolean;
}

interface ChatGptEvent extends ChatGptLogin {
  state: "installing" | "installed" | "login" | "device" | "logged_in" | "logged_out" | "error" | string;
  error?: string;
  progress?: number;
}

/** Подписка ChatGPT: компонент (≈150 МБ) → вход в аккаунт через браузер → «Вы вошли». */
export function ChatGptPanel({ compact }: { compact?: boolean }) {
  const q = useApi<ChatGptState>("/api/ai/chatgpt");
  const { data, setData, reload } = q;
  const [busy, setBusy] = useState<"" | "install" | "login" | "device" | "logout">("");
  const [waiting, setWaiting] = useState(false);
  const [login, setLogin] = useState<ChatGptLogin | null>(null);
  const [progress, setProgress] = useState<number | null>(null);

  useEffect(
    () =>
      onEvent<ChatGptEvent>("ai.chatgpt", (e) => {
        const d = e.data;
        if (typeof d.progress === "number") setProgress(d.progress);
        setData((prev) =>
          prev
            ? {
                ...prev,
                installing: d.state === "installing",
                installed: d.state === "installed" ? true : prev.installed,
                error: d.state === "error" ? d.error || "Не получилось" : "",
              }
            : prev,
        );
        if (d.state === "error" && d.error) notify(d.error, "error");
        if (d.state === "login" || d.state === "device") {
          // вход начат (здесь или в мастере первой настройки) — показать ссылку или код
          setLogin({ auth_url: d.auth_url, verification_url: d.verification_url, user_code: d.user_code, opened: d.opened });
          setWaiting(true);
        }
        if (d.state === "logged_in" || d.state === "logged_out" || d.state === "error") setLogin(null);
        if (d.state !== "installing") void reload();
      }),
    [setData, reload],
  );

  // ждём входа в браузере: ядро пришлёт ai.chatgpt, но на всякий случай перечитываем состояние
  useEffect(() => {
    if (!waiting) return;
    const poll = setInterval(() => void reload(), 3000);
    const stop = setTimeout(() => setWaiting(false), 180000);
    return () => {
      clearInterval(poll);
      clearTimeout(stop);
    };
  }, [waiting, reload]);
  const loggedIn = !!data?.logged_in;
  useEffect(() => {
    if (loggedIn && waiting) {
      setWaiting(false);
      notify("Вы вошли в ChatGPT — Jarvis AI готов", "ok");
      refreshAiConfigured();
    }
  }, [loggedIn, waiting]);

  const startLogin = async (device: boolean) => {
    setBusy(device ? "device" : "login");
    try {
      const r = await api.post<ChatGptLogin & { logged_in?: boolean }>("/api/ai/chatgpt/login", device ? { device: true } : {});
      if (r.logged_in) {
        await reload();
        refreshAiConfigured();
        return;
      }
      setLogin({ auth_url: r.auth_url, verification_url: r.verification_url, user_code: r.user_code, opened: r.opened });
      setWaiting(true);
    } catch (e) {
      notifyError(e);
      void reload();
    } finally {
      setBusy("");
    }
  };

  const cancelLogin = async () => {
    setWaiting(false);
    setLogin(null);
    try {
      await api.post("/api/ai/chatgpt/login/cancel");
    } catch {
      /* вход уже завершился или не начинался */
    }
  };

  const run = async (kind: "install" | "logout") => {
    setBusy(kind);
    try {
      await api.post(`/api/ai/chatgpt/${kind}`);
      if (kind === "install") {
        setProgress(null);
        setData((prev) => prev && { ...prev, installing: true, error: "" });
      }
      if (kind === "logout") {
        await reload();
        refreshAiConfigured();
      }
    } catch (e) {
      notifyError(e);
      void reload();
    } finally {
      setBusy("");
    }
  };

  // состояние → подсказка и действие (в мастере — одной строкой, в настройках — отдельной плашкой)
  let hint: ReactNode = null;
  let action: ReactNode = null;
  let progressBar: ReactNode = null;
  if (q.missing) {
    hint = "Вход через подписку появится, когда модуль ИИ ядра будет готов";
  } else if (!data) {
    hint = q.error ? `Не удалось узнать состояние: ${q.error.message}` : "Проверяю…";
    if (!q.error) action = <span className="spinner" />;
  } else if (data.installing) {
    hint = "Скачиваю компонент ChatGPT…";
    action = progress !== null ? <span className="faint tiny">{Math.round(progress * 100)}%</span> : <span className="spinner" />;
    progressBar = <ProgressBar value={progress ?? 0.35} className={progress === null ? "indeterminate" : undefined} />;
  } else if (!data.installed) {
    hint = "Нужен компонент ChatGPT (Codex) — скачивается один раз";
    action = (
      <Button variant="primary" size={compact ? "sm" : undefined} icon={<Download />} loading={busy === "install"} onClick={() => run("install")}>
        {compact ? "Скачать (~150 МБ)" : "Скачать компонент ChatGPT (~150 МБ)"}
      </Button>
    );
  } else if (!data.logged_in) {
    const device = !!login?.user_code;
    if (!waiting) hint = "Откроется браузер, войдите в свой аккаунт ChatGPT";
    else if (device)
      hint = (
        <span className="gpt-device">
          На телефоне или другом компьютере откройте{" "}
          <a href={login?.verification_url} onClick={(e) => (e.preventDefault(), bridge.openExternal(login?.verification_url ?? ""))}>
            {login?.verification_url}
          </a>{" "}
          и введите код <b className="mono gpt-code">{login?.user_code}</b>
          <CopyButton text={login?.user_code ?? ""} label="Скопировать код" />
        </span>
      );
    else if (login && login.opened === false && login.auth_url)
      hint = (
        <span className="gpt-device">
          Браузер не открылся сам —{" "}
          <a href={login.auth_url} onClick={(e) => (e.preventDefault(), bridge.openExternal(login.auth_url ?? ""))}>
            откройте страницу входа
          </a>
          <CopyButton text={login.auth_url} label="Скопировать ссылку" />
        </span>
      );
    else hint = "Жду входа в браузере… Вернитесь сюда, когда войдёте";
    action = waiting ? (
      <>
        <span className="spinner" />
        <Button size="sm" variant="ghost" onClick={cancelLogin}>
          Отмена
        </Button>
      </>
    ) : (
      <>
        {!compact && (
          <Button size="sm" variant="ghost" icon={<Smartphone />} loading={busy === "device"} onClick={() => startLogin(true)}>
            С другого устройства
          </Button>
        )}
        <Button variant="primary" size={compact ? "sm" : undefined} icon={<LogIn />} loading={busy === "login"} onClick={() => startLogin(false)}>
          Войти через ChatGPT
        </Button>
      </>
    );
  } else {
    hint = (
      <span className="chip ok">
        <Check /> Вы вошли{data.account ? `: ${data.account}` : ""}
      </span>
    );
    action = (
      <Button size="sm" variant="ghost" icon={<LogOut />} loading={busy === "logout"} onClick={() => run("logout")}>
        Выйти
      </Button>
    );
  }
  const status = data && !q.missing && (
    <span className={cx("chip", data.logged_in ? "ok" : data.error ? "err" : "")}>
      <span className={cx("status-dot", data.logged_in ? "ok" : data.installing || waiting ? "busy" : data.error ? "err" : "")} />
      {data.logged_in ? "Готово" : data.installing ? "Скачиваю" : !data.installed ? "Нет компонента" : waiting ? "Жду входа" : "Не выполнен вход"}
    </span>
  );
  const error = data?.error && !data.installing && (
    <div className="test-err" style={{ textAlign: "left" }}>
      {data.error}
    </div>
  );

  if (compact) {
    return (
      <div className="gpt-panel compact">
        <div className="gpt-head">
          <AppIcon name="openai" size={34} />
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="gpt-title">ChatGPT (подписка)</div>
            <div className="faint tiny">{hint}</div>
          </div>
          {action}
        </div>
        {progressBar}
        {error}
      </div>
    );
  }
  return (
    <div className="gpt-panel">
      <div className="gpt-head">
        <AppIcon name="openai" size={40} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="gpt-title">ChatGPT (подписка)</div>
          <div className="faint tiny">Jarvis AI работает через вашу подписку ChatGPT — без ключа API и оплаты за токены</div>
        </div>
        {status}
      </div>
      {q.missing ? (
        <Note>{hint}</Note>
      ) : (
        <div className="gpt-row">
          <div className="grow col" style={{ gap: 6, minWidth: 0 }}>
            <span className="small muted">{hint}</span>
            {progressBar}
          </div>
          {action}
        </div>
      )}
      {error}
    </div>
  );
}

interface TestResult {
  ok: boolean;
  error?: string;
  latency_ms?: number;
}

/** «Проверить подключение» → POST /api/ai/test. */
export function TestConnection({ size }: { size?: "sm" }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);
  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      setResult(await api.post<TestResult>("/api/ai/test"));
    } catch (e) {
      const missing = e instanceof ApiError && e.status === 404;
      setResult({ ok: false, error: missing ? "Проверка появится, когда модуль ИИ будет готов" : (e as Error).message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="test-conn">
      <div className="row" style={{ gap: 10, justifyContent: "flex-end" }}>
        {result?.ok && <span className="chip ok">Работает{result.latency_ms ? ` · ${result.latency_ms} мс` : ""}</span>}
        {result && !result.ok && <span className="chip err">Нет связи</span>}
        <Button size={size} variant="soft" icon={<Zap />} loading={busy} onClick={run}>
          Проверить подключение
        </Button>
      </div>
      {result && !result.ok && result.error && <div className="test-err">{result.error}</div>}
    </div>
  );
}

/** Строки подключения: адрес (свой/локальный), ключ, модель, проверка. */
export function AiConnection({ compact }: { compact?: boolean }) {
  const [settings, patch] = useSettingsPatch();
  const hasKey = useApp((s) => !!s.secrets.ai_api_key);
  const { items } = useProviders();
  const ai = settings?.ai;
  const preset = items.find((p) => p.id === ai?.provider) ?? FALLBACK_PROVIDERS.find((p) => p.id === ai?.provider);
  const models = useApi<{ items: string[] }>(
    ai ? `/api/ai/models?p=${encodeURIComponent(ai.provider)}&k=${hasKey ? 1 : 0}&u=${encodeURIComponent(ai.base_url)}` : null,
  );
  if (!settings || !ai) return null;
  const local = preset?.local || ai.provider === "custom";
  const needsKey = preset?.needs_key ?? true;
  const modelList = [...new Set([...(models.data?.items ?? []), ...(preset?.models ?? [])])];
  const keyUrl = preset?.key_url;

  return (
    <>
      {local && (
        <Setting
          name="Адрес сервера"
          desc={ai.provider === "custom" ? "OpenAI-совместимый адрес, например http://127.0.0.1:8000/v1" : `Пусто — ${preset?.base_url}`}
          wide
        >
          <LazyText
            value={ai.base_url}
            placeholder={preset?.base_url || "http://…/v1"}
            width="100%"
            onCommit={(v) => patch({ ai: { base_url: v } })}
          />
        </Setting>
      )}
      {(needsKey || ai.provider === "custom") && (
        <Setting
          name="Ключ API"
          desc={
            <>
              {needsKey ? "Хранится зашифрованным только на этом ПК" : "Если сервер его требует"}
              {keyUrl && !compact && (
                <>
                  {" · "}
                  <button type="button" className="link-btn" onClick={() => bridge.openExternal(keyUrl)}>
                    где взять ключ <ExternalLink />
                  </button>
                </>
              )}
            </>
          }
          wide
        >
          <SecretField name="ai_api_key" placeholder={ai.provider === "anthropic" ? "sk-ant-…" : ai.provider === "openai" ? "sk-…" : "Вставьте ключ"} />
        </Setting>
      )}
      <Setting name="Модель" desc={compact ? undefined : "Из списка провайдера или своя"}>
        <Combo
          value={ai.model}
          width={compact ? 240 : 260}
          placeholder={preset?.default_model || "имя модели"}
          options={modelList.map((m) => ({ value: m, hint: m === preset?.default_model ? "по умолчанию" : undefined }))}
          onCommit={(v) => patch({ ai: { model: v || preset?.default_model || "" } })}
        />
      </Setting>
      <Setting name="Проверка" desc={compact ? undefined : "Короткий запрос к модели: ключ, адрес и прокси"}>
        <TestConnection size="sm" />
      </Setting>
    </>
  );
}
