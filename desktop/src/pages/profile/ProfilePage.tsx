// «ПРОФИЛЬ»: сфера, приветствие и версия, статистика за сегодня, как Jarvis к вам обращается, сводка настроек.
import "@/styles/settings.css";
import {
  AudioLines,
  ChevronRight,
  CircleCheck,
  MapPin,
  MessagesSquare,
  Mic,
  Smartphone,
  Sparkles,
  SquareTerminal,
  UserRound,
  Zap,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import type { HistoryItem } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { Orb } from "@/components/Orb";
import { bridge } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { Card, Setting } from "@/ui";
import { cap, LazyText, useApi, useSettingsPatch } from "../settings/kit";
import { ENGINES, voiceName, voiceOf } from "../settings/tts";
import { FALLBACK_PROVIDERS, providerTitle, useAiConfigured } from "../settings/ai";

export type AddressMode = "sir" | "maam" | "name" | "custom";

export function addressMode(address: string, name: string): AddressMode {
  const a = address.trim().toLowerCase();
  if (a === "сэр") return "sir";
  if (a === "мэм") return "maam";
  if (name.trim() && address.trim() === name.trim()) return "name";
  return "custom";
}

/** «Как к вам обращаться»: сэр / мэм / по имени / своё. По имени — обращение повторяет имя. */
export function AddressPicker({
  address,
  name,
  onChange,
  align = "end",
}: {
  address: string;
  name: string;
  onChange: (address: string) => void;
  align?: "start" | "end";
}) {
  const mode = addressMode(address, name);
  const [custom, setCustom] = useState(mode === "custom");
  useEffect(() => {
    if (mode !== "custom") setCustom(false);
  }, [mode]);
  const pick = (m: AddressMode) => {
    setCustom(m === "custom");
    if (m === "sir") onChange("сэр");
    if (m === "maam") onChange("мэм");
    if (m === "name") onChange(name.trim());
  };
  const active = custom ? "custom" : mode;
  return (
    <div className="col" style={{ gap: 8, alignItems: align === "end" ? "flex-end" : "flex-start" }}>
      <div className="address-pick" role="radiogroup" aria-label="Обращение">
        <button type="button" className={active === "sir" ? "pill on" : "pill"} onClick={() => pick("sir")}>
          Сэр
        </button>
        <button type="button" className={active === "maam" ? "pill on" : "pill"} onClick={() => pick("maam")}>
          Мэм
        </button>
        <button
          type="button"
          className={active === "name" ? "pill on" : "pill"}
          disabled={!name.trim()}
          title={name.trim() ? undefined : "Сначала укажите имя"}
          onClick={() => pick("name")}
        >
          По имени
        </button>
        <button type="button" className={active === "custom" ? "pill on" : "pill"} onClick={() => pick("custom")}>
          Своё
        </button>
      </div>
      {active === "custom" && (
        <LazyText
          value={mode === "custom" ? address : ""}
          placeholder="Например: шеф, босс, капитан"
          width={240}
          onCommit={(v) => onChange(v)}
        />
      )}
    </div>
  );
}

function StatTile({ icon, value, label }: { icon: ReactNode; value: ReactNode; label: string }) {
  return (
    <div className="stat-tile">
      <span className="stat-ico">{icon}</span>
      <span className="stat-num">{value}</span>
      <span className="faint small">{label}</span>
    </div>
  );
}

function SummaryRow({ icon, title, value, to }: { icon: ReactNode; title: string; value: string; to: string }) {
  return (
    <button type="button" className="summary-row" onClick={() => navigate("settings", to)}>
      <span className="summary-ico">{icon}</span>
      <span className="grow" style={{ minWidth: 0 }}>
        <div className="small">{title}</div>
        <div className="faint tiny ellipsis">{value}</div>
      </span>
      <ChevronRight className="chev" />
    </button>
  );
}

function startOfToday(): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

export default function ProfilePage() {
  const [settings, patch] = useSettingsPatch();
  const version = useApp((s) => s.version);
  const voiceState = useApp((s) => s.voiceState);
  const aiConfigured = useAiConfigured();
  const liveHistory = useApp((s) => s.history);
  const history = useApi<{ items: HistoryItem[] }>("/api/history?limit=500");
  const chats = useApi<{ items: unknown[] }>("/api/chats");
  const { reload } = history;
  // новая запись в истории — пересчитать статистику
  const newest = liveHistory[0]?.id;
  useEffect(() => {
    if (newest !== undefined) void reload();
  }, [newest, reload]);
  if (!settings) return null;
  const p = settings.profile;
  const since = startOfToday();
  const today = (history.data?.items ?? []).filter((h) => h.ts >= since && h.source !== "system");
  const commands = today.filter((h) => h.kind === "command");
  const ok = today.filter((h) => h.status === "ok").length;
  const rate = today.length ? Math.round((ok / today.length) * 100) : null;
  const engine = ENGINES.find((e) => e.id === settings.tts.engine);
  const voice = voiceOf(settings.tts, settings.tts.engine);
  const provider = FALLBACK_PROVIDERS.find((x) => x.id === settings.ai.provider);
  const name = p.name.trim();
  const hello = name ? `Привет, ${name}!` : "Добро пожаловать!";
  const changeName = (v: string) => {
    // «по имени» — обращение следует за именем
    const byName = addressMode(p.address, p.name) === "name";
    patch({ profile: { name: v, ...(byName && v ? { address: v } : {}) } });
  };

  return (
    <div className="page set-scope">
      <PageHeader title="Профиль" />
      <div className="page-body">
        <div className="stack" style={{ maxWidth: 1080 }}>
          <div className="set-hero prof-hero">
            <div className="prof-orb">
              <Orb scale={0.4} />
            </div>
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="eyebrow">{p.assistant_name || "Джарвис"} к вашим услугам</div>
              <div className="prof-hello">{hello}</div>
              <div className="muted small">
                Jarvis {bridge.version}
                {version && version !== bridge.version ? ` · ядро ${version}` : ""} · {p.city || "город не указан"}
              </div>
              <div className="hero-chips">
                <span className="hero-chip">
                  <Mic />{" "}
                  {voiceState === "off"
                    ? "Микрофон выключен"
                    : voiceState === "error"
                      ? "Голос не готов"
                      : `Скажите «${cap(settings.voice.wake_words[0] ?? "джарвис")}»`}
                </span>
                <span className="hero-chip">
                  <Sparkles /> {aiConfigured ? "Jarvis AI подключён" : "ИИ не настроен"}
                </span>
              </div>
            </div>
          </div>

          <div className="prof-stats">
            <StatTile icon={<SquareTerminal />} value={history.data ? commands.length : "—"} label="команд сегодня" />
            <StatTile icon={<Zap />} value={history.data ? today.length : "—"} label="запросов сегодня" />
            <StatTile icon={<CircleCheck />} value={rate === null ? "—" : `${rate}%`} label="выполнено успешно" />
            <StatTile icon={<MessagesSquare />} value={chats.data ? chats.data.items.length : "—"} label="чатов с Jarvis AI" />
          </div>

          <div className="prof-grid">
            <Card icon={<UserRound />} title="О вас" sub="Jarvis учитывает это в ответах и командах">
              <Setting name="Имя" desc="Как вас зовут">
                <LazyText value={p.name} placeholder="Ваше имя" width={240} onCommit={changeName} />
              </Setting>
              <Setting name="Обращение" desc={`«Готово, ${p.address || "сэр"}»`}>
                <AddressPicker address={p.address} name={p.name} onChange={(address) => patch({ profile: { address } })} />
              </Setting>
              <Setting name="Город" desc="Для погоды и времени">
                <LazyText
                  value={p.city}
                  placeholder="Москва"
                  width={240}
                  icon={<MapPin />}
                  onCommit={(city) => patch({ profile: { city } })}
                />
              </Setting>
              <Setting name="Имя ассистента" desc="Как Jarvis представляется">
                <LazyText
                  value={p.assistant_name}
                  placeholder="Джарвис"
                  width={240}
                  onCommit={(v) => patch({ profile: { assistant_name: v || "Джарвис" } })}
                />
              </Setting>
              <div className="say-quote" style={{ marginTop: 10 }}>
                <span className="faint tiny">Так звучит ответ:</span>
                <div style={{ marginTop: 4 }}>
                  «Готово, {p.address || "сэр"}.» · «{p.assistant_name || "Джарвис"} на связи
                  {name && p.address !== name ? `, ${name}` : ""}.»
                </div>
              </div>
            </Card>

            <Card icon={<Sparkles />} title="Ваш Jarvis" sub="Коротко о главном — нажмите, чтобы изменить">
              <SummaryRow
                icon={<Mic />}
                title="Голос и распознавание"
                value={`${settings.voice.enabled ? "Микрофон включён" : "Микрофон выключен"} · активатор «${cap(settings.voice.wake_words.join(", "))}»`}
                to="voice"
              />
              <SummaryRow
                icon={<AudioLines />}
                title="Голос Jarvis"
                value={`${engine?.title ?? settings.tts.engine}${voice ? ` · ${voiceName(settings.tts.engine, voice)}` : ""} · ${settings.tts.speed.toFixed(2).replace(/0$/, "")}×`}
                to="tts"
              />
              <SummaryRow
                icon={<Sparkles />}
                title="Jarvis AI"
                value={`${provider ? providerTitle(provider, true) : settings.ai.provider} · ${settings.ai.model || "модель не выбрана"}`}
                to="ai"
              />
              <SummaryRow
                icon={<Smartphone />}
                title="Пульт в телефоне"
                value={
                  [settings.remote.lan_enabled && "Wi-Fi", settings.remote.telegram_enabled && "Telegram"].filter(Boolean).join(" · ") ||
                  "Выключен"
                }
                to="remote"
              />
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
