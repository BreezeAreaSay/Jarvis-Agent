// Типы контракта ядра (docs/CONTRACTS.md). Settings повторяет core/jarvis/settings.py.

export type VoiceState = "off" | "loading" | "idle" | "listening" | "processing" | "speaking" | "error";
export type AiProvider =
  | "openai"
  | "chatgpt"
  | "openrouter"
  | "deepseek"
  | "anthropic"
  | "groq"
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
    check_updates: boolean;
    overlay: boolean;
  };
  remote: {
    lan_enabled: boolean;
    lan_port: number;
    telegram_enabled: boolean;
    telegram_owner_id: number;
    favorites: string[];
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
}

export interface MemoryItem {
  id: string;
  text: string;
  source: string;
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
