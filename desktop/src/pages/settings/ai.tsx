// Подключение Jarvis AI: выбор провайдера по группам (по подписке, по ключу API, на этом ПК, свой сервер),
// вход в ChatGPT и в программы-провайдеры (Claude Code, Gemini CLI, Qwen Code), регион, ключ, модель и проверка.
import {
  Check,
  ChevronDown,
  Download,
  ExternalLink,
  Info,
  LogIn,
  LogOut,
  RefreshCw,
  Server,
  Smartphone,
  SquareTerminal,
  Zap,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useState } from "react";
import { siBytedance, siClaude, siGooglegemini, siKimi, siLmstudio, siMinimax, siMistralai, siQwen } from "simple-icons";
import { api, ApiError } from "@/api/client";
import { onEvent } from "@/api/events";
import type { AiProvider, CliProviderEvent, CliProviderState, ProviderGroup, ProviderPreset } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, cx, notify, notifyError, Setting, Tabs } from "@/ui";
import { Combo, CopyButton, LazyText, Note, ProgressBar, SecretField, useApi, useSettingsPatch } from "./kit";
import "@/styles/ai-providers.css";

/** Провайдер: контракт ядра (GET /api/ai/providers). */
export type Provider = ProviderPreset;

const account = (id: AiProvider, title: string, models: string[], extra: Partial<Provider> = {}): Provider => ({
  id,
  title,
  base_url: "",
  needs_key: false,
  default_model: models[0] ?? "",
  models,
  login: true,
  group: "account",
  ...extra,
});

const keyed = (id: AiProvider, title: string, base_url: string, models: string[], extra: Partial<Provider> = {}): Provider => ({
  id,
  title,
  base_url,
  needs_key: true,
  default_model: models[0] ?? "",
  models,
  group: "key",
  ...extra,
});

/** Запасной список (ядро ещё не ответило или старое): названия и умолчания — как в core/jarvis/ai/providers.py. */
export const FALLBACK_PROVIDERS: Provider[] = [
  account("chatgpt", "ChatGPT (подписка)", [], { component: true }),
  account("claude_code", "Claude (подписка)", ["sonnet", "opus", "haiku"], { cli: true }),
  account("gemini_cli", "Gemini CLI", ["auto", "pro", "flash", "flash-lite"], { cli: true }),
  account("qwen_code", "Qwen Code", ["qwen3.7-plus", "qwen3.8-max"], { cli: true, country: "cn", default_model: "" }),
  keyed("openai", "OpenAI", "https://api.openai.com/v1", ["gpt-5.4-mini", "gpt-5.4", "gpt-5.4-nano"], {
    key_url: "https://platform.openai.com/api-keys",
  }),
  keyed("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1/", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], {
    key_url: "https://console.anthropic.com/settings/keys",
  }),
  keyed("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", ["gemini-3.5-flash", "gemini-3.1-pro-preview"], {
    key_url: "https://aistudio.google.com/apikey",
  }),
  keyed("xai", "xAI Grok", "https://api.x.ai/v1", ["grok-4.7", "grok-4.6"], { key_url: "https://console.x.ai/" }),
  keyed("mistral", "Mistral", "https://api.mistral.ai/v1", ["mistral-large-latest", "mistral-small-latest"], {
    key_url: "https://console.mistral.ai/api-keys",
  }),
  keyed("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", ["openai/gpt-5.4-mini", "openai/gpt-5.4"], {
    key_url: "https://openrouter.ai/keys",
  }),
  keyed("groq", "Groq", "https://api.groq.com/openai/v1", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"], {
    key_url: "https://console.groq.com/keys",
  }),
  keyed("deepseek", "DeepSeek", "https://api.deepseek.com", ["deepseek-v4-flash", "deepseek-v4-pro"], {
    key_url: "https://platform.deepseek.com/api_keys",
    country: "cn",
  }),
  keyed("qwen", "Qwen (Alibaba Cloud)", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", ["qwen3.7-plus", "qwen3.8-max"], {
    country: "cn",
  }),
  keyed("moonshot", "Kimi (Moonshot)", "https://api.moonshot.ai/v1", ["kimi-k2.6", "kimi-k3"], { country: "cn" }),
  keyed("zhipu", "GLM (Z.ai)", "https://api.z.ai/api/paas/v4", ["glm-5.2", "glm-5-turbo"], { country: "cn" }),
  keyed("minimax", "MiniMax", "https://api.minimax.io/v1", ["MiniMax-M3", "MiniMax-M2.7"], { country: "cn" }),
  keyed("doubao", "Doubao (Volcengine)", "https://ark.cn-beijing.volces.com/api/v3", ["doubao-seed-2-1-pro-260628"], { country: "cn" }),
  keyed("siliconflow", "SiliconFlow", "https://api.siliconflow.com/v1", ["deepseek-ai/DeepSeek-V4-Flash"], { country: "cn" }),
  keyed("yandex", "YandexGPT", "https://llm.api.cloud.yandex.net/v1", ["yandexgpt/latest", "yandexgpt-lite/latest"], {
    key_url: "https://console.yandex.cloud/",
    country: "ru",
    folder: true,
  }),
  {
    id: "ollama",
    title: "Ollama (на этом ПК)",
    base_url: "http://127.0.0.1:11434/v1",
    needs_key: false,
    default_model: "qwen3:8b",
    models: ["qwen3:8b", "qwen3:14b", "gemma3:12b", "llama3.1:8b"],
    local: true,
    group: "local",
  },
  {
    id: "lmstudio",
    title: "LM Studio (на этом ПК)",
    base_url: "http://127.0.0.1:1234/v1",
    needs_key: false,
    default_model: "qwen/qwen3-8b",
    models: ["qwen/qwen3-8b", "google/gemma-3-12b"],
    local: true,
    group: "local",
  },
  { id: "custom", title: "Свой сервер (OpenAI-совместимый)", base_url: "", needs_key: false, default_model: "", models: [], group: "custom" },
];

/** Подписи карточек: по подписке — что нужно, по ключу — чем провайдер хорош. */
const SUBTITLES: Record<string, string> = {
  chatgpt: "Подписка ChatGPT Plus или Pro — вход в аккаунт, без ключа API",
  claude_code: "Подписка Claude Pro или Max через программу Claude Code",
  gemini_cli: "Через программу Gemini CLI: рабочий аккаунт Google или свой ключ",
  qwen_code: "Через программу Qwen Code: тариф Coding Plan или свой ключ",
  openai: "модели GPT",
  anthropic: "модели Claude",
  gemini: "есть бесплатный ключ",
  xai: "модели Grok",
  mistral: "европейский провайдер",
  openrouter: "сотни моделей, один ключ",
  groq: "очень быстрые ответы",
  deepseek: "умно и недорого",
  qwen: "Alibaba Cloud Model Studio",
  moonshot: "модели Kimi",
  zhipu: "модели GLM",
  minimax: "модели MiniMax",
  doubao: "Volcengine и BytePlus",
  siliconflow: "открытые модели",
  yandex: "Yandex AI Studio",
  ollama: "бесплатно, без интернета",
  lmstudio: "бесплатно, без интернета",
  custom: "любой совместимый API",
};

const SHORT_TITLES: Record<string, string> = {
  chatgpt: "ChatGPT",
  claude_code: "Claude",
  gemini_cli: "Gemini",
  anthropic: "Anthropic",
  qwen: "Qwen",
  moonshot: "Kimi",
  doubao: "Doubao",
  ollama: "Ollama",
  lmstudio: "LM Studio",
  custom: "Свой сервер",
};

const COUNTRIES: Record<string, string> = { cn: "Китайская компания", ru: "Российская компания" };

/** Провайдеры без ключа API: вход в аккаунт (ChatGPT) или программа на этом ПК. */
const ACCOUNT_IDS = new Set<string>(["chatgpt", "claude_code", "gemini_cli", "qwen_code"]);

export function isAccountProvider(id: string | undefined): boolean {
  return !!id && ACCOUNT_IDS.has(id);
}

export function providerTitle(p: Provider, short = false): string {
  return (short && SHORT_TITLES[p.id]) || p.title;
}

export function providerGroup(p: Provider): ProviderGroup {
  if (p.group) return p.group;
  if (ACCOUNT_IDS.has(p.id) || p.login) return "account";
  if (p.local) return "local";
  return p.id === "custom" ? "custom" : "key";
}

// --- значки ----------------------------------------------------------------------------------------------
type BrandMark = { path: string; color: string; gradient?: [string, string] };

/** Знаки брендов из simple-icons (цвета — фирменные, на тёмной плитке). */
const MARKS: Record<string, BrandMark> = {
  claude_code: { path: siClaude.path, color: "#D97757" },
  gemini_cli: { path: siGooglegemini.path, color: "#8E75B2", gradient: ["#4C8DF6", "#B57CE6"] },
  gemini: { path: siGooglegemini.path, color: "#8E75B2", gradient: ["#4C8DF6", "#B57CE6"] },
  qwen_code: { path: siQwen.path, color: "#8B7CF8" },
  qwen: { path: siQwen.path, color: "#8B7CF8" },
  mistral: { path: siMistralai.path, color: "#FA520F" },
  moonshot: { path: siKimi.path, color: "#F2F2F2" },
  minimax: { path: siMinimax.path, color: "#F0507A" },
  doubao: { path: siBytedance.path, color: "#4F9BFF" },
};

/** Где знака нет в simple-icons — плитка с буквами. */
const LETTERS: Record<string, { text: string; bg: string }> = {
  groq: { text: "g", bg: "#F55036" },
  xai: { text: "xAI", bg: "#0B0B0D" },
  zhipu: { text: "GLM", bg: "#2F5BFF" },
  siliconflow: { text: "SF", bg: "#6D3AED" },
};

/** Значок провайдера: бренды — simple-icons и AppIcon, остальное — свои плитки. */
export function ProviderIcon({ id, size = 36 }: { id: string; size?: number }) {
  const gradientId = "g" + useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const r = Math.round(size * 0.28);
  const tile = (bg: string, child: ReactNode, cls?: string) => (
    <span className={cx("brand-tile", cls)} style={{ width: size, height: size, borderRadius: r, background: bg }}>
      {child}
    </span>
  );
  const mark = MARKS[id];
  if (mark) {
    const fill = mark.gradient ? `url(#${gradientId})` : mark.color;
    return tile(
      "rgba(255,255,255,0.06)",
      <svg viewBox="0 0 24 24" width={size * 0.56} height={size * 0.56} aria-hidden>
        {mark.gradient && (
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor={mark.gradient[0]} />
              <stop offset="1" stopColor={mark.gradient[1]} />
            </linearGradient>
          </defs>
        )}
        <path d={mark.path} fill={fill} />
      </svg>,
    );
  }
  const letters = LETTERS[id];
  if (letters)
    return tile(
      letters.bg,
      <b style={{ fontSize: size * (letters.text.length > 2 ? 0.3 : letters.text.length > 1 ? 0.36 : 0.42) }}>{letters.text}</b>,
      "letters",
    );
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

// --- «ИИ настроен» -----------------------------------------------------------------------------------------

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
    [
      s.secrets.ai_api_key,
      s.settings?.ai.provider,
      s.settings?.ai.base_url,
      s.settings?.ai.folder_id,
      s.settings?.ai.enabled,
      s.settings?.ai.model,
    ].join("|"),
  );
  useEffect(() => {
    const t = setTimeout(refreshAiConfigured, 400);
    return () => clearTimeout(t);
  }, [key]);
  useEffect(() => onEvent("ai.chatgpt", () => setTimeout(refreshAiConfigured, 400)), []);
  useEffect(() => onEvent("ai.cli", () => setTimeout(refreshAiConfigured, 400)), []);
  return configured;
}

// --- выбор провайдера ----------------------------------------------------------------------------------------

/** Провайдеры: GET /api/ai/providers или запасной список; ChatGPT (подписка) — первым. */
export function useProviders() {
  const q = useApi<{ items: Provider[] }>("/api/ai/providers");
  const list = q.data?.items?.length ? q.data.items : FALLBACK_PROVIDERS;
  const chatgpt = list.find((p) => p.id === "chatgpt") ?? FALLBACK_PROVIDERS[0];
  const items = [chatgpt, ...list.filter((p) => p.id !== "chatgpt")];
  return { ...q, items };
}

/** Выбор провайдера: модель — по умолчанию нового провайдера (или его выбор в программе), адрес и регион — его же. */
export function useChooseProvider() {
  const [settings, patch] = useSettingsPatch();
  return (p: Provider) => {
    if (!settings || settings.ai.provider === p.id) return;
    const keepModel = p.models.includes(settings.ai.model) || (!p.default_model && !p.models.length);
    patch({ ai: { provider: p.id, model: keepModel ? settings.ai.model : p.default_model, base_url: "" } });
  };
}

function ProviderCard({ p, selected, onPick, wide, compact }: { p: Provider; selected: boolean; onPick: () => void; wide?: boolean; compact?: boolean }) {
  const sub = SUBTITLES[p.id] ?? (p.needs_key ? "нужен ключ API" : "без ключа");
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      className={cx("radio-card provider-card", wide && "wide", selected && "selected")}
      onClick={onPick}
    >
      <ProviderIcon id={p.id} size={wide ? (compact ? 36 : 40) : compact ? 30 : 34} />
      <span className="grow prov-text">
        <span className="title prov-title">
          <span className="ellipsis">{providerTitle(p, true)}</span>
          {p.country && COUNTRIES[p.country] && (
            <span className="prov-flag" title={COUNTRIES[p.country]}>
              {p.country.toUpperCase()}
            </span>
          )}
          {p.id === "chatgpt" && <span className="chip accent tiny-chip">рекомендуем</span>}
        </span>
        <span className={cx("sub", !wide && "ellipsis")}>{sub}</span>
      </span>
      <span className="radio-dot" />
    </button>
  );
}

function GroupHead({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="prov-group-head">
      <span className="prov-group-title">{title}</span>
      {hint && <span className="prov-group-hint">{hint}</span>}
    </div>
  );
}

/** Выбор провайдера по группам. compact (мастер первого запуска): сначала только «по подписке», остальное — по кнопке. */
export function ProviderCards({ compact }: { compact?: boolean }) {
  const [settings] = useSettingsPatch();
  const { items } = useProviders();
  const choose = useChooseProvider();
  const current = settings?.ai.provider;
  const [more, setMore] = useState(() => !isAccountProvider(current));
  if (!settings) return null;
  const of = (g: ProviderGroup) => items.filter((p) => providerGroup(p) === g);
  const byKey = of("key").sort((a, b) => providerTitle(a, true).localeCompare(providerTitle(b, true), "ru"));
  const local = of("local");
  const custom = of("custom");
  const card = (p: Provider, wide?: boolean) => (
    <ProviderCard key={p.id} p={p} wide={wide} compact={compact} selected={current === p.id} onPick={() => choose(p)} />
  );
  const others = byKey.length + local.length + custom.length;
  return (
    <div className={cx("prov-groups", compact && "compact")}>
      <section className="prov-group">
        <GroupHead title="По подписке, без ключа" hint="ваш аккаунт — без оплаты за токены" />
        <div className="prov-grid prov-grid-2" role="radiogroup" aria-label="По подписке, без ключа">
          {of("account").map((p) => card(p, true))}
        </div>
      </section>
      {compact && !more ? (
        <button type="button" className="prov-more" onClick={() => setMore(true)}>
          <span className="prov-more-icons" aria-hidden>
            {["openai", "anthropic", "gemini", "deepseek", "yandex", "ollama"].map((id) => (
              <span key={id} className="prov-more-icon">
                <ProviderIcon id={id} size={24} />
              </span>
            ))}
          </span>
          <span className="grow">
            <span className="prov-more-title">Другие провайдеры…</span>
            <span className="prov-more-sub">по ключу API, на этом ПК и свой сервер — ещё {others}</span>
          </span>
          <ChevronDown />
        </button>
      ) : (
        <>
          <section className="prov-group">
            <GroupHead title="По ключу API" hint="оплата за токены у провайдера, ключ хранится только на этом ПК" />
            <div className="prov-grid prov-grid-3" role="radiogroup" aria-label="По ключу API">
              {byKey.map((p) => card(p))}
            </div>
          </section>
          <section className="prov-group">
            <div className="prov-grid prov-grid-3 prov-tail" role="radiogroup" aria-label="На этом ПК и свой сервер">
              <div className="prov-tail-head local">
                <GroupHead title="На этом ПК" hint="бесплатно, без интернета" />
              </div>
              <div className="prov-tail-head custom">
                <GroupHead title="Свой сервер" />
              </div>
              {local.map((p) => card(p))}
              {custom.map((p) => (
                <div key={p.id} className="prov-tail-custom">
                  {card(p)}
                </div>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

// --- подписка ChatGPT ---------------------------------------------------------------------------------------

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

// --- программы-провайдеры: Claude Code, Gemini CLI, Qwen Code -------------------------------------------------

const CLI_ABOUT: Record<string, string> = {
  claude_code: "Jarvis AI работает через Claude Code — по вашей подписке Claude, без ключа API и оплаты за токены",
  gemini_cli: "Jarvis AI работает через Gemini CLI — официальную программу Google на этом ПК",
  qwen_code: "Jarvis AI работает через Qwen Code — с вашим тарифом Alibaba Cloud или ключом",
};

/** Что сделать в открытом окне терминала (программа сама ведёт вход). */
const LOGIN_STEPS: Record<string, string> = {
  claude_code: "В открывшемся окне войдите в аккаунт Claude (если вход не предложен сам — выполните /login)",
  gemini_cli: "В открывшемся окне выберите способ входа в Gemini CLI",
  qwen_code: "В открывшемся окне выполните /auth и выберите тариф Coding Plan или ключ API",
};

function CommandLine({ text }: { text: string }) {
  return (
    <div className="cli-cmd">
      <code className="mono">{text}</code>
      <CopyButton text={text} label="Скопировать команду" />
    </div>
  );
}

/** Программа-провайдер: установлена ли, выполнен ли вход (это делает сама программа), установка и терминал для входа. */
export function CliPanel({ provider, compact }: { provider: Provider; compact?: boolean }) {
  const id = provider.id;
  // при открытии — свежая проверка: человек мог войти в программу сам, в своём терминале
  const q = useApi<CliProviderState>(`/api/ai/cli/${id}?refresh=true`);
  const { data, setData } = q;
  const [busy, setBusy] = useState<"" | "install" | "login" | "check">("");
  const [waiting, setWaiting] = useState(false);
  const [log, setLog] = useState("");
  const name = data?.name || providerTitle(provider);

  /** Перепроверить: найти программу заново и спросить её саму о входе. */
  const check = useCallback(
    async (quiet = false) => {
      if (!quiet) setBusy("check");
      try {
        setData(await api.get<CliProviderState>(`/api/ai/cli/${id}?refresh=true`));
        refreshAiConfigured();
      } catch (e) {
        if (!quiet) notifyError(e);
      } finally {
        if (!quiet) setBusy("");
      }
    },
    [id, setData],
  );

  useEffect(
    () =>
      onEvent<CliProviderEvent>("ai.cli", (e) => {
        const d = e.data;
        if (d.provider !== id) return;
        if (d.state === "installing") {
          setLog("");
          setData((prev) => prev && { ...prev, installing: true, install_error: "" });
        } else if (d.state === "log" && d.line) setLog(d.line);
        else if (d.state === "installed") {
          notify(`${name} установлен${d.version ? ` (версия ${d.version})` : ""}`, "ok");
          void check(true);
        } else if (d.state === "error") {
          if (d.error) notify(d.error, "error");
          void check(true);
        } else if (d.state === "auth") void check(true);
      }),
    [id, name, setData, check],
  );

  // окно терминала открыто: ждём входа и перечитываем состояние (вход проверяет сама программа)
  useEffect(() => {
    if (!waiting) return;
    const poll = setInterval(() => void check(true), 6000);
    const stop = setTimeout(() => setWaiting(false), 300000);
    return () => {
      clearInterval(poll);
      clearTimeout(stop);
    };
  }, [waiting, check]);
  const loggedIn = data?.logged_in === true;
  useEffect(() => {
    if (loggedIn && waiting) {
      setWaiting(false);
      notify(`Вход в ${name} выполнен — Jarvis AI готов`, "ok");
    }
  }, [loggedIn, waiting, name]);

  const install = async () => {
    setBusy("install");
    try {
      await api.post(`/api/ai/cli/${id}/install`);
      setLog("");
      setData((prev) => prev && { ...prev, installing: true, install_error: "" });
    } catch (e) {
      notifyError(e);
      void check(true);
    } finally {
      setBusy("");
    }
  };

  const openTerminal = async () => {
    setBusy("login");
    try {
      const r = await api.post<{ opened: boolean; command: string }>(`/api/ai/cli/${id}/login`);
      if (r.opened) setWaiting(true);
      else notify(`Терминал не открылся — откройте его сами и запустите ${r.command}`, "error");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy("");
    }
  };

  const recheck = (
    <Button size="sm" variant="ghost" icon={<RefreshCw />} loading={busy === "check"} onClick={() => check()}>
      Проверить снова
    </Button>
  );
  const terminal = (primary: boolean) => (
    <Button
      size={primary && !compact ? undefined : "sm"}
      variant={primary ? "primary" : "ghost"}
      icon={<SquareTerminal />}
      loading={busy === "login"}
      onClick={openTerminal}
    >
      {primary ? "Открыть терминал для входа" : "Терминал"}
    </Button>
  );

  // состояние → подсказка, действие и строка с командой
  let hint: ReactNode = null;
  let action: ReactNode = null;
  let extra: ReactNode = null;
  let progressBar: ReactNode = null;
  if (q.missing) {
    hint = "Появится, когда модуль ИИ ядра будет готов";
  } else if (!data) {
    hint = q.error ? `Не удалось узнать состояние: ${q.error.message}` : `Ищу ${name} на этом ПК…`;
    if (!q.error) action = <span className="spinner" />;
  } else if (data.installing) {
    hint = `Устанавливаю ${name} — это займёт минуту-другую…`;
    extra = (log || data.install_log) && <div className="cli-log mono ellipsis">{log || data.install_log}</div>;
    action = <span className="spinner" />;
    progressBar = <ProgressBar value={0.35} className="indeterminate" />;
  } else if (!data.installed) {
    if (!data.npm) {
      // Claude Code ставит сам человек официальным установщиком
      hint = `${name} не установлен. Установите его официальным установщиком: откройте PowerShell и выполните`;
      extra = <CommandLine text={data.install_command} />;
      action = (
        <>
          {!compact && data.install_url && (
            <Button size="sm" variant="ghost" icon={<ExternalLink />} onClick={() => bridge.openExternal(data.install_url)}>
              Инструкция
            </Button>
          )}
          {recheck}
        </>
      );
    } else if (data.can_install) {
      hint = `${name} не установлен — поставлю официальный пакет ${data.npm}`;
      action = (
        <Button variant="primary" size={compact ? "sm" : undefined} icon={<Download />} loading={busy === "install"} onClick={install}>
          Установить
        </Button>
      );
    } else {
      hint = data.node
        ? `Для установки нужен Node.js ${data.node_required} или новее (сейчас ${data.node}) — обновите его и проверьте снова. Или выполните в терминале`
        : `Для установки нужен Node.js ${data.node_required} или новее — поставьте его с nodejs.org и проверьте снова. Или выполните в терминале`;
      extra = <CommandLine text={data.install_command} />;
      action = (
        <>
          <Button size="sm" variant="ghost" icon={<ExternalLink />} onClick={() => bridge.openExternal("https://nodejs.org/")}>
            Node.js
          </Button>
          {recheck}
        </>
      );
    }
  } else if (!loggedIn) {
    if (waiting) {
      hint = `${LOGIN_STEPS[id] ?? "Войдите в открывшемся окне терминала"} — Jarvis проверит сам`;
      action = (
        <>
          <span className="spinner" />
          {recheck}
        </>
      );
    } else {
      hint =
        data.logged_in === false
          ? data.login_hint
          : `Вход пока не проверен — нажмите «Проверить подключение» ниже. Если вы ещё не входили: ${data.login_hint}`;
      action = (
        <>
          {!compact && recheck}
          {terminal(true)}
        </>
      );
    }
  } else {
    hint = (
      <span className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <span className="chip ok">
          <Check /> Вход выполнен{data.account ? `: ${data.account}` : ""}
        </span>
        {data.version && <span className="faint tiny">версия {data.version}</span>}
      </span>
    );
    action = (
      <>
        {recheck}
        {!compact && terminal(false)}
      </>
    );
  }

  const status = data && !q.missing && (
    <span className={cx("chip", loggedIn ? "ok" : data.install_error || data.error ? "err" : "")}>
      <span className={cx("status-dot", loggedIn ? "ok" : data.installing || waiting ? "busy" : data.install_error || data.error ? "err" : "")} />
      {loggedIn
        ? "Готово"
        : data.installing
          ? "Устанавливаю"
          : !data.installed
            ? "Не установлен"
            : waiting
              ? "Жду входа"
              : data.logged_in === false
                ? "Нужен вход"
                : "Вход не проверен"}
    </span>
  );
  const problem = !data?.installing && (data?.install_error || data?.error);
  const error = problem && (
    <div className="test-err" style={{ textAlign: "left" }}>
      {problem}
    </div>
  );
  // у Gemini CLI и Qwen Code в заметке — о закрытом бесплатном входе: это предупреждение
  const note =
    data?.note &&
    (compact ? (
      <div className="cli-note">{data.note}</div>
    ) : (
      <Note icon={<Info />} tone={id === "claude_code" ? "info" : "warn"}>
        {data.note}
      </Note>
    ));

  return (
    <div className={cx("gpt-panel cli-panel", compact && "compact", `cli-${id}`)}>
      <div className="gpt-head">
        <ProviderIcon id={id} size={compact ? 34 : 40} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="gpt-title">{providerTitle(provider)}</div>
          <div className="faint tiny">{compact ? hint : CLI_ABOUT[id]}</div>
        </div>
        {compact ? action : status}
      </div>
      {compact ? (
        <>
          {extra}
          {progressBar}
        </>
      ) : (
        <div className="gpt-row">
          <div className="grow col" style={{ gap: 8, minWidth: 0 }}>
            <span className="small muted">{hint}</span>
            {extra}
            {progressBar}
          </div>
          <div className="row cli-actions">{action}</div>
        </div>
      )}
      {error}
      {note}
    </div>
  );
}

/** Панель входа для провайдера без ключа: ChatGPT или программа-провайдер; для остальных — ничего. */
export function AccountPanel({ compact }: { compact?: boolean }) {
  const [settings] = useSettingsPatch();
  const { items } = useProviders();
  const id = settings?.ai.provider;
  if (!id || !isAccountProvider(id)) return null;
  if (id === "chatgpt") return <ChatGptPanel compact={compact} />;
  const provider = items.find((p) => p.id === id) ?? FALLBACK_PROVIDERS.find((p) => p.id === id);
  return provider ? <CliPanel key={id} provider={provider} compact={compact} /> : null;
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

/** Как выглядит ключ провайдера — подсказка в поле. */
const KEY_PLACEHOLDERS: Record<string, string> = {
  openai: "sk-…",
  anthropic: "sk-ant-…",
  gemini: "AIza…",
  xai: "xai-…",
  openrouter: "sk-or-…",
  groq: "gsk_…",
  deepseek: "sk-…",
  yandex: "AQVN…",
};

const CLI_MODEL_HINT: Record<string, string> = {
  claude_code: "Псевдоним Claude Code (sonnet, opus, haiku) или полное имя модели",
  gemini_cli: "auto — Gemini CLI выбирает сам",
  qwen_code: "Пусто — модель, выбранная в самом Qwen Code",
};

/** Строки подключения: регион, адрес (свой/локальный), ключ, каталог Yandex Cloud, модель, проверка. */
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
  const cli = !!preset?.cli;
  const needsKey = preset?.needs_key ?? true;
  const modelList = [...new Set([...(models.data?.items ?? []), ...(preset?.models ?? [])])];
  const regions = preset?.regions ?? [];
  const trim = (url: string) => url.trim().replace(/\/+$/, "");
  const region = regions.find((r) => trim(r.base_url) === trim(ai.base_url)) ?? (ai.base_url.trim() ? undefined : regions[0]);
  const keyUrl = region?.key_url || preset?.key_url;
  const folderId = ai.folder_id ?? "";

  return (
    <>
      {regions.length > 1 && (
        <Setting name="Регион" desc={compact ? undefined : "Ключ выдаётся в кабинете того же региона — с другим он не подойдёт"}>
          <Tabs
            value={region?.id ?? ""}
            items={regions.map((r) => ({ value: r.id, label: r.title }))}
            onChange={(v) => {
              const picked = regions.find((r) => r.id === v);
              if (picked) patch({ ai: { base_url: picked.id === regions[0].id ? "" : picked.base_url } });
            }}
          />
        </Setting>
      )}
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
      {!cli && (needsKey || ai.provider === "custom") && (
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
          <SecretField name="ai_api_key" placeholder={KEY_PLACEHOLDERS[ai.provider] ?? "Вставьте ключ"} />
        </Setting>
      )}
      {preset?.folder && (
        <Setting
          name="ID каталога"
          desc={compact ? undefined : "Каталог Yandex Cloud, в котором создан ключ: его ID есть на главной странице консоли"}
          wide
        >
          <LazyText value={folderId} placeholder="b1g…" width="100%" onCommit={(v) => patch({ ai: { folder_id: v.trim() } })} />
        </Setting>
      )}
      <Setting name="Модель" desc={compact ? undefined : cli ? CLI_MODEL_HINT[ai.provider] : "Из списка провайдера или своя"}>
        <Combo
          value={ai.model}
          width={compact ? 240 : 260}
          placeholder={preset?.default_model || (cli ? "как в программе" : "имя модели")}
          options={modelList.map((m) => ({ value: m, hint: m === preset?.default_model ? "по умолчанию" : undefined }))}
          onCommit={(v) => patch({ ai: { model: v || preset?.default_model || "" } })}
        />
      </Setting>
      <Setting
        name="Проверка"
        desc={compact ? undefined : cli ? "Короткий запрос через программу: вход, подписка и сеть" : "Короткий запрос к модели: ключ, адрес и прокси"}
      >
        <TestConnection size="sm" />
      </Setting>
    </>
  );
}
