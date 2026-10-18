// Типы контракта ядра (docs/CONTRACTS.md). Settings повторяет core/jarvis/settings.py.

export type VoiceState = "off" | "loading" | "idle" | "listening" | "processing" | "speaking" | "error";
export type AiProvider =
  | "openai"
  | "chatgpt"
  | "claude_code"
  | "gemini_cli"
  | "qwen_code"
  | "openrouter"
  | "deepseek"
  | "anthropic"
  | "gemini"
  | "xai"
  | "mistral"
  | "groq"
  | "qwen"
  | "moonshot"
  | "zhipu"
  | "minimax"
  | "doubao"
  | "siliconflow"
  | "yandex"
  | "ollama"
  | "lmstudio"
  | "custom";

export interface Settings {
  version: number;
  onboarding_done: boolean;
  profile: { name: string; address: string; city: string; assistant_name: string };
  interface: {
    accent: string;
    transparency: number;
    blur: number;
    animations: boolean;
    language: "ru" | "en";
    minimize_to_tray: boolean;
    always_on_top: boolean;
    window_material: "none" | "acrylic" | "mica";
    start_minimized: boolean;
    ui_scale: number;
  };
  voice: {
    enabled: boolean;
    wake_words: string[];
    prefix_mode: boolean;
    silent_mode: boolean;
    input_device: string;
    sensitivity: number;
    asr_model: "gigaam-v3" | "gigaam-v3-punct" | "vosk-small";
    follow_up_s: number;
    ptt_key: string;
    earcons: boolean;
    max_utterance_s: number;
    confirm_by_voice: boolean;
    speak_confirmations: "short" | "off";
  };
  tts: {
    engine: "piper" | "windows" | "edge" | "openai" | "fish";
    piper_voice: "ruslan" | "denis" | "dmitri" | "irina";
    windows_voice: string;
    edge_voice: string;
    openai_model: string;
    openai_voice: string;
    openai_instructions: string;
    fish_voice_id: string;
    fish_model: string;
    speed: number;
    pitch: number;
    effect: number;
    volume: number;
    output_device: string;
  };
  ai: {
    enabled: boolean;
    provider: AiProvider;
    base_url: string;
    folder_id: string;
    model: string;
    temperature: number;
    proxy: string;
    persona: "jarvis" | "plain";
    custom_instructions: string;
    web_search: boolean;
    memory: boolean;
    mode: "chat" | "all" | "agent";
    vision: boolean;
    agent_confirm: "risky" | "always";
    max_agent_steps: number;
    monthly_budget: number;
    /** Разрешения инструментов ИИ по имени; нет записи или auto — по риску. */
    tool_policy: Record<string, "auto" | "allow" | "ask" | "deny">;
  };
  avatar: {
    desktop: boolean;
    size: number;
    on_top: boolean;
    animated: boolean;
    position: number[] | null;
    hide_fullscreen: boolean;
  };
  system: {
    autostart: boolean;
    hotkey_show: string;
    hotkey_listen: string;
    hotkey_mic: string;
    check_updates: boolean;
    auto_update: boolean;
    overlay: boolean;
  };
  remote: {
    lan_enabled: boolean;
    lan_port: number;
    telegram_enabled: boolean;
    telegram_owner_id: number;
    favorites: string[];
  };
  /** Сроки хранения в днях; 0 — хранить, пока не удалите сами. */
  privacy: {
    history_days: number;
    chat_days: number;
    voice_chat_days: number;
    tool_results_days: number;
    audio_days: number;
    logs_days: number;
  };
  plugins: Record<string, { enabled: boolean; config: Record<string, unknown> }>;
  packs: string[];
  disabled_commands: string[];
}

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

export type SecretName =
  | "ai_api_key"
  | "openai_api_key"
  | "fish_api_key"
  | "telegram_token"
  | "ha_token"
  | "yandex_iot_token"
  | "remote_token";

export type SecretsStatus = Partial<Record<SecretName, boolean>>;

export interface CoreEvent<T = Record<string, unknown>> {
  type: string;
  data: T;
  ts: number;
}

export interface HistoryItem {
  id: number;
  ts: number;
  source: string;
  text: string;
  kind: "command" | "ai" | "unknown" | "system";
  title: string;
  status: "ok" | "error" | "pending" | "cancelled" | "denied";
  detail: string;
  icon: string;
  command_id: string;
}

export interface ExecResult {
  ok: boolean;
  title: string;
  message: string;
  icon: string;
  say: string;
  command_ids: string[];
  data: Record<string, unknown>;
}

export interface ToastEvent {
  title: string;
  message: string;
  status: "ok" | "error" | "info";
  icon: string;
  ts: number;
}

export interface Confirmation {
  id: string;
  title: string;
  detail: string;
  kind: "command" | "agent" | "plugin";
  source: string;
  created: number;
  timeout: number;
}

// --- команды ---------------------------------------------------------------------------------------
export interface CommandAction {
  type: string;
  value: string;
}

export interface CommandInfo {
  id: string;
  name: string;
  category: string;
  phrases: string[];
  optional: string[];
  actions: CommandAction[];
  confirm: boolean;
  chain: boolean;
  icon: string;
  enabled: boolean;
  say: string;
  source: "builtin" | "pack" | "user" | "dynamic";
  pack?: string;
  editable: boolean;
}

export interface CommandIn {
  name: string;
  phrases: string[];
  optional: string[];
  actions: CommandAction[];
  confirm: boolean;
  chain: boolean;
  icon?: string;
  say?: string;
  category?: string;
  enabled?: boolean;
}

export interface ActionParam {
  name: string;
  kind: "text" | "number" | "keys" | "path" | "app" | "url" | "select" | "coords";
  label: string;
  placeholder?: string;
  options?: { value: string; label: string }[];
}

export interface ActionType {
  type: string;
  title: string;
  category: string;
  icon: string;
  params: ActionParam[];
  description: string;
}

export interface Pack {
  id: string;
  name: string;
  app: string;
  icon: string;
  description: string;
  commands_count: number;
  installed: boolean;
  commands: { id: string; name: string; phrases: string[] }[];
}

export interface AppEntry {
  name: string;
  app_id: string;
  path: string;
  source: string;
}

// --- сценарии (docs/CONTRACTS.md §6) ---------------------------------------------------------------
export type ScenarioTrigger =
  | { type: "time"; at: string; weekdays: number[] } // weekdays: 0 — Пн; [] — каждый день
  | { type: "interval"; minutes: number; only_active: boolean }
  | { type: "phrase"; phrases: string[] }
  | { type: "jarvis_start" }
  | { type: "app_open"; app: string }
  | { type: "app_close"; app: string }
  | { type: "idle_return"; after_min: number }
  | { type: "battery_low"; percent: number }
  | { type: "calendar_event"; before_min: number };

export type ScenarioTriggerType = ScenarioTrigger["type"];

export interface ScenarioConditions {
  weekdays?: number[];
  time_from?: string; // "HH:MM" или ""
  time_to?: string;
}

export interface Scenario {
  id: string;
  name: string;
  icon: string;
  enabled: boolean;
  triggers: ScenarioTrigger[];
  conditions: ScenarioConditions;
  actions: CommandAction[];
  say: string;
  template: string;
  last_run: number | null;
  last_ok: boolean | null;
  last_trigger: string; // тип триггера или "manual"
  last_error: string;
  runs: number;
  created: number;
  updated: number;
}

export interface ScenarioIn {
  name: string;
  icon?: string;
  enabled?: boolean;
  triggers: ScenarioTrigger[];
  conditions?: ScenarioConditions;
  actions: CommandAction[];
  say?: string;
  template?: string; // id шаблона, из которого создан (только при создании)
}

export interface ScenarioTemplate {
  id: string;
  name: string;
  icon: string;
  description: string;
  triggers: ScenarioTrigger[];
  conditions: ScenarioConditions;
  actions: CommandAction[];
  say: string;
  added: boolean;
}

export interface ScenarioRunEvent {
  id: string;
  name: string;
  trigger: string;
  ok: boolean;
  error?: string;
  cancelled?: boolean;
}

// --- ИИ --------------------------------------------------------------------------------------------
export interface Chat {
  id: string;
  title: string;
  kind: "chat" | "voice";
  mode: "chat" | "all" | "agent";
  model: string;
  pinned: boolean | number;
  created: number;
  updated: number;
}

export interface UiMessage {
  id: string;
  chat_id: string;
  role: "user" | "assistant";
  content: string;
  created: number;
  meta: {
    sources?: { title: string; url: string }[];
    actions?: { title: string; ok: boolean; detail?: string }[];
    memory?: string[];
    images?: string[];
    error?: string;
    usage?: Record<string, number>;
    status?: string;
    stopped?: boolean;
    agent?: { run_id: string; status: string; steps: number; plan: string[] };
  };
}

/** Группа в выборе провайдера: по подписке (без ключа), по ключу API, на этом ПК, свой сервер. */
export type ProviderGroup = "account" | "key" | "local" | "custom";

/** Регион провайдера (Китай / международный): свой адрес API и свой кабинет для ключа. */
export interface ProviderRegion {
  id: string;
  title: string;
  base_url: string;
  key_url: string;
}

export interface ProviderPreset {
  id: AiProvider;
  title: string;
  base_url: string;
  needs_key: boolean;
  default_model: string;
  models: string[];
  local?: boolean;
  key_url?: string;
  login?: boolean;
  component?: boolean;
  /** Работает через официальную программу человека (Claude Code, Gemini CLI, Qwen Code). */
  cli?: boolean;
  group?: ProviderGroup;
  /** cn | ru — значок страны. */
  country?: string;
  /** Первый — по умолчанию; выбранный регион = settings.ai.base_url (пусто — первый). */
  regions?: ProviderRegion[];
  /** Нужен ID каталога Yandex Cloud (settings.ai.folder_id). */
  folder?: boolean;
}

/** GET /api/ai/cli/{provider} — программа-провайдер на этом ПК. */
export interface CliProviderState {
  id: AiProvider;
  name: string;
  installed: boolean;
  path: string;
  version: string;
  /** true / false / null — неизвестно (проверьте подключение). */
  logged_in: boolean | null;
  account: string;
  installing: boolean;
  install_error: string;
  install_log: string;
  can_install: boolean;
  /** Пакет npm для кнопки «Установить» ("" — ставит сам человек официальным установщиком). */
  npm: string;
  /** Версия Node.js на ПК ("" — не найден). */
  node: string;
  node_required: number;
  install_url: string;
  install_command: string;
  login_command: string;
  login_hint: string;
  note: string;
  images: boolean;
  agent: boolean;
  tools: boolean;
  error: string;
}

/** Событие ai.cli: установка программы-провайдера, терминал для входа, смена состояния входа. */
export interface CliProviderEvent {
  provider: AiProvider;
  state: "installing" | "log" | "installed" | "error" | "login" | "auth";
  line?: string;
  version?: string;
  error?: string;
  opened?: boolean;
  logged_in?: boolean | null;
}

export type MemoryKind = "fact" | "assumption" | "temp";

export interface MemoryItem {
  id: string;
  text: string;
  /** ai — записал Jarvis по просьбе в разговоре, user — добавили или поправили вы. */
  source: string;
  /** fact — факт, assumption — предположение (Jarvis уточняет), temp — на сутки. */
  kind: MemoryKind;
  /** Когда временная запись забудется (unix-секунды); null — бессрочно. */
  expires: number | null;
  created: number;
}

export interface NoteItem {
  id: string;
  title: string;
  body: string;
  pinned: number | boolean;
  color: string;
  created: number;
  updated: number;
}

export interface TaskItem {
  id: string;
  title: string;
  done: number | boolean;
  due: number | null;
  position: number;
  created: number;
  completed: number | null;
}

export interface EventItem {
  id: string;
  title: string;
  start: number;
  end: number | null;
  all_day: number | boolean;
  notes: string;
  remind_min: number | null;
}

export interface ReminderItem {
  id: string;
  text: string;
  at: number;
  repeat: string | null;
  done: number | boolean;
}

export interface McpServer {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers?: Record<string, string>;
  enabled: boolean;
  status: "connected" | "connecting" | "error" | "disabled";
  error: string;
  tools: { name: string; description: string }[];
}

export interface McpCatalogItem {
  name: string;
  title: string;
  description: string;
  version: string;
  repository: string;
  website?: string;
  installs?: number;
  install: {
    transport: "stdio" | "http";
    command?: string;
    args?: string[];
    url?: string;
    env_vars?: { name: string; description?: string; required?: boolean; secret?: boolean; default?: string }[];
    headers?: { name: string; description?: string; required?: boolean; secret?: boolean; default?: string }[];
  } | null;
}

// --- столы, виджеты, плагины ------------------------------------------------------------------------
export interface WidgetPlacement {
  id: string;
  type: string;
  x: number;
  y: number;
  w: number;
  h: number;
  config: Record<string, unknown>;
}

export interface Desk {
  id: string;
  name: string;
  icon: string;
  preset: string;
  widgets: WidgetPlacement[];
}

export interface DeskPreset {
  id: string;
  name: string;
  description: string;
  icon: string;
  widgets: WidgetPlacement[];
}

export interface PluginSchemaItem {
  key: string;
  kind: "text" | "number" | "bool" | "select" | "secret" | "hotkey" | "device" | "list";
  label: string;
  options?: { value: string; label: string }[] | string[];
  secret?: SecretName;
  set?: boolean;
  default?: unknown;
  hint?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  device?: string;
  fields?: PluginSchemaItem[];
}

export interface PluginInfo {
  id: string;
  name: string;
  description: string;
  icon: string;
  color: string[] | string;
  enabled: boolean;
  config: Record<string, unknown>;
  schema: PluginSchemaItem[];
  status: Record<string, unknown>;
  actions: { id: string; title: string }[];
  examples?: string[];
}

export interface MediaInfo {
  title: string;
  artist: string;
  album: string;
  app: string;
  status: "playing" | "paused" | "stopped" | "unknown";
  position_s: number;
  duration_s: number;
  thumbnail: string;
}

export interface VoiceModel {
  id: string;
  kind: "asr" | "stream" | "vad" | "tts" | "whisper";
  title: string;
  size_mb: number;
  installed: boolean;
  downloading: boolean;
  progress: number;
  required: boolean;
  description?: string;
  error?: string;
}

export interface RemoteInfo {
  lan?: { enabled: boolean; port: number; urls: string[]; qr_svg: string; status?: string; error?: string };
  telegram?: {
    enabled: boolean;
    status: "off" | "connecting" | "online" | "error";
    bot: string;
    owner_id: number;
    pairing_code?: string;
    pairing_expires?: number | null;
    pairing_link?: string;
    token_set?: boolean;
    error?: string;
  };
}

// --- «Умный буфер» (плагин clipboard): события plugin.clipboard.* и ответы действий -------------------------
export interface ClipboardItem {
  id: string;
  text: string; // начало записи (до 400 знаков); целиком вставляется по id
  chars: number;
  ts: number;
  pinned: boolean;
}

export interface ClipboardItems {
  items: ClipboardItem[];
  total: number;
  pinned: number;
  skipped?: number; // копий из менеджеров паролей, которые не запомнены
}

export interface ClipboardPalette extends ClipboardItems {
  actions: { id: string; title: string }[];
  at: { x: number; y: number } | null; // курсор, физические пиксели экрана
  selection: string;
  selection_chars: number;
  target: { title: string; exe: string } | null;
  ai: boolean;
  hotkey: string;
}

export interface ClipboardResult {
  id: string;
  state: "working" | "done" | "replaced" | "error";
  action: string;
  title: string;
  source: string;
  text?: string;
  error?: string;
  at?: { x: number; y: number } | null;
}

// --- «Фокус» (plugin.focus.*) ---------------------------------------------------------------------------
export type FocusPhase = "idle" | "focus" | "break" | "paused";

/** Состояние «Фокуса»: событие plugin.focus.state и статус плагина (секунды интерфейс отсчитывает сам). */
export interface FocusState {
  phase: FocusPhase;
  paused_from: "" | "focus" | "break";
  reason: "" | "user" | "idle"; // почему пауза: сами или вас не было за компьютером
  long_break: boolean;
  left_s: number;
  total_s: number;
  ends_at: number | null; // unix-время конца, только пока таймер идёт
  cycle: number; // номер текущего (или последнего) фокуса в серии
  long_every: number;
  next: "" | "focus" | "break" | "long_break";
  waiting: boolean; // перерыв кончился, ждём следующий фокус
  today_min: number;
  today_sessions: number;
  streak: number;
  focus_min: number;
  break_min: number;
  long_break_min: number;
  guard: "remind" | "close" | "off";
  allowed: string[];
}

export interface FocusDay {
  date: string; // YYYY-MM-DD
  min: number;
  sessions: number;
}

/** Статус плагина «Фокус» = состояние + статистика. */
export interface FocusStatus extends FocusState {
  week: FocusDay[]; // 7 дней, старые первыми
  best: { date: string; min: number } | null;
  distractions: number;
  total_min: number;
  total_sessions: number;
}

/** Карточка «Фокуса» в оверлее (plugin.focus.alert). */
export interface FocusAlert {
  kind: "focus_done" | "break_done" | "back" | "distraction";
  title: string;
  text: string;
  phase: FocusPhase;
  left_s: number;
  ts: number;
  minutes?: number; // focus_done
  break_min?: number;
  long?: boolean;
  auto?: boolean; // break_done: следующий фокус начался сам
  key?: string; // distraction: app:telegram | site:youtube
  name?: string;
  icon?: string;
  site?: boolean;
  closed?: boolean;
}

// --- «Поиск по ПК» (плагин file_search) -------------------------------------------------------------------

/** Тип найденного: значок и фильтр. */
export type FileKind =
  | "document"
  | "spreadsheet"
  | "presentation"
  | "image"
  | "video"
  | "audio"
  | "archive"
  | "pdf"
  | "folder"
  | "other";

/** Найденный файл или папка (действие search, событие plugin.file_search.results). */
export interface FoundFile {
  path: string;
  name: string;
  kind: FileKind;
  size: number; // байт; у папок 0
  modified: number; // unix-время
  folder: string; // где лежит
  where: string; // она же для человека: «Документы › Проекты»
}

/** Ответ действия search: mode — где искали (индекс Windows или свой индекс Jarvis). */
export interface FileSearchResult {
  ok: boolean;
  query: string;
  items: FoundFile[];
  total: number;
  mode: "index" | "own";
  content_skipped?: boolean; // текст файлов не просматривался (свой индекс) — искали в именах
}

/** Карточка голосового поиска в оверлее (plugin.file_search.results): ttl — сколько ждём «открой второй». */
export interface FileSearchCard {
  query: string;
  items: FoundFile[];
  total: number;
  ttl: number;
}
