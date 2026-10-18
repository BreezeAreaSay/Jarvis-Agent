// Выбор голоса Jarvis: карточки движков синтеза (как «Выберите голос» в Luxify) и их настройки.
import { Fish, Globe, Play, Square } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { Settings } from "@/api/types";
import { AppIcon } from "@/components/AppIcon";
import { LogoMark } from "@/components/Orb";
import { navigate } from "@/lib/router";
import { useApp } from "@/store/app";
import { Button, cx, notify, notifyError, Select, Slider } from "@/ui";
import {
  Combo,
  fmtMb,
  greeting,
  LazyText,
  type ModelItem,
  ProgressBar,
  SecretField,
  useApi,
  useDevices,
  useSettingsPatch,
  useSpeaking,
  useVoiceModels,
} from "./kit";

export type Engine = Settings["tts"]["engine"];

export interface EngineMeta {
  id: Engine;
  title: string;
  sub: string;
  tile: ReactNode;
  offline: boolean;
}

function WindowsGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden>
      <path fill="#1E90FF" d="M3 4.6 10.2 3.6v7H3zM11.2 3.5 21 2v8.6h-9.8zM3 11.6h7.2v7L3 17.6zM11.2 11.6H21V20l-9.8-1.4z" />
    </svg>
  );
}

export const ENGINES: EngineMeta[] = [
  {
    id: "piper",
    title: "Локальный голос Джарвиса",
    sub: "piper, работает без интернета",
    tile: <LogoMark size={30} />,
    offline: true,
  },
  { id: "windows", title: "Голос Windows", sub: "встроенный, работает без интернета", tile: <WindowsGlyph />, offline: true },
  {
    id: "edge",
    title: "Microsoft Edge",
    sub: "естественные голоса, бесплатно, нужен интернет",
    tile: <Globe style={{ color: "#38BDF8" }} />,
    offline: false,
  },
  { id: "openai", title: "OpenAI", sub: "естественные нейросетевые голоса", tile: <AppIcon name="openai" size={32} radius={9} />, offline: false },
  {
    id: "fish",
    title: "Fish Audio",
    sub: "реалистичные и ваши собственные голоса",
    tile: <Fish style={{ color: "#A5B4FC" }} />,
    offline: false,
  },
];

export interface TtsVoice {
  id: string;
  name: string;
  lang?: string;
  gender?: string;
}

const FALLBACK_VOICES: Record<Engine, TtsVoice[]> = {
  piper: [
    { id: "ruslan", name: "Руслан", gender: "male" },
    { id: "denis", name: "Денис", gender: "male" },
    { id: "dmitri", name: "Дмитрий", gender: "male" },
    { id: "irina", name: "Ирина", gender: "female" },
  ],
  windows: [],
  edge: [
    { id: "ru-RU-DmitryNeural", name: "Дмитрий", gender: "male" },
    { id: "ru-RU-SvetlanaNeural", name: "Светлана", gender: "female" },
    { id: "ru-RU-DariyaNeural", name: "Дария", gender: "female" },
  ],
  openai: ["onyx", "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "sage", "shimmer", "verse"].map((v) => ({
    id: v,
    name: v[0].toUpperCase() + v.slice(1),
  })),
  fish: [],
};

const VOICE_FIELD = {
  piper: "piper_voice",
  windows: "windows_voice",
  edge: "edge_voice",
  openai: "openai_voice",
  fish: "fish_voice_id",
} as const satisfies Record<Engine, keyof Settings["tts"]>;

export function voiceOf(tts: Settings["tts"], engine: Engine): string {
  return String(tts[VOICE_FIELD[engine]] ?? "");
}

/** Имя голоса для людей: ruslan → Руслан (из запасного списка), иначе — как есть. */
export function voiceName(engine: Engine, id: string): string {
  return FALLBACK_VOICES[engine].find((v) => v.id === id)?.name ?? id;
}

const OPENAI_MODELS = [
  { value: "gpt-4o-mini-tts", hint: "с интонациями" },
  { value: "tts-1", hint: "быстрый" },
  { value: "tts-1-hd", hint: "чище звук" },
];

const FISH_MODELS = [
  { value: "s1", hint: "лучший" },
  { value: "speech-1.6", hint: "" },
  { value: "speech-1.5", hint: "" },
];

/** Голоса движка: GET /api/tts/voices?engine=… или запасной список. */
export function useTtsVoices(engine: Engine) {
  const q = useApi<{ items: TtsVoice[] }>(`/api/tts/voices?engine=${engine}`);
  const items = q.data?.items?.length ? q.data.items : FALLBACK_VOICES[engine];
  return { ...q, items };
}

/** Фраза для проверки голоса. */
export function samplePhrase(address: string): string {
  return `${greeting()}${address ? `, ${address}` : ""}. Все системы работают в штатном режиме.`;
}

/** Круглая кнопка ▶ / ■ — прослушать голос (POST /api/tts/test). */
export function PlayButton({ engine, voice, text, size = 40 }: { engine: Engine; voice: string; text?: string; size?: number }) {
  const speaking = useSpeaking();
  const [busy, setBusy] = useState(false);
  const [mine, setMine] = useState(false);
  const startedAt = useRef(0);
  useEffect(() => {
    if (!speaking) setMine(false);
  }, [speaking]);
  // ошибка синтеза приходит событием, когда голос уже запускался
  useEffect(
    () =>
      onEvent<{ error: string }>("tts.error", (e) => {
        if (Date.now() - startedAt.current < 30000) notify(e.data.error, "error");
      }),
    [],
  );
  const playing = speaking && mine;
  const click = async () => {
    if (playing) {
      await api.post("/api/tts/stop").catch(notifyError);
      return;
    }
    setBusy(true);
    startedAt.current = Date.now();
    try {
      await api.post("/api/tts/test", { text: text || undefined, engine, voice: voice || undefined });
      setMine(true);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      className={cx("play-round", playing && "playing")}
      style={{ width: size, height: size }}
      aria-label={playing ? "Остановить" : "Прослушать"}
      title={playing ? "Остановить" : "Прослушать"}
      onClick={click}
      disabled={busy}
    >
      {busy ? <span className="spinner" /> : playing ? <Square /> : <Play />}
    </button>
  );
}

/** Голос piper: список голосов с отметкой «скачан» / размером. */
function PiperVoicePicker({ value, onChange, models }: { value: string; onChange: (v: string) => void; models: ModelItem[] }) {
  const voices = useTtsVoices("piper").items;
  const byId = new Map(models.map((m) => [m.id, m]));
  return (
    <Select
      value={value}
      width={168}
      options={voices.map((v) => {
        const m = byId.get(`piper-${v.id}`);
        return {
          value: v.id,
          label: v.name,
          hint: m ? (m.installed ? "✓" : m.downloading ? `${Math.round(m.progress * 100)}%` : fmtMb(m.size_mb)) : undefined,
        };
      })}
      onChange={onChange}
    />
  );
}

/** Голос piper ещё не на этом ПК: скачать и прогресс — отдельной строкой под голосом. */
function PiperDownload({ model, onDownload }: { model: ModelItem | undefined; onDownload: (id: string) => void }) {
  if (!model || model.installed) return null;
  const name = model.title.replace(/^Голос\s*/, "").replace(/\s*\(.*\)\s*$/, "");
  return (
    <div className="voice-dl">
      {model.downloading ? (
        <>
          <span className="small">Скачиваю голос {name}…</span>
          <ProgressBar value={model.progress} />
          <span className="faint tiny">{Math.round(model.progress * 100)}%</span>
        </>
      ) : (
        <>
          <span className="small grow">Голос {name} ещё не скачан на этот компьютер</span>
          <Button size="sm" variant="primary" onClick={() => onDownload(model.id)}>
            Скачать · {fmtMb(model.size_mb)}
          </Button>
        </>
      )}
    </div>
  );
}

/** Голос выбранного движка: список, поле ID (Fish) или «по умолчанию». */
function VoiceControl({
  engine,
  tts,
  patchTts,
  models,
}: {
  engine: Engine;
  tts: Settings["tts"];
  patchTts: (p: Partial<Settings["tts"]>) => void;
  models: ModelItem[];
}) {
  const { items } = useTtsVoices(engine);
  const value = voiceOf(tts, engine);
  if (engine === "piper") {
    return <PiperVoicePicker value={value} onChange={(v) => patchTts({ piper_voice: v as Settings["tts"]["piper_voice"] })} models={models} />;
  }
  if (engine === "fish") {
    return (
      <Combo
        value={value}
        width={240}
        placeholder="ID голоса (reference_id)"
        options={items.map((v) => ({ value: v.id, hint: v.name }))}
        onCommit={(v) => patchTts({ fish_voice_id: v })}
      />
    );
  }
  const options = items.map((v) => ({ value: v.id, label: v.name, hint: v.gender === "female" ? "жен." : v.gender === "male" ? "муж." : undefined }));
  if (engine === "windows") options.unshift({ value: "", label: "По умолчанию", hint: undefined });
  if (value && !options.some((o) => o.value === value)) options.push({ value, label: value, hint: undefined });
  return (
    <Select
      value={value}
      width={220}
      options={options}
      onChange={(v) => patchTts({ [VOICE_FIELD[engine]]: v } as Partial<Settings["tts"]>)}
    />
  );
}

/** Подробности выбранного движка: голос, скорость, ▶ — как «Голос: Джарвис · Скорость 1.0× · ▶», и тонкая настройка. */
export function TtsOptions({ engine, compact }: { engine: Engine; compact?: boolean }) {
  const [settings, patch] = useSettingsPatch();
  const secrets = useApp((s) => s.secrets);
  const models = useVoiceModels();
  const devices = useDevices();
  const [phrase, setPhrase] = useState("");
  const savedSpeed = settings?.tts.speed ?? 1;
  const [speed, setSpeed] = useState(savedSpeed);
  useEffect(() => setSpeed(savedSpeed), [savedSpeed]);
  if (!settings) return null;
  const tts = settings.tts;
  const patchTts = (p: Partial<Settings["tts"]>) => patch({ tts: p });
  const voice = voiceOf(tts, engine);
  const address = settings.profile.address;
  const aiIsOpenai = settings.ai.provider === "openai";
  const outputs = devices.data?.outputs ?? [];

  const keyBlock =
    engine === "openai" ? (
      aiIsOpenai && secrets.ai_api_key ? (
        <div className="tts-key">
          <span className="chip ok">Используется ключ Jarvis AI (OpenAI)</span>
        </div>
      ) : (
        <div className="tts-key">
          <span className="field-label">Ключ OpenAI</span>
          <SecretField name={aiIsOpenai ? "ai_api_key" : "openai_api_key"} placeholder="sk-…" />
        </div>
      )
    ) : engine === "fish" ? (
      <div className="tts-key">
        <span className="field-label">Ключ Fish Audio</span>
        <SecretField name="fish_api_key" placeholder="Ключ из кабинета fish.audio" />
      </div>
    ) : null;

  return (
    <div className="tts-options" onClick={(e) => e.stopPropagation()}>
      <div className="tts-line">
        <div className="tts-voice">
          <span className="tts-label">Голос:</span>
          <VoiceControl engine={engine} tts={tts} patchTts={patchTts} models={models.items} />
        </div>
        <div className="tts-speed">
          <span className="tts-label">Скорость {speed.toFixed(2).replace(/0$/, "")}×</span>
          <Slider
            value={tts.speed}
            min={0.5}
            max={2}
            step={0.05}
            showValue={false}
            onChange={setSpeed}
            onCommit={(v) => patchTts({ speed: Math.round(v * 100) / 100 })}
          />
        </div>
        <PlayButton engine={engine} voice={voice} text={phrase || samplePhrase(address)} size={compact ? 38 : 42} />
      </div>

      {engine === "piper" && (
        <PiperDownload model={models.items.find((m) => m.id === `piper-${tts.piper_voice}`)} onDownload={(id) => void models.download(id)} />
      )}

      {keyBlock}

      {!compact && (
        <div className="tts-grid">
          {engine === "piper" && (
            <label className="tts-cell">
              <span className="tts-label">Высота голоса</span>
              <Slider
                value={tts.pitch}
                min={0.8}
                max={1.2}
                step={0.01}
                format={(v) => `${v.toFixed(2)}`}
                onCommit={(v) => patchTts({ pitch: Math.round(v * 100) / 100 })}
              />
            </label>
          )}
          <label className="tts-cell">
            <span className="tts-label">Эффект «Джарвис»</span>
            <Slider value={Math.round(tts.effect * 100)} onCommit={(v) => patchTts({ effect: Math.round(v) / 100 })} />
          </label>
          <label className="tts-cell">
            <span className="tts-label">Громкость</span>
            <Slider value={tts.volume} onCommit={(v) => patchTts({ volume: Math.round(v) })} />
          </label>
          <div className="tts-cell">
            <span className="tts-label">Устройство вывода</span>
            <Select
              value={tts.output_device}
              options={[
                { value: "", label: "По умолчанию" },
                ...outputs.map((d) => ({ value: d.id, label: d.name, hint: d.default ? "сейчас" : undefined })),
                ...(tts.output_device && !outputs.some((d) => d.id === tts.output_device)
                  ? [{ value: tts.output_device, label: tts.output_device, hint: "нет" }]
                  : []),
              ]}
              onChange={(v) => patchTts({ output_device: v })}
            />
          </div>
          {engine === "openai" && (
            <>
              <div className="tts-cell">
                <span className="tts-label">Модель</span>
                <Combo value={tts.openai_model} options={OPENAI_MODELS} onCommit={(v) => patchTts({ openai_model: v || "gpt-4o-mini-tts" })} />
              </div>
              <div className="tts-cell span-2">
                <span className="tts-label">Как говорить</span>
                <LazyText
                  multiline
                  rows={2}
                  value={tts.openai_instructions}
                  placeholder="Например: спокойно, уверенно, с лёгкой иронией"
                  onCommit={(v) => patchTts({ openai_instructions: v })}
                />
              </div>
            </>
          )}
          {engine === "fish" && (
            <div className="tts-cell">
              <span className="tts-label">Модель Fish Audio</span>
              <Combo value={tts.fish_model} options={FISH_MODELS} onCommit={(v) => patchTts({ fish_model: v || "s1" })} />
            </div>
          )}
          <div className="tts-cell span-2">
            <span className="tts-label">Фраза для проверки</span>
            <input
              className="input"
              value={phrase}
              placeholder={samplePhrase(address)}
              spellCheck={false}
              onChange={(e) => setPhrase(e.target.value)}
            />
          </div>
        </div>
      )}
      {engine === "fish" && !compact && (
        <div className="faint tiny">
          ID голоса — в адресе страницы голоса на fish.audio (можно клонировать свой голос и вставить его ID).
        </div>
      )}
      {engine === "openai" && aiIsOpenai && !secrets.ai_api_key && !compact && (
        <button type="button" className="link-btn tiny" onClick={() => navigate("settings", "ai")}>
          Ключ можно указать и в разделе Jarvis AI
        </button>
      )}
    </div>
  );
}

/** Карточки движков синтеза: выбранная раскрывается с настройками. */
export function TtsEngines({ compact }: { compact?: boolean }) {
  const [settings, patch] = useSettingsPatch();
  const engine = settings?.tts.engine ?? "piper";
  const list = useMemo(() => ENGINES, []);
  if (!settings) return null;
  return (
    <div className={cx("engine-list", compact && "compact")} role="radiogroup" aria-label="Голос Jarvis">
      {list.map((e) => {
        const selected = e.id === engine;
        return (
          <div
            key={e.id}
            role="radio"
            aria-checked={selected}
            tabIndex={0}
            className={cx("radio-card engine-card", selected && "selected")}
            onClick={() => !selected && patch({ tts: { engine: e.id } })}
            onKeyDown={(ev) => {
              if ((ev.key === "Enter" || ev.key === " ") && ev.target === ev.currentTarget) {
                ev.preventDefault();
                if (!selected) patch({ tts: { engine: e.id } });
              }
            }}
          >
            <div className="engine-head">
              <span className="tile">{e.tile}</span>
              <span className="grow">
                <div className="title">{e.title}</div>
                <div className="sub">{e.sub}</div>
              </span>
              <span className="radio-dot" />
            </div>
            {selected && <TtsOptions engine={e.id} compact={compact} />}
          </div>
        );
      })}
    </div>
  );
}
