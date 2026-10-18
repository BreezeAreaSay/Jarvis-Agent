// Значки серверов MCP: бренд угадывается по названию, команде и адресу (GitHub, Notion, Figma…),
// иначе — значок по смыслу (файлы, браузер, память, время) или общий «сервер».
import { Brain, BrainCircuit, Clock, Database, FolderOpen, Globe, Plug, Search, Server, TerminalSquare } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import type { McpCatalogItem, McpServer } from "@/api/types";
import {
  siAirtable,
  siAnthropic,
  siAsana,
  siAtlassian,
  siBitbucket,
  siBlender,
  siBrave,
  siClickup,
  siCloudflare,
  siConfluence,
  siDiscord,
  siDocker,
  siDropbox,
  siDuckduckgo,
  siElasticsearch,
  siFigma,
  siFirebase,
  siGit,
  siGithub,
  siGitlab,
  siGmail,
  siGooglecalendar,
  siGoogledrive,
  siGooglemaps,
  siGrafana,
  siHomeassistant,
  siHubspot,
  siHuggingface,
  siJira,
  siKubernetes,
  siLinear,
  siMiro,
  siModelcontextprotocol,
  siMongodb,
  siMysql,
  siN8n,
  siNetlify,
  siNotion,
  siObsidian,
  siPerplexity,
  siPostgresql,
  siPuppeteer,
  siRedis,
  siSentry,
  siShopify,
  siSpotify,
  siSqlite,
  siStripe,
  siSupabase,
  siTelegram,
  siTodoist,
  siTrello,
  siVercel,
  siWikipedia,
  siYoutube,
  siZapier,
} from "simple-icons";

type Brand = { path: string; hex: string };
type SimpleIcon = { path: string; hex: string };

function brand(icon: SimpleIcon, light?: string): Brand {
  const hex = `#${icon.hex}`;
  const n = Number.parseInt(icon.hex, 16);
  const lum = (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255;
  return { path: icon.path, hex: light ?? (lum < 0.22 ? "#EDEFF3" : hex) };
}

const BRANDS: Record<string, Brand> = {
  github: brand(siGithub),
  gitlab: brand(siGitlab),
  bitbucket: brand(siBitbucket),
  git: brand(siGit),
  notion: brand(siNotion),
  figma: brand(siFigma, "#A259FF"),
  todoist: brand(siTodoist),
  jira: brand(siJira),
  confluence: brand(siConfluence),
  atlassian: brand(siAtlassian),
  trello: brand(siTrello),
  asana: brand(siAsana),
  linear: brand(siLinear),
  clickup: brand(siClickup),
  miro: brand(siMiro),
  sentry: brand(siSentry, "#A78BFA"),
  stripe: brand(siStripe),
  supabase: brand(siSupabase),
  vercel: brand(siVercel),
  netlify: brand(siNetlify),
  cloudflare: brand(siCloudflare),
  docker: brand(siDocker),
  kubernetes: brand(siKubernetes),
  postgres: brand(siPostgresql, "#6FA8DC"),
  sqlite: brand(siSqlite, "#7DB9E8"),
  mysql: brand(siMysql, "#4DA3C7"),
  mongodb: brand(siMongodb),
  redis: brand(siRedis),
  elastic: brand(siElasticsearch),
  firebase: brand(siFirebase),
  obsidian: brand(siObsidian),
  duckduckgo: brand(siDuckduckgo),
  brave: brand(siBrave),
  perplexity: brand(siPerplexity),
  airtable: brand(siAirtable),
  gdrive: brand(siGoogledrive),
  gcalendar: brand(siGooglecalendar),
  gmail: brand(siGmail),
  maps: brand(siGooglemaps),
  dropbox: brand(siDropbox),
  youtube: brand(siYoutube),
  spotify: brand(siSpotify),
  telegram: brand(siTelegram),
  discord: brand(siDiscord),
  blender: brand(siBlender),
  wikipedia: brand(siWikipedia),
  homeassistant: brand(siHomeassistant),
  huggingface: brand(siHuggingface),
  zapier: brand(siZapier),
  n8n: brand(siN8n, "#FF6D8A"),
  shopify: brand(siShopify),
  hubspot: brand(siHubspot),
  grafana: brand(siGrafana),
  puppeteer: brand(siPuppeteer),
  anthropic: brand(siAnthropic),
  mcp: brand(siModelcontextprotocol),
};

// ключевое слово в названии/команде → ключ бренда (первое совпадение)
const KEYWORDS: [RegExp, string][] = [
  [/github/, "github"],
  [/gitlab/, "gitlab"],
  [/bitbucket/, "bitbucket"],
  [/notion/, "notion"],
  [/figma/, "figma"],
  [/todoist|doist/, "todoist"],
  [/jira/, "jira"],
  [/confluence/, "confluence"],
  [/atlassian/, "atlassian"],
  [/trello/, "trello"],
  [/asana/, "asana"],
  [/linear/, "linear"],
  [/clickup/, "clickup"],
  [/miro/, "miro"],
  [/sentry/, "sentry"],
  [/stripe/, "stripe"],
  [/supabase/, "supabase"],
  [/vercel/, "vercel"],
  [/netlify/, "netlify"],
  [/cloudflare/, "cloudflare"],
  [/docker/, "docker"],
  [/kubernetes|k8s|kubectl/, "kubernetes"],
  [/postgres|pgsql/, "postgres"],
  [/sqlite/, "sqlite"],
  [/mysql|mariadb/, "mysql"],
  [/mongo/, "mongodb"],
  [/redis/, "redis"],
  [/elastic/, "elastic"],
  [/firebase/, "firebase"],
  [/obsidian/, "obsidian"],
  [/duckduckgo/, "duckduckgo"],
  [/brave/, "brave"],
  [/perplexity/, "perplexity"],
  [/airtable/, "airtable"],
  [/google.?drive|gdrive/, "gdrive"],
  [/calendar/, "gcalendar"],
  [/gmail/, "gmail"],
  [/google.?maps|maps/, "maps"],
  [/dropbox/, "dropbox"],
  [/youtube/, "youtube"],
  [/spotify/, "spotify"],
  [/telegram/, "telegram"],
  [/discord/, "discord"],
  [/blender/, "blender"],
  [/wiki/, "wikipedia"],
  [/home.?assistant|hass/, "homeassistant"],
  [/hugging/, "huggingface"],
  [/zapier/, "zapier"],
  [/n8n/, "n8n"],
  [/shopify/, "shopify"],
  [/hubspot/, "hubspot"],
  [/grafana/, "grafana"],
  [/puppeteer|playwright/, "puppeteer"],
  [/anthropic|claude/, "anthropic"],
  [/\bgit\b|mcp-server-git|server-git\b/, "git"],
];

const GLYPHS: [RegExp, ReactNode][] = [
  [/ai-dev|ai dev system/, <BrainCircuit key="aidev" />],
  [/file|папк|файл|fs\b|disk/, <FolderOpen key="fs" />],
  [/fetch|browser|chrome|сайт|\bweb\b/, <Globe key="web" />],
  [/memory|память|knowledge|think/, <Brain key="mem" />],
  [/time|clock|время/, <Clock key="time" />],
  [/search|поиск|\bexa\b|tavily/, <Search key="search" />],
  [/sql|db|database|база/, <Database key="db" />],
  [/shell|terminal|cmd|powershell|desktop-commander/, <TerminalSquare key="sh" />],
];

function host(url: string | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

/** Где искать бренд: название, команда, аргументы и адрес сервера (но не репозиторий — он почти всегда на GitHub). */
function haystack(x: Partial<McpServer & McpCatalogItem>): string {
  const install = (x as McpCatalogItem).install;
  // имя в реестре — «io.github.<автор>/<сервер>»: пространство имён ничего не говорит о бренде
  const name = (x.name ?? "").split("/").pop();
  return [name, x.title, x.command, ...(x.args ?? []), host(x.url), install?.command, ...(install?.args ?? []), host(install?.url)]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function mcpBrandKey(x: Partial<McpServer & McpCatalogItem>): string {
  const h = haystack(x);
  for (const [re, key] of KEYWORDS) if (re.test(h)) return key;
  return "";
}

export const GITHUB_PATH = siGithub.path;

/** Плитка-значок сервера MCP (бренд, значок по смыслу или общий). */
export function McpIcon({
  item,
  brandKey,
  size = 36,
  className,
}: {
  item?: Partial<McpServer & McpCatalogItem>;
  brandKey?: string;
  size?: number;
  className?: string;
}) {
  const key = brandKey ?? (item ? mcpBrandKey(item) : "");
  const b = BRANDS[key];
  const style = { width: size, height: size, borderRadius: Math.round(size * 0.28) } as CSSProperties;
  if (b) {
    return (
      <span className={`mcp-icon ${className ?? ""}`} style={style}>
        <svg viewBox="0 0 24 24" width={size * 0.54} height={size * 0.54} aria-hidden>
          <path d={b.path} fill={b.hex} />
        </svg>
      </span>
    );
  }
  const h = item ? haystack(item) : "";
  const transport = item?.transport ?? (item as McpCatalogItem | undefined)?.install?.transport;
  const glyph = GLYPHS.find(([re]) => re.test(h))?.[1] ?? (!item ? <Plug /> : transport === "http" ? <Globe /> : <Server />);
  return (
    <span className={`mcp-icon glyph ${className ?? ""}`} style={style}>
      <span style={{ width: size * 0.5, height: size * 0.5, display: "inline-flex" }}>{glyph}</span>
    </span>
  );
}

/** Бренды для витрины на орбите, когда своих серверов мало. */
export const SHOWCASE = [
  "github",
  "notion",
  "figma",
  "blender",
  "obsidian",
  "todoist",
  "jira",
  "wikipedia",
  "brave",
  "mongodb",
  "gdrive",
  "postgres",
  "airtable",
  "duckduckgo",
  "linear",
  "telegram",
  "youtube",
  "docker",
];

export const MCP_STATUS: Record<string, { text: string; dot: string }> = {
  connected: { text: "Подключён", dot: "ok" },
  connecting: { text: "Подключаюсь…", dot: "busy" },
  error: { text: "Ошибка", dot: "err" },
  disabled: { text: "Выключен", dot: "" },
};

// --- аргументы командной строки ---------------------------------------------------------------------
/** «-y "C:\My Files" --flag» → ["-y", "C:\My Files", "--flag"] (кавычки группируют, обратная косая — как есть). */
export function splitArgs(s: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote: string | null = null;
  let started = false;
  for (const ch of s) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (cur || started) out.push(cur);
      cur = "";
      started = false;
      continue;
    }
    cur += ch;
  }
  if (cur || started) out.push(cur);
  return out;
}

export function joinArgs(args: string[] | undefined): string {
  return (args ?? []).map((a) => (a === "" || /[\s"']/.test(a) ? `"${a.replace(/"/g, "'")}"` : a)).join(" ");
}

export function shortRepo(url: string): string {
  return url.replace(/^https?:\/\/(www\.)?/, "").replace(/^github\.com\//, "").replace(/\.git$/, "").replace(/\/$/, "");
}
