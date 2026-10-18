// «Jarvis AI» (быстрый вопрос), «Плагины» (быстрые переключатели), «Столы» (переход на другие столы)
// и «Питомец» (живая пиксельная медуза).
import {
  ArrowUpRight,
  Briefcase,
  Clock,
  Cpu,
  Gamepad2,
  GraduationCap,
  Home,
  LayoutGrid,
  Music2,
  PawPrint,
  Puzzle,
  SendHorizontal,
  Sparkles,
  Sunrise,
  TrendingUp,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { ExecResult } from "@/api/types";
import { navigate } from "@/lib/router";
import { useDesks } from "@/store/desks";
import { cx, Dots, notifyError } from "@/ui";
import { PluginTile, pluginHealth, type Plugin } from "@/pages/extensions/pluginKit";
import { bindPlugins, usePlugins } from "@/pages/extensions/pluginStore";
import { WidgetFrame, type WidgetProps } from "../common";
import { type JellyMood, PetCanvas } from "./PixelJelly";
import { CfgButton, HeadButton, toList, useBox, WidgetSettingsModal } from "./shared";

// --- Jarvis AI ------------------------------------------------------------------------------------------
type Answer = { kind: "ai" | "command" | "unknown" | "error" | "pending"; text: string; chat?: string; ok?: boolean };

interface DispatchResult {
  kind: string;
  result?: ExecResult;
  chat_id?: string;
  text?: string;
  error?: string;
}

export function AiWidget({ h, config }: WidgetProps) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const ask = async () => {
    const text = q.trim();
    if (!text || answer?.kind === "pending") return;
    setAnswer({ kind: "pending", text });
    try {
      const res = await api.post<DispatchResult>("/api/dispatch", { text, source: "ui" });
      setQ("");
      if (res.kind === "ai") setAnswer({ kind: res.error ? "error" : "ai", text: res.error || res.text || "Готово", chat: res.chat_id });
      else if (res.kind === "command")
        setAnswer({ kind: "command", ok: res.result?.ok, text: [res.result?.title, res.result?.message].filter(Boolean).join(" — ") || "Выполнено" });
      else if (res.kind === "unknown") setAnswer({ kind: "unknown", text: "Не понял команду. Включите Jarvis AI в настройках, чтобы задавать любые вопросы" });
      else setAnswer(null);
    } catch (e) {
      setAnswer({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  };
  const tall = h >= 2;
  return (
    <div className={cx("widget wx-ai", tall && "tall")}>
      <div className="wx-ai-row">
        <span className="wx-ai-orb">
          <Sparkles />
        </span>
        <input
          className="wx-ai-input"
          value={q}
          placeholder={(config.placeholder as string) || "Спросите Jarvis…"}
          spellCheck={false}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
        />
        <button className="wx-ai-send" onClick={ask} disabled={!q.trim() || answer?.kind === "pending"} aria-label="Спросить">
          <SendHorizontal />
        </button>
      </div>
      <div className={cx("wx-ai-answer", answer?.kind)}>
        {!answer ? (
          <span className="faint">Ответ появится здесь — коротко, без перехода в чат</span>
        ) : answer.kind === "pending" ? (
          <span className="faint row" style={{ gap: 8 }}>
            <Dots /> Думаю над «{answer.text}»
          </span>
        ) : (
          <>
            <span className={tall ? "wx-ai-text" : "wx-ai-text one"}>{answer.text}</span>
            {answer.chat && (
              <button className="wx-link" onClick={() => navigate("ai", answer.chat)}>
                В чат <ArrowUpRight />
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// --- плагины --------------------------------------------------------------------------------------------
const SHORT_NAMES: Record<string, string> = {
  smart_home: "Дом",
  dictation: "Диктовка",
  subtitles: "Субтитры",
  summary: "Конспект",
  reader: "Чтение",
  autolock: "Блокировка",
  second_pc: "Второй ПК",
  notify_voice: "Озвучка",
  pet: "Питомец",
  focus: "Фокус",
  clipboard: "Буфер",
  file_search: "Поиск",
};

export function PluginsWidget({ w, h, config, setConfig }: WidgetProps) {
  const items = usePlugins((s) => s.items);
  const update = usePlugins((s) => s.update);
  useEffect(() => bindPlugins(), []);
  const ids = toList(config.items);
  const all = items ?? [];
  const chosen = ids.length ? ids.map((id) => all.find((p) => p.id === id)).filter((p): p is Plugin => !!p) : all;
  const [ref, box] = useBox<HTMLDivElement>();
  const cell = h >= 2 ? 76 : 62;
  const perRow = Math.max(1, Math.floor((box.width + 6) / (cell + 6)));
  const rows = Math.max(1, Math.floor((box.height + 6) / ((h >= 2 ? 78 : 56) + 6)));
  const shown = box.width ? chosen.slice(0, perRow * rows) : chosen;
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<string[]>([]);
  const [busy, setBusy] = useState("");

  const toggle = async (p: Plugin) => {
    setBusy(p.id);
    try {
      await update(p.id, { enabled: !p.enabled });
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy("");
    }
  };

  return (
    <WidgetFrame
      icon={<Puzzle />}
      title={(config.title as string) || "Плагины"}
      actions={
        <>
          <CfgButton
            onClick={() => {
              setPick(ids.length ? ids : chosen.map((p) => p.id));
              setOpen(true);
            }}
          />
          <HeadButton icon={<ArrowUpRight />} label="Открыть «Расширения»" onClick={() => navigate("extensions", "plugins")} />
        </>
      }
    >
      <div ref={ref} className="wx-plugins-box">
        {!items ? (
          <div className="sub">Загружаю плагины…</div>
        ) : !chosen.length ? (
          <div className="sub">Выберите плагины в настройках виджета</div>
        ) : (
          <div className="wx-plugins" style={{ gridTemplateColumns: `repeat(${Math.min(perRow, shown.length) || 1}, ${cell}px)` }}>
            {shown.map((p) => {
              const health = pluginHealth(p);
              return (
                <button
                  key={p.id}
                  className={cx("wx-plugin", busy === p.id && "busy", w <= 1 && "narrow")}
                  onClick={() => toggle(p)}
                  title={`${p.name}: ${p.enabled ? "включён — нажмите, чтобы выключить" : "выключен — нажмите, чтобы включить"}`}
                >
                  <span className="wx-plugin-tile">
                    <PluginTile plugin={p} size={h >= 2 ? 48 : 34} dim={!p.enabled} />
                    {health !== "off" && <span className={cx("pl-badge", health)} />}
                  </span>
                  <span className="wx-plugin-name">{SHORT_NAMES[p.id] ?? p.name.split(/[ ,]/)[0]}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
      <WidgetSettingsModal open={open} title="Плагины на виджете" width={520} onClose={() => setOpen(false)} onSave={() => setConfig({ ...config, items: pick })}>
        <div className="wx-plugin-pick">
          {(all.length ? all : []).map((p) => {
            const on = pick.includes(p.id);
            return (
              <button key={p.id} className={cx("wx-app-row", on && "on")} onClick={() => setPick((x) => (on ? x.filter((i) => i !== p.id) : [...x, p.id]))}>
                <PluginTile plugin={p} size={26} />
                <span className="grow ellipsis">{p.name}</span>
                <span className={cx("check-box", on && "on")} style={on ? { background: "var(--accent)", borderColor: "var(--accent)" } : undefined} />
              </button>
            );
          })}
          {!all.length && <div className="faint small">Плагины пока недоступны</div>}
        </div>
      </WidgetSettingsModal>
    </WidgetFrame>
  );
}


// --- столы ----------------------------------------------------------------------------------------------
const DESK_ICONS: Record<string, ReactNode> = {
  home: <Home />,
  main: <Home />,
  gamepad: <Gamepad2 />,
  games: <Gamepad2 />,
  briefcase: <Briefcase />,
  work: <Briefcase />,
  book: <GraduationCap />,
  study: <GraduationCap />,
  clock: <Clock />,
  chart: <TrendingUp />,
  finance: <TrendingUp />,
  music: <Music2 />,
  media: <Music2 />,
  monitor: <Cpu />,
  computer: <Cpu />,
  paw: <PawPrint />,
  pet: <PawPrint />,
  avatar: <Sparkles />,
  sunrise: <Sunrise />,
  morning: <Sunrise />,
  desk: <LayoutGrid />,
};

export function DeskLinksWidget({ w, h, config }: WidgetProps) {
  const desks = useDesks((s) => s.desks);
  const current = useDesks((s) => s.current);
  const select = useDesks((s) => s.select);
  const [ref, box] = useBox<HTMLDivElement>();
  // мало места — только значки (название в подсказке), иначе — значок и название
  const compact = (w <= 1 && h <= 1) || (box.width > 0 && box.width < desks.length * 96 && h <= 1);
  return (
    <WidgetFrame icon={<LayoutGrid />} title={(config.title as string) || "Столы"}>
      <div ref={ref} className={cx("wx-desks", compact && "compact")}>
        {desks.map((d) => (
          <button key={d.id} className={cx("wx-desk", d.id === current && "on")} onClick={() => select(d.id)} title={`Открыть стол «${d.name}»`}>
            {DESK_ICONS[d.icon] ?? DESK_ICONS[d.preset] ?? <LayoutGrid />}
            {!compact && <span className="ellipsis">{d.name}</span>}
          </button>
        ))}
      </div>
    </WidgetFrame>
  );
}

// --- питомец --------------------------------------------------------------------------------------------
const PET_TEXT: Record<string, string> = {
  dance: "Танцует",
  music: "Слушает музыку",
  sleep: "Спит",
  eat: "Ест",
  feed: "Ест",
  happy: "Радуется",
  play: "Играет",
  walk: "Гуляет",
};

export function PetWidget({ w, h, config }: WidgetProps) {
  const pet = usePlugins((s) => s.items?.find((p) => p.id === "pet"));
  const live = usePlugins((s) => s.live.pet?.action);
  useEffect(() => bindPlugins(), []);
  const [ref, box] = useBox<HTMLDivElement>();
  const [poke, setPoke] = useState(0);
  const pokeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(pokeTimer.current), []);
  const action = String((live?.data as Record<string, unknown> | undefined)?.action ?? pet?.status?.action ?? "idle");
  const recent = live ? Date.now() - live.ts < 10_000 : false;
  const music = pet?.enabled && pet.status?.playing === true && pet.config?.dance_to_music !== false;
  const mood: JellyMood = poke
    ? "happy"
    : action === "sleep"
      ? "sleep"
      : (recent && (action === "dance" || action === "music")) || music
        ? "dance"
        : recent && (action === "eat" || action === "feed")
          ? "eat"
          : "idle";
  const name = String(config.name || pet?.config?.name || "Люкси");
  const size = Math.max(48, Math.min(box.width, box.height));
  const status = poke
    ? "Мур!"
    : PET_TEXT[action] && (recent || action === "sleep")
      ? PET_TEXT[action]
      : music
        ? "Танцует под музыку"
        : pet && !pet.enabled
          ? "Плагин выключен"
          : "Плавает";
  return (
    <div className="widget wx-pet">
      <div ref={ref} className="wx-pet-stage">
        {box.width > 0 && (
          <PetCanvas
            size={size}
            mood={mood}
            onClick={() => {
              setPoke(Date.now());
              clearTimeout(pokeTimer.current);
              pokeTimer.current = setTimeout(() => setPoke(0), 1800);
            }}
          />
        )}
      </div>
      <div className="wx-pet-foot">
        <span className="lv-petname">{name}</span>
        {(h >= 2 || w >= 2) && <span className="faint tiny">{status}</span>}
      </div>
    </div>
  );
}
