// Значки программ и категорий по ключу (icon в событиях и командах): бренды — из simple-icons,
// чего там нет (Adobe, Microsoft, Minecraft…) — фирменные «плитки» с буквами, категории — lucide.
import {
  AppWindow,
  Bell,
  Bot,
  Calendar,
  Camera,
  CircleAlert,
  CircleHelp,
  Clock,
  CloudSun,
  Command,
  Folder,
  Globe,
  Home,
  Keyboard,
  Lock,
  Monitor,
  MousePointer2,
  Music,
  NotebookPen,
  Power,
  Puzzle,
  Search,
  Sparkles,
  SquareCheckBig,
  Timer,
  Volume2,
  VolumeX,
  LayoutGrid,
  Fish,
  Mic,
  MicOff,
} from "lucide-react";
import type { ReactNode } from "react";
import {
  siAnthropic,
  siBlender,
  siBrave,
  siCounterstrike,
  siDavinciresolve,
  siDeepseek,
  siDiscord,
  siDota2,
  siEpicgames,
  siFigma,
  siFirefoxbrowser,
  siGithub,
  siGmail,
  siGooglechrome,
  siGooglemaps,
  siHabr,
  siHomeassistant,
  siInstagram,
  siKinopoisk,
  siNetflix,
  siNotion,
  siObsstudio,
  siOllama,
  siOpera,
  siOpenrouter,
  siReddit,
  siSpotify,
  siSteam,
  siTelegram,
  siTiktok,
  siTwitch,
  siVk,
  siVlcmediaplayer,
  siWhatsapp,
  siWikipedia,
  siYoutube,
  siZoom,
} from "simple-icons";

type Brand = { path: string; hex: string; bg?: string };
type Tile = { text: string; fg: string; bg: string };

const BRANDS: Record<string, Brand> = {
  spotify: { path: siSpotify.path, hex: "#1ED760" },
  telegram: { path: siTelegram.path, hex: "#2AABEE" },
  discord: { path: siDiscord.path, hex: "#5865F2" },
  obs: { path: siObsstudio.path, hex: "#DCDCE0" },
  steam: { path: siSteam.path, hex: "#C7D5E0", bg: "#1B2838" },
  youtube: { path: siYoutube.path, hex: "#FF0033" },
  chrome: { path: siGooglechrome.path, hex: "#4C8BF5" },
  firefox: { path: siFirefoxbrowser.path, hex: "#FF7139" },
  opera: { path: siOpera.path, hex: "#FF1B2D" },
  brave: { path: siBrave.path, hex: "#FB542B" },
  vk: { path: siVk.path, hex: "#0077FF" },
  notion: { path: siNotion.path, hex: "#F2F2F2" },
  figma: { path: siFigma.path, hex: "#A259FF" },
  zoom: { path: siZoom.path, hex: "#0B5CFF" },
  blender: { path: siBlender.path, hex: "#F5792A" },
  vlc: { path: siVlcmediaplayer.path, hex: "#FF8800" },
  davinci: { path: siDavinciresolve.path, hex: "#E4E4E4" },
  github: { path: siGithub.path, hex: "#F0F0F0" },
  twitch: { path: siTwitch.path, hex: "#9146FF" },
  whatsapp: { path: siWhatsapp.path, hex: "#25D366" },
  cs2: { path: siCounterstrike.path, hex: "#F0A41C" },
  dota2: { path: siDota2.path, hex: "#E53935" },
  epic: { path: siEpicgames.path, hex: "#F2F2F2" },
  netflix: { path: siNetflix.path, hex: "#E50914" },
  tiktok: { path: siTiktok.path, hex: "#F2F2F2" },
  instagram: { path: siInstagram.path, hex: "#FF0069" },
  reddit: { path: siReddit.path, hex: "#FF4500" },
  wikipedia: { path: siWikipedia.path, hex: "#F2F2F2" },
  gmail: { path: siGmail.path, hex: "#EA4335" },
  maps: { path: siGooglemaps.path, hex: "#34A853" },
  kinopoisk: { path: siKinopoisk.path, hex: "#FF6600" },
  habr: { path: siHabr.path, hex: "#65A3BE" },
  homeassistant: { path: siHomeassistant.path, hex: "#18BCF2" },
  anthropic: { path: siAnthropic.path, hex: "#D97757" },
  deepseek: { path: siDeepseek.path, hex: "#4D6BFE" },
  ollama: { path: siOllama.path, hex: "#F2F2F2" },
  openrouter: { path: siOpenrouter.path, hex: "#94A3B8" },
};

const TILES: Record<string, Tile> = {
  photoshop: { text: "Ps", fg: "#31A8FF", bg: "#001E36" },
  premiere: { text: "Pr", fg: "#9999FF", bg: "#00005B" },
  after_effects: { text: "Ae", fg: "#9999FF", bg: "#00005B" },
  illustrator: { text: "Ai", fg: "#FF9A00", bg: "#330000" },
  lightroom: { text: "Lr", fg: "#31A8FF", bg: "#001E36" },
  word: { text: "W", fg: "#ffffff", bg: "#185ABD" },
  excel: { text: "X", fg: "#ffffff", bg: "#107C41" },
  powerpoint: { text: "P", fg: "#ffffff", bg: "#C43E1C" },
  vscode: { text: "</>", fg: "#ffffff", bg: "#0065A9" },
  yandex: { text: "Я", fg: "#ffffff", bg: "#FC3F1D" },
  yandex_browser: { text: "Я", fg: "#ffffff", bg: "#FC3F1D" },
  yandex_music: { text: "♪", fg: "#121212", bg: "#FFCC00" },
  openai: { text: "AI", fg: "#ffffff", bg: "#10A37F" },
  explorer: { text: "📁", fg: "#ffffff", bg: "#2B5797" },
  calculator: { text: "=", fg: "#ffffff", bg: "#3C3C3C" },
  notepad: { text: "✎", fg: "#ffffff", bg: "#2B5797" },
  ozon: { text: "O", fg: "#ffffff", bg: "#005BFF" },
  wildberries: { text: "WB", fg: "#ffffff", bg: "#A73AFD" },
  avito: { text: "A", fg: "#ffffff", bg: "#00AAFF" },
  gosuslugi: { text: "Г", fg: "#ffffff", bg: "#0D4CD3" },
  rutube: { text: "R", fg: "#ffffff", bg: "#100943" },
  windows: { text: "⊞", fg: "#ffffff", bg: "#0078D4" },
};

const CATEGORY: Record<string, ReactNode> = {
  volume: <Volume2 />,
  mute: <VolumeX />,
  media: <Music />,
  music: <Music />,
  window: <AppWindow />,
  windows_ops: <AppWindow />,
  power: <Power />,
  lock: <Lock />,
  screenshot: <Camera />,
  keyboard: <Keyboard />,
  mouse: <MousePointer2 />,
  search: <Search />,
  weather: <CloudSun />,
  timer: <Timer />,
  clock: <Clock />,
  note: <NotebookPen />,
  task: <SquareCheckBig />,
  calendar: <Calendar />,
  desk: <LayoutGrid />,
  ai: <Sparkles />,
  home: <Home />,
  plugin: <Puzzle />,
  error: <CircleAlert />,
  unknown: <CircleHelp />,
  system: <Monitor />,
  folder: <Folder />,
  site: <Globe />,
  web: <Globe />,
  agent: <Bot />,
  bell: <Bell />,
  command: <Command />,
  fish: <Fish />,
  mic: <Mic />,
  "mic-off": <MicOff />,
};

const ALIASES: Record<string, string> = {
  googlechrome: "chrome",
  chromeexe: "chrome",
  obsstudio: "obs",
  obs64: "obs",
  obs64exe: "obs",
  spotifyexe: "spotify",
  telegramdesktop: "telegram",
  telegramexe: "telegram",
  discordexe: "discord",
  visualstudiocode: "vscode",
  code: "vscode",
  codeexe: "vscode",
  adobephotoshop: "photoshop",
  photoshopexe: "photoshop",
  premierepro: "premiere",
  adobepremierepro: "premiere",
  aftereffects: "after_effects",
  adobeaftereffects: "after_effects",
  adobeillustrator: "illustrator",
  adobelightroom: "lightroom",
  microsoftword: "word",
  winword: "word",
  winwordexe: "word",
  microsoftexcel: "excel",
  excelexe: "excel",
  microsoftpowerpoint: "powerpoint",
  minecraftlauncher: "minecraft",
  counterstrike: "cs2",
  counterstrike2: "cs2",
  dota: "dota2",
  firefoxbrowser: "firefox",
  mozillafirefox: "firefox",
  yandexbrowser: "yandex_browser",
  яндексбраузер: "yandex_browser",
  яндексмузыка: "yandex_music",
  yandexmusic: "yandex_music",
  vlcmediaplayer: "vlc",
  davinciresolve: "davinci",
  epicgames: "epic",
  epicgameslauncher: "epic",
  проводник: "explorer",
  explorerexe: "explorer",
  калькулятор: "calculator",
  блокнот: "notepad",
  notepadexe: "notepad",
  vkontakte: "vk",
  вконтакте: "vk",
  ютуб: "youtube",
  chatgpt: "openai",
  gpt: "openai",
  claude: "anthropic",
};

function normKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/\.(exe|lnk)$/, "exe")
    .replace(/[^a-zа-яё0-9]/gi, "");
}

export function resolveIcon(key: string | undefined | null): { brand?: Brand; tile?: Tile; glyph?: ReactNode; minecraft?: boolean } {
  if (!key) return { glyph: CATEGORY.command };
  const raw = key.toLowerCase();
  if (CATEGORY[raw]) return { glyph: CATEGORY[raw] };
  const k = ALIASES[normKey(raw)] ?? ALIASES[raw] ?? (BRANDS[raw] || TILES[raw] ? raw : normKey(raw));
  if (k === "minecraft") return { minecraft: true };
  if (BRANDS[k]) return { brand: BRANDS[k] };
  if (TILES[k]) return { tile: TILES[k] };
  for (const name of Object.keys(BRANDS)) if (normKey(raw).includes(name)) return { brand: BRANDS[name] };
  for (const name of Object.keys(TILES)) if (normKey(raw).includes(name.replace("_", ""))) return { tile: TILES[name] };
  return { glyph: CATEGORY.command };
}

/** Квадратная плитка со значком программы или категории. */
export function AppIcon({ name, size = 36, radius }: { name?: string | null; size?: number; radius?: number }) {
  const r = resolveIcon(name);
  const style: React.CSSProperties = {
    width: size,
    height: size,
    borderRadius: radius ?? Math.round(size * 0.28),
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    flex: "none",
    background: "rgba(255,255,255,0.06)",
    border: "1px solid rgba(255,255,255,0.06)",
    color: "var(--text-2)",
  };
  if (r.brand) {
    return (
      <span style={{ ...style, background: r.brand.bg ?? "rgba(255,255,255,0.06)" }}>
        <svg viewBox="0 0 24 24" width={size * 0.56} height={size * 0.56} aria-hidden>
          <path d={r.brand.path} fill={r.brand.hex} />
        </svg>
      </span>
    );
  }
  if (r.tile) {
    return (
      <span
        style={{
          ...style,
          background: r.tile.bg,
          color: r.tile.fg,
          fontWeight: 800,
          fontSize: size * (r.tile.text.length > 2 ? 0.3 : 0.4),
          border: "1px solid rgba(255,255,255,0.08)",
          letterSpacing: "-0.02em",
        }}
      >
        {r.tile.text}
      </span>
    );
  }
  if (r.minecraft) {
    return (
      <span style={{ ...style, background: "#5B8731", overflow: "hidden" }}>
        <svg viewBox="0 0 8 8" width={size * 0.62} height={size * 0.62} shapeRendering="crispEdges" aria-hidden>
          <rect width="8" height="8" fill="#866043" />
          <rect width="8" height="3" fill="#5DA130" />
          <rect x="1" y="3" width="1" height="1" fill="#5DA130" />
          <rect x="4" y="3" width="2" height="1" fill="#5DA130" />
          <rect x="2" y="5" width="1" height="1" fill="#6B4A31" />
          <rect x="5" y="6" width="1" height="1" fill="#6B4A31" />
        </svg>
      </span>
    );
  }
  return (
    <span style={style}>
      <span className="glyph" style={{ width: size * 0.5, height: size * 0.5 }}>
        {r.glyph}
      </span>
    </span>
  );
}
