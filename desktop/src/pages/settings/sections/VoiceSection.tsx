// «Голос и распознавание»: микрофон, слово-активатор, ответы голосом и модели распознавания.
import {
  Activity,
  AudioLines,
  Cloud,
  Cpu,
  Download,
  Ear,
  ExternalLink,
  Languages,
  MessageSquare,
  Mic,
  MicOff,
  Package,
  Sparkles,
  WifiOff,
} from "lucide-react";
import type { ReactNode } from "react";
import type { Settings } from "@/api/types";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, Card, cx, Select, Setting, Slider, Toggle } from "@/ui";
import {
  cap,
  ChipsEditor,
  fmtMb,
  KeyCapture,
  MicMeter,
  type ModelItem,
  ModelRow,
  Note,
  SecretField,
  useDevices,
  useSettingsPatch,
  useVoiceModels,
  useVoiceStatus,
  voiceStateText,
} from "../kit";

const MODEL_ICONS: Record<string, ReactNode> = {
  vad: <Activity />,
  stream: <AudioLines />,
  asr: <Ear />,
  whisper: <Languages />,
  tts: <MessageSquare />,
};

const ASR_OPTIONS: { value: Settings["voice"]["asr_model"]; label: string; hint: string }[] = [
  { value: "elevenlabs", label: "ElevenLabs Scribe", hint: "облако, быстрее всех" },
  { value: "gigaam-v3", label: "GigaAM v3 — команды", hint: "точная" },
  { value: "gigaam-v3-punct", label: "GigaAM v3 с пунктуацией", hint: "диктовка" },
  { value: "vosk-small", label: "Vosk small", hint: "лёгкая" },
];

const LANGUAGE_OPTIONS: { value: Settings["voice"]["language"]; label: string; hint?: string }[] = [
  { value: "ru", label: "Русский", hint: "точнее всего" },
  { value: "auto", label: "Определять самому", hint: "Whisper" },
  { value: "en", label: "Английский" },
  { value: "uk", label: "Украинский" },
  { value: "de", label: "Немецкий" },
  { value: "fr", label: "Французский" },
  { value: "es", label: "Испанский" },
  { value: "it", label: "Итальянский" },
  { value: "pt", label: "Португальский" },
  { value: "pl", label: "Польский" },
  { value: "tr", label: "Турецкий" },
  { value: "ja", label: "Японский" },
  { value: "ko", label: "Корейский" },
  { value: "zh", label: "Китайский" },
];

const fmtSeconds = (ms: number) => `${String(Number((ms / 1000).toFixed(2))).replace(".", ",")} с`;
const ELEVEN_KEYS_URL = "https://elevenlabs.io/app/settings/api-keys";

/** Полоса «Приватно и быстро» (распознавание на ПК) или «Быстро и точно» (ElevenLabs), живой уровень микрофона. */
function VoiceHero() {
  const [settings, patch] = useSettingsPatch();
  const state = useApp((s) => s.voiceState);
  const reason = useApp((s) => s.voiceReason);
  const status = useVoiceStatus();
  if (!settings) return null;
  const v = settings.voice;
  const live = state === "listening" || state === "processing";
  const error = state === "error" ? status.data?.error : "";
  const cloud = v.asr_model === "elevenlabs";
  return (
    <div className="set-hero voice-hero">
      <div className="grow">
        <div className="eyebrow">Распознавание речи</div>
        <h2 className="hero-title">{cloud ? "Быстро и точно" : "Приватно и быстро"}</h2>
        <div className="hero-chips">
          {cloud ? (
            <>
              <span className="hero-chip">
                <Cloud /> ElevenLabs по ходу речи
              </span>
              <span className="hero-chip">
                <Cpu /> «{cap(v.wake_words[0] ?? "Джарвис")}» — на ПК
              </span>
            </>
          ) : (
            <>
              <span className="hero-chip">
                <Cpu /> На вашем ПК
              </span>
              <span className="hero-chip">
                <WifiOff className="wifi-off" /> Без интернета
              </span>
            </>
          )}
        </div>
      </div>
      <div className="hero-mic">
        <button
          type="button"
          className={cx("mic-orb", v.enabled && "on", live && "mic-live")}
          aria-label={v.enabled ? "Выключить микрофон" : "Включить микрофон"}
          title={v.enabled ? "Выключить микрофон" : "Включить микрофон"}
          onClick={() => patch({ voice: { enabled: !v.enabled } })}
        >
          {v.enabled ? <Mic /> : <MicOff />}
        </button>
        <div className="col" style={{ gap: 6, minWidth: 0 }}>
          <div className="row small" style={{ gap: 8 }}>
            <span
              className={cx(
                "status-dot",
                state === "error" ? "err" : state === "idle" ? "ok" : state === "off" ? "" : "busy",
              )}
            />
            <span className="ellipsis">{voiceStateText(state, cap(v.wake_words[0] ?? "Джарвис"), reason)}</span>
          </div>
          {error ? <div className="hero-mic-err">{error}</div> : <MicMeter className={cx(!v.enabled && "off")} />}
        </div>
      </div>
    </div>
  );
}

/** Распознавание ElevenLabs: ключ (тот же, что у голоса) и почему облако сейчас не зовётся. */
function CloudAsr() {
  const status = useVoiceStatus();
  const hasKey = useApp((s) => !!s.secrets.elevenlabs_api_key);
  const error = status.data?.cloud_error;
  return (
    <>
      {!hasKey && (
        <Setting name="Ключ ElevenLabs" desc="Тот же ключ, что у голоса ElevenLabs. Нужно право «Speech to Text»" wide>
          <div className="row" style={{ gap: 8 }}>
            <SecretField name="elevenlabs_api_key" placeholder="sk_… из кабинета elevenlabs.io" />
            <Button size="sm" variant="ghost" icon={<ExternalLink />} onClick={() => bridge.openExternal(ELEVEN_KEYS_URL)}>
              Получить ключ
            </Button>
          </div>
        </Setting>
      )}
      {error && hasKey ? (
        <Note tone="warn">{`${cap(error)}. Пока распознаю на ПК (Vosk)`}</Note>
      ) : (
        <Note icon={<Cloud />}>
          Звук фразы уходит в ElevenLabs, пока вы говорите, — текст готов к концу фразы. Уходят только фразы к Jarvis
          (после «Джарвис», с клавишей, в окне продолжения); тратятся кредиты ElevenLabs по секундам звука. Нет сети —
          распознаю на ПК
        </Note>
      )}
    </>
  );
}

function MicrophoneCard() {
  const [settings, patch] = useSettingsPatch();
  const devices = useDevices();
  if (!settings) return null;
  const v = settings.voice;
  const cloud = v.asr_model === "elevenlabs";
  const inputs = devices.data?.inputs ?? [];
  const def = inputs.find((d) => d.default);
  const options = [
    { value: "", label: def ? `По умолчанию — ${def.name}` : "По умолчанию" },
    ...inputs.map((d) => ({ value: d.id, label: d.name })),
  ];
  if (v.input_device && !inputs.some((d) => d.id === v.input_device)) {
    options.push({ value: v.input_device, label: `${v.input_device} (не найден)` });
  }
  return (
    <Card icon={<Mic />} title="Микрофон" sub="Jarvis слушает только слово-активатор, пока вы его не позовёте">
      <Setting name="Микрофон" desc="Голосовое управление включено">
        <Toggle checked={v.enabled} onChange={(on) => patch({ voice: { enabled: on } })} label="Микрофон" />
      </Setting>
      <Setting
        name="Устройство ввода"
        desc={
          devices.missing ? (
            "Список устройств появится, когда голосовой модуль будет готов"
          ) : devices.data?.error ? (
            <span className="err-text">{devices.data.error}</span>
          ) : undefined
        }
        wide
      >
        <Select value={v.input_device} options={options} onChange={(id) => patch({ voice: { input_device: id } })} width="100%" />
      </Setting>
      <Setting name="Уровень сигнала" desc="Скажите что-нибудь — полоска должна ожить" wide>
        <MicMeter segments={30} className={cx("wide-meter", !v.enabled && "off")} />
      </Setting>
      <Setting name="Чувствительность" desc="Выше — слышит тихую речь, ниже — меньше реагирует на шум" wide>
        <Slider
          value={Math.round(v.sensitivity * 100)}
          onCommit={(x) => patch({ voice: { sensitivity: Math.round(x) / 100 } })}
        />
      </Setting>
      <Setting
        name="Пауза в конце фразы"
        desc="Столько тишины значит «я договорил». Говорите с паузами — прибавьте (после «открой…» Jarvis и так подождёт)"
        wide
      >
        <Slider
          value={v.end_silence_ms}
          min={300}
          max={2000}
          step={50}
          format={fmtSeconds}
          onCommit={(x) => patch({ voice: { end_silence_ms: Math.round(x / 50) * 50 } })}
        />
      </Setting>
      <Setting
        name="Язык команд"
        desc={
          v.language !== "ru"
            ? "Распознаёт Whisper (скачается сам, ~200 МБ; пока его нет — слушаю по-русски). Встроенные команды Jarvis — русские: фразы на другом языке выполняет Jarvis AI"
            : cloud
              ? "Русский — ElevenLabs, «Джарвис» — Vosk на ПК"
              : "Русский распознаёт GigaAM — точнее и быстрее всего"
        }
      >
        <Select value={v.language} width={250} options={LANGUAGE_OPTIONS} onChange={(l) => patch({ voice: { language: l } })} />
      </Setting>
      <Setting
        name="Модель распознавания"
        desc={
          cloud
            ? "Облако ElevenLabs по ключу — самый быстрый и точный итог фразы"
            : v.language === "ru"
              ? "Работает на вашем ПК — без интернета"
              : "Для русского языка команд"
        }
      >
        <Select
          value={v.asr_model}
          width={250}
          options={ASR_OPTIONS}
          onChange={(m) => patch({ voice: { asr_model: m } })}
        />
      </Setting>
      {cloud && <CloudAsr />}
    </Card>
  );
}

function WakeCard() {
  const [settings, patch] = useSettingsPatch();
  const status = useVoiceStatus();
  if (!settings) return null;
  const pttError = settings.voice.ptt_key ? status.data?.ptt_error : "";
  const v = settings.voice;
  const name = settings.profile.assistant_name.trim().toLowerCase();
  const suggest = name && !v.wake_words.includes(name) ? name : "";
  return (
    <Card icon={<Sparkles />} title="Активация" sub="Как позвать Jarvis голосом и клавишей">
      <Setting
        name="Слова-активаторы"
        desc={
          <>
            {suggest ? (
              <>
                Добавьте и имя ассистента —{" "}
                <button type="button" className="link-btn" onClick={() => patch({ voice: { wake_words: [...v.wake_words, suggest] } })}>
                  «{suggest}»
                </button>
                .{" "}
              </>
            ) : (
              "Enter — добавить слово. "
            )}
            Узнаю и с окончаниями («пятницу»), и с искажениями распознавания; фразу из нескольких слов («эй, Джарвис») говорите целиком, в начале команды
          </>
        }
        wide
      >
        <ChipsEditor
          value={v.wake_words}
          min={1}
          max={8}
          placeholder="Добавить слово…"
          onChange={(words) => patch({ voice: { wake_words: words } })}
        />
      </Setting>
      <Setting name="Режим префикса" desc={`Команды — только после слова «${cap(v.wake_words[0] ?? "джарвис")}»`}>
        <Toggle checked={v.prefix_mode} onChange={(on) => patch({ voice: { prefix_mode: on } })} label="Режим префикса" />
      </Setting>
      <Setting name="Продолжение диалога" desc="Сколько слушать следующую фразу без активатора после ответа" wide>
        <Slider
          value={v.follow_up_s}
          min={0}
          max={30}
          format={(x) => (x === 0 ? "выкл" : `${Math.round(x)} с`)}
          onCommit={(x) => patch({ voice: { follow_up_s: Math.round(x) } })}
        />
      </Setting>
      <Setting
        name="«Что-нибудь ещё?»"
        desc="После ответа голосом Jarvis спрашивает и ещё слушает без активатора; «нет» или «всё» — перестаёт"
      >
        <Toggle
          checked={v.ask_more ?? true}
          disabled={v.follow_up_s === 0}
          onChange={(on) => patch({ voice: { ask_more: on } })}
          label="Спрашивать «Что-нибудь ещё?»"
        />
      </Setting>
      <Setting
        name="Клавиша «держи и говори»"
        desc={
          pttError ? (
            <span className="err-text" title={pttError}>
              {pttError}
            </span>
          ) : (
            "Держите — Jarvis слушает без активатора, отпустили — выполняет"
          )
        }
      >
        <KeyCapture
          mode="key"
          clearable
          emptyText="Выключено"
          value={v.ptt_key}
          onChange={(k) => patch({ voice: { ptt_key: k } })}
        />
      </Setting>
      <Setting name="Самая длинная фраза" desc="Дольше — фраза обрывается" wide>
        <Slider
          value={v.max_utterance_s}
          min={3}
          max={60}
          format={(x) => `${Math.round(x)} с`}
          onCommit={(x) => patch({ voice: { max_utterance_s: Math.round(x) } })}
        />
      </Setting>
    </Card>
  );
}

/** Приветствие по возвращении (ядро: voice.presence_greeting, voice.presence_away_min). */
const GREETING_OPTIONS: { value: Settings["voice"]["presence_greeting"]; label: string }[] = [
  { value: "off", label: "Выключено" },
  { value: "news", label: "Если есть напоминания" },
  { value: "always", label: "Всегда" },
];

function RepliesCard() {
  const [settings, patch] = useSettingsPatch();
  if (!settings) return null;
  const v = settings.voice;
  const greeting = v.presence_greeting ?? "off";
  return (
    <Card icon={<MessageSquare />} title="Ответы" sub="Звуки и короткие ответы голосом">
      <Setting name="Звуковые сигналы" desc="Короткие звуки «слушаю», «готово», «ошибка»">
        <Toggle checked={v.earcons} onChange={(on) => patch({ voice: { earcons: on } })} label="Звуковые сигналы" />
      </Setting>
      <Setting name="Озвучивать итог команд" desc={`Например: «Готово, ${settings.profile.address || "сэр"}»`}>
        <Select
          value={v.speak_confirmations}
          width={200}
          options={[
            { value: "short", label: "Коротко" },
            { value: "off", label: "Не озвучивать" },
          ]}
          onChange={(x) => patch({ voice: { speak_confirmations: x } })}
        />
      </Setting>
      <Setting name="Подтверждения голосом" desc="Отвечайте «да» или «нет», когда Jarvis спрашивает разрешение">
        <Toggle checked={v.confirm_by_voice} onChange={(on) => patch({ voice: { confirm_by_voice: on } })} label="Подтверждения голосом" />
      </Setting>
      <Setting name="Тихий режим" desc="Jarvis выполняет команды молча, без ответов голосом">
        <Toggle checked={v.silent_mode} onChange={(on) => patch({ voice: { silent_mode: on } })} label="Тихий режим" />
      </Setting>
      <Setting
        name="Приветствие при возвращении"
        desc="Вернулись к компьютеру — Jarvis поздоровается и напомнит о делах. Молчит над играми, в звонках и в тихом режиме"
      >
        <Select
          value={greeting}
          width={230}
          options={GREETING_OPTIONS}
          onChange={(x) => patch({ voice: { presence_greeting: x } })}
        />
      </Setting>
      {greeting !== "off" && (
        <Setting name="Вас не было" desc="Столько минут без мыши и клавиатуры — значит, вы отходили" wide>
          <Slider
            value={v.presence_away_min ?? 20}
            min={5}
            max={240}
            step={5}
            format={(x) => `${Math.round(x)} мин`}
            onCommit={(x) => patch({ voice: { presence_away_min: Math.round(x) } })}
          />
        </Setting>
      )}
    </Card>
  );
}

function ModelsCard() {
  const models = useVoiceModels();
  const cloud = useApp((s) => s.settings?.voice.asr_model === "elevenlabs");
  const list = models.items.filter((m) => m.kind !== "tts");
  const missing = list.filter((m: ModelItem) => m.required && !m.installed && !m.downloading);
  const totalMb = missing.reduce((s, m) => s + m.size_mb, 0);
  return (
    <Card
      icon={<Package />}
      title="Модели"
      sub={
        cloud
          ? "«Джарвис» и паузы Jarvis слышит на ПК — эти модели скачиваются один раз"
          : "Распознавание работает полностью на вашем ПК — модели скачиваются один раз"
      }
      actions={
        missing.length > 0 && (
          <Button size="sm" variant="primary" icon={<Download />} onClick={() => missing.forEach((m) => void models.download(m.id))}>
            Скачать нужные · {fmtMb(totalMb)}
          </Button>
        )
      }
    >
      {models.missing ? (
        <Note>Список моделей появится, когда голосовой модуль ядра будет готов.</Note>
      ) : models.error && !models.data ? (
        <Note tone="err">
          Не удалось получить список моделей: {models.error.message}{" "}
          <button type="button" className="link-btn" onClick={() => void models.reload()}>
            Повторить
          </button>
        </Note>
      ) : !models.data ? (
        <div className="row faint small" style={{ padding: 10 }}>
          <span className="spinner" /> Загружаю…
        </div>
      ) : (
        <div className="model-list">
          {list.map((m) => (
            <ModelRow
              key={m.id}
              model={m}
              icon={MODEL_ICONS[m.kind]}
              onDownload={() => void models.download(m.id)}
              onRemove={() => void models.remove(m.id)}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

export default function VoiceSection() {
  return (
    <div className="stack">
      <VoiceHero />
      <MicrophoneCard />
      <WakeCard />
      <RepliesCard />
      <ModelsCard />
    </div>
  );
}
