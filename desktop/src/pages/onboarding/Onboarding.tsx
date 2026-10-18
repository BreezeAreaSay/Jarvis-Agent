// Мастер первого запуска поверх большой сферы: знакомство → микрофон и модели → голос → ИИ → готово.
import "@/styles/settings.css";
import {
  ArrowLeft,
  ArrowRight,
  AudioLines,
  Check,
  CloudSun,
  Download,
  Keyboard,
  Mic,
  MicOff,
  MonitorSmartphone,
  Music,
  Package,
  Search,
  Sparkles,
  Volume2,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { onEvent } from "@/api/events";
import { Orb } from "@/components/Orb";
import { useApp } from "@/store/app";
import { Button, cx, notifyError, Select, useDebounced } from "@/ui";
import { AiConnection, ChatGptPanel, ProviderCards, useAiConfigured } from "../settings/ai";
import {
  cap,
  fmtMb,
  keyParts,
  MicMeter,
  ModelRow,
  Note,
  useDevices,
  useSettingsPatch,
  useVoiceModels,
  useVoiceStatus,
  voiceStateText,
  Waveform,
} from "../settings/kit";
import { TtsEngines } from "../settings/tts";
import { AddressPicker, addressMode } from "../profile/ProfilePage";

const STEPS = [
  { id: "hello", label: "Знакомство" },
  { id: "mic", label: "Микрофон" },
  { id: "voice", label: "Голос" },
  { id: "ai", label: "ИИ" },
  { id: "done", label: "Готово" },
] as const;

function Stepper({ step, onPick }: { step: number; onPick: (i: number) => void }) {
  return (
    <div className="ob-steps" role="tablist" aria-label="Шаги">
      {STEPS.map((s, i) => (
        <button
          key={s.id}
          type="button"
          role="tab"
          aria-selected={i === step}
          className={cx("ob-step", i === step && "active", i < step && "done")}
          onClick={() => onPick(i)}
        >
          <span className="n">{i < step ? <Check strokeWidth={3} /> : i + 1}</span>
          {s.label}
        </button>
      ))}
    </div>
  );
}

// --- 1. знакомство ---------------------------------------------------------------------------------------
function HelloStep() {
  const [settings, patch] = useSettingsPatch();
  const saved = settings?.profile;
  const [name, setName] = useState(saved?.name ?? "");
  const [city, setCity] = useState(saved?.city ?? "");
  const saveName = useDebounced((v: string) => {
    const p = useApp.getState().settings?.profile;
    const byName = p ? addressMode(p.address, p.name) === "name" : false;
    patch({ profile: { name: v.trim(), ...(byName && v.trim() ? { address: v.trim() } : {}) } });
  }, 500);
  const saveCity = useDebounced((v: string) => patch({ profile: { city: v.trim() } }), 600);
  if (!saved) return null;
  return (
    <>
      <div className="ob-section-title">Как вас зовут?</div>
      <input
        className="input ob-name"
        value={name}
        placeholder="Ваше имя"
        autoFocus
        spellCheck={false}
        onChange={(e) => {
          setName(e.target.value);
          saveName(e.target.value);
        }}
      />
      <div className="ob-section-title">Как мне к вам обращаться?</div>
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
        <AddressPicker align="start" address={saved.address} name={name} onChange={(address) => patch({ profile: { address } })} />
        <span className="faint small">«Готово, {saved.address || "сэр"}»</span>
      </div>
      <div className="ob-section-title">Ваш город — для погоды</div>
      <input
        className="input"
        value={city}
        placeholder="Москва"
        spellCheck={false}
        onChange={(e) => {
          setCity(e.target.value);
          saveCity(e.target.value);
        }}
      />
    </>
  );
}

// --- 2. микрофон и модели -------------------------------------------------------------------------------
function WakeTest({ ready }: { ready: boolean }) {
  const settings = useApp((s) => s.settings);
  const state = useApp((s) => s.voiceState);
  const reason = useApp((s) => s.voiceReason);
  const [, patch] = useSettingsPatch();
  const [heard, setHeard] = useState<{ text: string; ok: boolean } | null>(null);
  const [partial, setPartial] = useState("");
  useEffect(() => {
    const offs = [
      onEvent("voice.wake", () => setHeard((h) => ({ text: h?.text ?? "", ok: true }))),
      onEvent<{ text: string; wake: boolean }>("voice.final", (e) => {
        setPartial("");
        if (e.data.wake) setHeard({ text: e.data.text, ok: true });
        else setHeard((h) => (h?.ok ? h : { text: e.data.text, ok: false }));
      }),
      onEvent<{ text: string }>("voice.partial", (e) => setPartial(e.data.text)),
    ];
    return () => offs.forEach((off) => off());
  }, []);
  if (!settings) return null;
  const wake = cap(settings.voice.wake_words[0] ?? "джарвис");
  const enabled = settings.voice.enabled;
  const ok = !!heard?.ok;
  return (
    <div className={cx("wake-test", ok && "ok")}>
      <span className={cx("wake-ico", !ok && enabled && state === "listening" && "mic-live")}>
        {ok ? <Check strokeWidth={3} /> : enabled ? <Mic /> : <MicOff />}
      </span>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="wake-word">{ok ? "Отлично, я вас слышу!" : `Скажите «${wake}»`}</div>
        <div className="wake-heard ellipsis">
          {partial
            ? `«${partial}»`
            : heard?.text
              ? `Услышал: «${heard.text}»`
              : !enabled
                ? "Микрофон выключен"
                : !ready
                  ? "Сначала скачайте модели — это займёт пару минут"
                  : voiceStateText(state, wake, reason)}
        </div>
      </div>
      {!enabled ? (
        <Button size="sm" variant="primary" onClick={() => patch({ voice: { enabled: true } })}>
          Включить микрофон
        </Button>
      ) : (
        <MicMeter segments={14} />
      )}
    </div>
  );
}

function MicStep() {
  const [settings, patch] = useSettingsPatch();
  const devices = useDevices();
  const models = useVoiceModels();
  if (!settings) return null;
  const v = settings.voice;
  const inputs = devices.data?.inputs ?? [];
  const def = inputs.find((d) => d.default);
  const required = models.items.filter((m) => m.required && m.kind !== "tts");
  const missing = required.filter((m) => !m.installed && !m.downloading);
  const ready = models.missing || (models.data ? required.every((m) => m.installed) : false);
  return (
    <>
      <div className="ob-section-title">
        <Mic /> Микрофон
      </div>
      <div className="row" style={{ gap: 14 }}>
        <div className="grow">
          <Select
            value={v.input_device}
            width="100%"
            options={[
              { value: "", label: def ? `По умолчанию — ${def.name}` : "Микрофон по умолчанию" },
              ...inputs.map((d) => ({ value: d.id, label: d.name })),
            ]}
            onChange={(id) => patch({ voice: { input_device: id } })}
          />
        </div>
        <MicMeter segments={16} className={cx(!v.enabled && "off")} />
      </div>

      <div className="ob-section-title">
        <Package /> Модели распознавания
        <span className="spacer" />
        {missing.length > 0 && (
          <Button size="sm" variant="primary" icon={<Download />} onClick={() => missing.forEach((m) => void models.download(m.id))}>
            Скачать всё · {fmtMb(missing.reduce((s, m) => s + m.size_mb, 0))}
          </Button>
        )}
      </div>
      {models.missing ? (
        <Note>Модели можно будет скачать в настройках, когда голосовой модуль будет готов.</Note>
      ) : !models.data ? (
        <div className="row faint small">
          <span className="spinner" /> Проверяю модели…
        </div>
      ) : (
        <div className="model-list">
          {required.map((m) => (
            <ModelRow key={m.id} model={m} compact onDownload={() => void models.download(m.id)} />
          ))}
        </div>
      )}

      <div className="ob-section-title">
        <AudioLines /> Проверка
      </div>
      <WakeTest ready={ready} />
    </>
  );
}

// --- 5. готово -----------------------------------------------------------------------------------------
const TIPS: { icon: ReactNode; text: string }[] = [
  { icon: <Music />, text: "включи музыку" },
  { icon: <Volume2 />, text: "громкость на пятьдесят" },
  { icon: <Search />, text: "найди рецепт пасты" },
  { icon: <CloudSun />, text: "какая погода завтра?" },
  { icon: <MonitorSmartphone />, text: "сверни все окна" },
  { icon: <Sparkles />, text: "открой стол «Работа»" },
];

function DoneStep() {
  const settings = useApp((s) => s.settings);
  const aiConfigured = useAiConfigured();
  const status = useVoiceStatus();
  if (!settings) return null;
  const voiceReady = settings.voice.enabled && (status.data?.models_ready ?? true);
  const wake = cap(settings.voice.wake_words[0] ?? "джарвис");
  const listen = keyParts(settings.system.hotkey_listen, "accelerator");
  return (
    <>
      <div className="ob-done-chips">
        <span className={cx("chip", voiceReady ? "ok" : "")}>
          {voiceReady ? <Check /> : <Mic />} Голос:{" "}
          {!settings.voice.enabled ? "выключен" : voiceReady ? "включён" : "скачайте модели в настройках"}
        </span>
        <span className={cx("chip", aiConfigured ? "ok" : "")}>
          <Sparkles /> Jarvis AI: {aiConfigured ? "подключён" : "только команды"}
        </span>
      </div>
      <div className="tips">
        {TIPS.map((t) => (
          <div key={t.text} className="tip">
            <span className="tip-ico">{t.icon}</span>
            <span>
              <b>{wake},</b> {t.text}
            </span>
          </div>
        ))}
      </div>
      {listen.length > 0 && (
        <div className="row small muted" style={{ marginTop: 14, gap: 8, justifyContent: "center", flexWrap: "wrap" }}>
          <Keyboard style={{ width: 15, height: 15 }} /> Сказать без «{wake}» —
          {listen.map((k, i) => (
            <span key={i} className="row" style={{ gap: 4 }}>
              {i > 0 && "+"}
              <span className="kbd">{k}</span>
            </span>
          ))}
        </div>
      )}
    </>
  );
}

// --- мастер ---------------------------------------------------------------------------------------------
export default function Onboarding() {
  const settings = useApp((s) => s.settings);
  const patchSettings = useApp((s) => s.patchSettings);
  const [step, setStep] = useState(0);
  const [finishing, setFinishing] = useState(false);
  if (!settings) return null;
  const address = settings.profile.address;
  const name = settings.profile.name.trim();

  const finish = async () => {
    setFinishing(true);
    try {
      await patchSettings({ onboarding_done: true });
    } catch (e) {
      notifyError(e);
      setFinishing(false);
    }
  };

  const heads: { eyebrow: string; title: string; text?: string; extra?: ReactNode }[] = [
    {
      eyebrow: "Знакомство",
      title: `Привет! Я ${settings.profile.assistant_name || "Джарвис"}`,
      text: "Ваш голосовой помощник для Windows: команды голосом, ответы на любые вопросы и пульт в телефоне. Настроим всё за минуту.",
    },
    {
      eyebrow: "Распознавание речи",
      title: "Приватно и быстро",
      text: "Речь распознаётся прямо на вашем ПК, без интернета. Выберите микрофон и скачайте модели.",
    },
    { eyebrow: "Синтез речи", title: "Выберите голос", extra: <Waveform bars={46} height={44} /> },
    {
      eyebrow: "Jarvis AI",
      title: "Подключите Jarvis AI",
      text: "Отвечает на вопросы, ищет в интернете и сам выполняет сложные задачи. Без него работают только команды.",
    },
    {
      eyebrow: "Всё готово",
      title: `Готово${address ? `, ${address}` : name ? `, ${name}` : ""}!`,
      text: "Вот с чего можно начать — просто скажите:",
    },
  ];
  const head = heads[step];
  const hero = step === 0 || step === 4;
  const content = [<HelloStep key="hello" />, <MicStep key="mic" />, <TtsEngines key="voice" compact />, null, <DoneStep key="done" />][step];

  return (
    <div className={cx("page ob-page set-scope", !hero && "focus")}>
      <div className="ob-orb" aria-hidden>
        <Orb scale={0.42} reactivity={1.3} logo={hero} />
      </div>
      <div className="ob-scroll">
        <Stepper step={step} onPick={setStep} />
        <div className={cx("ob-gap", hero && "hero")} />
        <div className="ob-head fade-in" key={`h${step}`}>
          <div className="eyebrow">{head.eyebrow}</div>
          <h1 className="h-hero">{head.title}</h1>
          {head.text && <p>{head.text}</p>}
          {head.extra && <div className="ob-extra">{head.extra}</div>}
        </div>
        <div className={cx("ob-card fade-in", step === 3 && "wide")} key={`c${step}`}>
          {step === 3 ? (
            <>
              <ProviderCards compact />
              {settings.ai.provider === "chatgpt" && (
                <div style={{ marginTop: 12 }}>
                  <ChatGptPanel compact />
                </div>
              )}
              <div style={{ marginTop: 12 }}>
                <AiConnection compact />
              </div>
            </>
          ) : (
            content
          )}
        </div>
        <div className="ob-foot">
          {step === 0 ? (
            <Button variant="ghost" onClick={finish} disabled={finishing}>
              Пропустить знакомство
            </Button>
          ) : (
            <Button variant="ghost" icon={<ArrowLeft />} onClick={() => setStep(step - 1)}>
              Назад
            </Button>
          )}
          <span className="spacer" />
          {step === 3 && (
            <Button variant="ghost" onClick={() => setStep(4)}>
              Пропустить — только команды
            </Button>
          )}
          {step < 4 ? (
            <Button variant="primary" size="lg" onClick={() => setStep(step + 1)}>
              Далее <ArrowRight />
            </Button>
          ) : (
            <Button variant="primary" size="lg" loading={finishing} onClick={finish}>
              Начать работу <ArrowRight />
            </Button>
          )}
        </div>
        <div className="ob-gap bottom" />
      </div>
    </div>
  );
}
