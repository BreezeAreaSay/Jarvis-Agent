// «Приватность и данные»: честно — что остаётся на ПК и что уходит провайдеру ИИ; сроки хранения; что хранится
// и сколько места занимает (с очисткой по категориям и «Удалить все данные»); журналы ядра и интерфейса.
import {
  AudioLines,
  BellRing,
  Boxes,
  Brain,
  CalendarDays,
  CircleDollarSign,
  ClipboardList,
  Cloud,
  Database,
  FolderOpen,
  FolderSearch,
  HardDrive,
  History,
  Image,
  Palette,
  KeyRound,
  ListChecks,
  MessagesSquare,
  Mic,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  StickyNote,
  Timer,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { Settings } from "@/api/types";
import { bridge, isElectron } from "@/lib/bridge";
import { Button, Card, Check, Modal, notify, notifyError, type Option, Select, Setting, TextInput, timeAgo, Toggle } from "@/ui";
import { FALLBACK_PROVIDERS, providerTitle } from "../ai";
import { Note, useApi, useSettingsPatch } from "../kit";
import { fileUrl } from "./SystemSection";

interface DataItem {
  id: string;
  title: string;
  hint: string;
  count: number;
  bytes: number;
  clearable: boolean;
}

interface Inventory {
  dir: string;
  logs_dir: string;
  db_bytes: number;
  purged_at: number | null;
  items: DataItem[];
}

interface LogFile {
  name: string;
  size: number;
  modified: number;
}

type Privacy = Settings["privacy"];

const ICONS: Record<string, ReactNode> = {
  history: <History />,
  chats: <MessagesSquare />,
  voice_chats: <Mic />,
  images: <Palette />,
  memory: <Brain />,
  notes: <StickyNote />,
  tasks: <ListChecks />,
  events: <CalendarDays />,
  reminders: <BellRing />,
  clipboard: <ClipboardList />,
  summaries: <AudioLines />,
  screenshots: <Image />,
  file_index: <FolderSearch />,
  voice_cache: <AudioLines />,
  usage: <CircleDollarSign />,
  logs: <ScrollText />,
  secrets: <KeyRound />,
  models: <Boxes />,
};

/** Сколько и чего: «12 записей». */
const UNITS: Record<string, [string, string, string]> = {
  chats: ["чат", "чата", "чатов"],
  voice_chats: ["чат", "чата", "чатов"],
  images: ["картинка", "картинки", "картинок"],
  summaries: ["запись", "записи", "записей"],
  screenshots: ["снимок", "снимка", "снимков"],
  file_index: ["файл", "файла", "файлов"],
  voice_cache: ["фраза", "фразы", "фраз"],
  logs: ["файл", "файла", "файлов"],
  secrets: ["ключ", "ключа", "ключей"],
  models: ["модель", "модели", "моделей"],
};

function plural(n: number, [one, few, many]: [string, string, string]): string {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

export function fmtBytes(bytes: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} МБ`;
  return `${(bytes / 1024 ** 3).toFixed(1).replace(".", ",")} ГБ`;
}

function amount(item: DataItem): string {
  if (!item.count) return "Пусто";
  const unit = UNITS[item.id] ?? ["запись", "записи", "записей"];
  const size = fmtBytes(item.bytes);
  return `${item.count.toLocaleString("ru-RU")} ${plural(item.count, unit)}${size ? ` · ${size}` : ""}`;
}

// --- что куда уходит --------------------------------------------------------------------------------------

const CLOUD_TTS: Record<string, string> = {
  edge: "Microsoft Edge",
  openai: "OpenAI",
  fish: "Fish Audio",
  elevenlabs: "ElevenLabs",
};

function FlowCard({ settings }: { settings: Settings }) {
  const provider = FALLBACK_PROVIDERS.find((p) => p.id === settings.ai.provider);
  const custom = settings.ai.provider === "custom";
  const localUrl = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/i.test(settings.ai.base_url);
  const local = provider?.local || (custom && localUrl);
  const name = provider ? providerTitle(provider, true) : custom ? "ваш сервер" : settings.ai.provider;
  const cloudTts = CLOUD_TTS[settings.tts.engine];
  const cloudAsr = settings.voice.asr_model === "elevenlabs";
  return (
    <Card icon={<ShieldCheck />} title="Что куда уходит" sub="Коротко и честно: что остаётся на компьютере, а что отправляется в сеть">
      <div className="priv-flow">
        <div className="priv-col">
          <div className="priv-col-head">
            <HardDrive /> Остаётся на этом компьютере
          </div>
          <ul>
            <li>
              {cloudAsr
                ? "Слово-активатор и чужие разговоры: этот звук никуда не отправляется"
                : "Распознавание речи и слово-активатор: звук микрофона никуда не отправляется"}
            </li>
            <li>Настройки, история команд, заметки, задачи, календарь, память — в папке данных Jarvis</li>
            <li>Ключи API и токены — зашифрованы средствами Windows (DPAPI)</li>
            <li>Записи созвонов для «Конспекта» — удаляются после конспекта (или по сроку ниже)</li>
            {!cloudTts && <li>Озвучка ответов — голос синтезируется на ПК</li>}
          </ul>
        </div>
        <div className="priv-col">
          <div className="priv-col-head">
            <Cloud /> {local ? `Jarvis AI (${name}) — тоже на этом ПК` : `Провайдеру ИИ (${name}), когда вы обращаетесь к Jarvis AI`}
          </div>
          <ul>
            <li>Ваше сообщение и предыдущая переписка в этом чате, приложенные картинки</li>
            <li>Память о вас, имя, город и обращение из профиля, ваши инструкции для ИИ</li>
            <li>Результаты инструментов: найденные страницы, погода, заметки и задачи, которые ИИ прочитал</li>
            <li>Пока работает агент — список кнопок и полей окна, а со «зрением» — снимок этого окна</li>
            <li>Расшифровка созвона для «Конспекта» и выделенный текст, когда вы просите ИИ его обработать</li>
          </ul>
          {!local && (
            <div className="faint small">
              {provider?.group === "account"
                ? "Через ваш аккаунт — по правилам этого сервиса для подписки."
                : "Хранение и использование — по правилам провайдера, у которого ваш ключ."}{" "}
              Чтобы ничего не уходило в сеть, выберите ИИ «на этом ПК» (Ollama или LM Studio).
            </div>
          )}
        </div>
      </div>
      {cloudAsr && (
        <Note icon={<Cloud />}>
          Распознавание — ElevenLabs: звук фраз, обращённых к Jarvis (после «Джарвис», с клавишей «держи и говори», в
          окне продолжения), отправляется этому сервису. Чтобы звук не уходил в сеть, выберите в настройках голоса
          модель GigaAM или Vosk.
        </Note>
      )}
      {cloudTts && (
        <Note icon={<Cloud />}>
          Озвучка ответов — облачный голос {cloudTts}: текст, который Jarvis произносит, отправляется этому сервису.
          Голос Джарвиса и голос Windows работают на ПК.
        </Note>
      )}
      <Note icon={<ShieldCheck />}>
        Погода, курсы, новости и поиск запрашиваются у этих сервисов напрямую — с городом для погоды и текстом запроса,
        без остальных ваших данных.
      </Note>
      <Note icon={<Cloud />}>
        Картинки рисует OpenAI: когда вы просите нарисовать, туда уходят описание картинки и ваше фото-образец, если вы
        его приложили. Нарисованное хранится в чате на ПК — удалить можно ниже, «Нарисованные картинки».
      </Note>
    </Card>
  );
}

// --- защита от утечек ----------------------------------------------------------------------------------------

function ProtectCard() {
  const [settings, patch] = useSettingsPatch();
  const [apps, setApps] = useState("");
  const saved = settings?.ai.private_apps.join(", ") ?? "";
  useEffect(() => setApps(saved), [saved]);
  if (!settings) return null;
  const commit = (value: string) => {
    const list = value
      .split(/[,;\n]/)
      .map((v) => v.trim())
      .filter(Boolean);
    if (list.join(", ") !== saved) patch({ ai: { private_apps: list } });
  };
  return (
    <Card icon={<KeyRound />} title="Защита от утечек" sub="Что Jarvis не отдаёт облачному ИИ, даже если это попало в текст">
      <Setting
        name="Скрывать пароли и ключи"
        desc="Пароли, ключи API, токены, номера карт и закрытые ключи в сообщениях, заметках, окнах и страницах заменяются на «•••», прежде чем уйти облачному ИИ. Модели на этом ПК видят текст как есть"
      >
        <Toggle checked={settings.ai.mask_secrets} onChange={(v) => patch({ ai: { mask_secrets: v } })} label="Скрывать пароли и ключи" />
      </Setting>
      <Setting
        name="Не показывать ИИ"
        desc="Программы или слова из заголовка окна через запятую (например: telegram, Сбербанк). Такие окна ИИ не видит на снимках, а агент в них не работает. Менеджеры паролей (KeePass, 1Password, Bitwarden…) и окна ввода пароля Windows скрыты всегда"
        wide
      >
        <TextInput value={apps} onChange={setApps} onCommit={commit} placeholder="telegram, Сбербанк" />
      </Setting>
    </Card>
  );
}

// --- сроки хранения ------------------------------------------------------------------------------------------

const DAY_OPTIONS = [0, 1, 3, 7, 14, 30, 90, 180, 365];

function dayOptions(current: number, zero: string): Option<string>[] {
  const values = DAY_OPTIONS.includes(current) ? DAY_OPTIONS : [...DAY_OPTIONS, current].sort((a, b) => a - b);
  return values.map((d) => ({
    value: String(d),
    label: d === 0 ? zero : d === 1 ? "1 день" : `${d} ${plural(d, ["день", "дня", "дней"])}`,
  }));
}

const RETENTION: { key: keyof Privacy; name: string; desc: string; zero?: string }[] = [
  { key: "history_days", name: "История команд", desc: "Что вы говорили и что выполнил Jarvis" },
  {
    key: "chat_days",
    name: "Чаты Jarvis AI",
    desc: "Чат без новых сообщений удаляется целиком. Закреплённые чаты не трогаем",
  },
  { key: "voice_chat_days", name: "Голосовой чат, Telegram и пульт", desc: "Старые сообщения этих чатов" },
  {
    key: "tool_results_days",
    name: "Результаты инструментов и картинки",
    desc: "Что ИИ получал от инструментов и приложенные картинки в старых сообщениях. Текст переписки остаётся",
  },
  {
    key: "audio_days",
    name: "Записи созвонов",
    desc: "Звук «Конспекта», который не удалился сам (например, конспект не получился)",
  },
  { key: "logs_days", name: "Журналы", desc: "Старые журналы ядра; большие обрезаются до последнего мегабайта" },
];

function RetentionCard({ privacy, purgedAt, onPurged }: { privacy: Privacy; purgedAt: number | null; onPurged: () => void }) {
  const [, patch] = useSettingsPatch();
  const [busy, setBusy] = useState(false);
  const purge = async () => {
    setBusy(true);
    try {
      const res = await api.post<{ removed: Record<string, number> }>("/api/data/purge");
      const total = Object.values(res?.removed ?? {}).reduce((a, b) => a + b, 0);
      notify(total ? `Удалено по срокам: ${total}` : "Удалять нечего — всё в пределах сроков", "ok");
      onPurged();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      icon={<Timer />}
      title="Сроки хранения"
      sub={`Jarvis сам удаляет старое раз в сутки${purgedAt ? ` · последний раз ${timeAgo(purgedAt)}` : ""}`}
      actions={
        <Button size="sm" variant="ghost" icon={<RefreshCw />} loading={busy} onClick={purge}>
          Очистить по срокам сейчас
        </Button>
      }
    >
      {RETENTION.map((r) => (
        <Setting key={r.key} name={r.name} desc={r.desc}>
          <Select
            value={String(privacy[r.key])}
            options={dayOptions(privacy[r.key], r.zero ?? "Хранить всегда")}
            onChange={(v) => patch({ privacy: { [r.key]: Number(v) } })}
            width={170}
          />
        </Setting>
      ))}
    </Card>
  );
}

// --- что хранится ---------------------------------------------------------------------------------------------

function DataRow({ item, onClear }: { item: DataItem; onClear: (id: string) => Promise<void> }) {
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const clear = async () => {
    setAsk(false);
    setBusy(true);
    try {
      await onClear(item.id);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="model-row compact priv-row">
      <div className="model-ico">{ICONS[item.id] ?? <Database />}</div>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="model-title">{item.title}</div>
        <div className="model-desc">{item.hint}</div>
      </div>
      <span className="model-size">{amount(item)}</span>
      {item.clearable &&
        (ask ? (
          <>
            <Button size="sm" variant="danger" onClick={clear}>
              Удалить
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAsk(false)}>
              Отмена
            </Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" icon={<Trash2 />} loading={busy} disabled={!item.count} onClick={() => setAsk(true)}>
            Очистить
          </Button>
        ))}
    </div>
  );
}

function WipeModal({ open, items, onClose, onDone }: { open: boolean; items: DataItem[]; onClose: () => void; onDone: () => void }) {
  const [keys, setKeys] = useState(false);
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) {
      setKeys(false);
      setSure(false);
    }
  }, [open]);
  const scopes = items.filter((i) => i.clearable && (keys || i.id !== "secrets")).map((i) => i.id);
  const wipe = async () => {
    setBusy(true);
    try {
      await api.post("/api/data/clear", { scopes });
      notify("Данные Jarvis удалены", "ok");
      onDone();
      onClose();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Удалить все данные?"
      icon={<TriangleAlert />}
      width={500}
      footer={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="danger" icon={<Trash2 />} loading={busy} disabled={!sure} onClick={wipe}>
            Удалить навсегда
          </Button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        Удалятся история команд, все чаты, память, заметки, задачи, календарь, напоминания, история буфера, записи
        созвонов, снимки, индекс поиска, учёт расходов и журналы. Настройки и голосовые модели останутся. Отменить это
        нельзя.
      </p>
      <div className="stack" style={{ gap: 10 }}>
        <Check checked={keys} onChange={setKeys}>
          Удалить и ключи API и токены (ИИ, Telegram, умный дом, пароль пульта)
        </Check>
        <Check checked={sure} onChange={setSure}>
          Понимаю, что удалённое не вернуть
        </Check>
      </div>
    </Modal>
  );
}

function StorageCard({ inv, reload }: { inv: { data?: Inventory; error: unknown; loading: boolean }; reload: () => void }) {
  const [wipe, setWipe] = useState(false);
  const items = inv.data?.items ?? [];
  const clearOne = async (id: string) => {
    try {
      const res = await api.post<{ cleared: Record<string, number> }>("/api/data/clear", { scopes: [id] });
      const n = res?.cleared?.[id] ?? 0;
      notify(n ? `Удалено: ${n}` : "Удалять было нечего", "ok");
      reload();
    } catch (e) {
      notifyError(e);
    }
  };
  return (
    <Card
      icon={<Database />}
      title="Что хранится"
      sub={
        inv.data
          ? `База Jarvis занимает ${fmtBytes(inv.data.db_bytes) || "меньше килобайта"}. Удалённое из базы затирается`
          : inv.error
            ? "Нет связи с ядром"
            : "Считаю…"
      }
      actions={
        <Button size="sm" variant="ghost" icon={<RefreshCw />} loading={inv.loading} onClick={reload}>
          Обновить
        </Button>
      }
    >
      <div className="model-list">
        {items.map((item) => (
          <DataRow key={item.id} item={item} onClear={clearOne} />
        ))}
      </div>
      <div className="priv-wipe">
        <div className="grow">
          <div className="name">Удалить все данные</div>
          <div className="faint small">Всё личное из списка выше одним действием — например, перед тем как отдать компьютер</div>
        </div>
        <Button variant="danger" icon={<Trash2 />} disabled={!items.length} onClick={() => setWipe(true)}>
          Удалить всё…
        </Button>
      </div>
      <WipeModal open={wipe} items={items} onClose={() => setWipe(false)} onDone={reload} />
    </Card>
  );
}

// --- журналы ---------------------------------------------------------------------------------------------------

function LogsCard({ dir }: { dir: string }) {
  const logs = useApi<{ dir: string; items: LogFile[] }>("/api/logs");
  const [ask, setAsk] = useState(false);
  const files = logs.data?.items ?? [];
  const total = files.reduce((a, f) => a + f.size, 0);
  const clear = async () => {
    setAsk(false);
    try {
      await api.del("/api/logs");
      notify("Журналы ядра очищены", "ok");
      void logs.reload();
    } catch (e) {
      notifyError(e);
    }
  };
  const folder = logs.data?.dir || dir;
  return (
    <Card icon={<ScrollText />} title="Журналы" sub="Помогают найти причину сбоя. Секреты (ключи, пароли, номера карт) в журнал не пишутся">
      <Setting
        name="Журналы ядра"
        desc={files.length ? `${files.length} ${plural(files.length, ["файл", "файла", "файлов"])} · ${fmtBytes(total) || "пусто"}` : "Пусто"}
      >
        <Button size="sm" icon={<FolderOpen />} disabled={!folder} onClick={() => bridge.openExternal(fileUrl(folder))}>
          Открыть папку
        </Button>
        {ask ? (
          <>
            <Button size="sm" variant="danger" onClick={clear}>
              Да, очистить
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAsk(false)}>
              Отмена
            </Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" icon={<Trash2 />} disabled={!total} onClick={() => setAsk(true)}>
            Очистить
          </Button>
        )}
      </Setting>
      {isElectron && (
        <Setting name="Журналы интерфейса" desc="Ошибки окон и обновлений (ui.log, updater.log)">
          <Button size="sm" icon={<FolderOpen />} onClick={() => bridge.send("logs:open")}>
            Открыть папку
          </Button>
        </Setting>
      )}
    </Card>
  );
}

export default function PrivacySection() {
  const [settings] = useSettingsPatch();
  const inv = useApi<Inventory>("/api/data/inventory");
  const { reload } = inv;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(() => void reload(), 300);
    };
    const offs = ["data.purged", "data.cleared", "history.cleared", "memory.cleared", "chat.deleted"].map((e) => onEvent(e, soon));
    return () => {
      clearTimeout(timer);
      for (const off of offs) off();
    };
  }, [reload]);
  if (!settings) return null;
  return (
    <div className="stack">
      <FlowCard settings={settings} />
      <ProtectCard />
      <RetentionCard privacy={settings.privacy} purgedAt={inv.data?.purged_at ?? null} onPurged={() => void reload()} />
      <StorageCard inv={inv} reload={() => void reload()} />
      <LogsCard dir={inv.data?.logs_dir ?? ""} />
    </div>
  );
}
