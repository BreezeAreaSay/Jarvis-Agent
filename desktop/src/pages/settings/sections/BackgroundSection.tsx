// «Фоновые задачи» в настройках: хранилище заметок (Obsidian), как самостоятельно работает агент, уведомления
// о вопросе и итоге, браузер агента. Ядро — settings.background, /api/background/vault, /api/agent-browser.
import { AppWindow, Bell, BookOpen, FolderOpen, Globe, Telescope } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { NotifyChannel, Settings } from "@/api/types";
import { navigate } from "@/lib/router";
import { Button, Card, Check, cx, notifyError, Select, Setting, Slider, Tabs, TextInput, Toggle } from "@/ui";
import { useApi, useSettingsPatch } from "../kit";

interface VaultState {
  path: string;
  folder: string;
  exists: boolean;
  obsidian: boolean;
  auto: boolean;
  vaults: { path: string; name: string; open: boolean }[];
}

interface AgentBrowserState {
  status: string;
  app: string;
  found: boolean;
  version: string;
  error: string;
  tasks: Record<string, string>;
  windowed?: boolean;
}

const AUTONOMY: Record<Settings["background"]["autonomy"], string> = {
  careful: "Спрашивает, когда хоть немного неясно, и перед любым действием, которое что-то меняет на сайте",
  normal: "Спрашивает, когда неясно, что нужно, и перед необратимым: оплата, покупка, отправка, публикация, удаление",
  bold: "Решает сам; спрашивает только перед деньгами и публикацией от вашего имени",
};

const CHANNELS: { value: NotifyChannel; label: string }[] = [
  { value: "toast", label: "Уведомление" },
  { value: "voice", label: "Голосом" },
  { value: "telegram", label: "В Telegram" },
];

function Channels({ value, onChange }: { value: NotifyChannel[]; onChange: (v: NotifyChannel[]) => void }) {
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      {CHANNELS.map((c) => (
        <Check
          key={c.value}
          chip
          checked={value.includes(c.value)}
          onChange={(on) => onChange(on ? [...value, c.value] : value.filter((v) => v !== c.value))}
        >
          {c.label}
        </Check>
      ))}
    </div>
  );
}

function VaultCard() {
  const [settings, patch] = useSettingsPatch();
  const q = useApi<VaultState>("/api/background/vault");
  const [custom, setCustom] = useState("");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const bg = settings?.background;
  useEffect(() => setCustom(bg?.vault ?? ""), [bg?.vault]);
  useEffect(() => setFolder(bg?.folder ?? ""), [bg?.folder]);
  useEffect(() => {
    void q.reload();
  }, [bg?.vault, bg?.folder]);
  if (!bg || q.missing) return null;
  const state = q.data;
  const setup = async () => {
    setBusy(true);
    try {
      q.setData(await api.post<VaultState>("/api/background/vault/setup", {}));
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  const options = [
    { value: "", label: "Найти самому" },
    ...(state?.vaults ?? []).map((v) => ({ value: v.path, label: v.name, hint: v.open ? "открыто" : undefined })),
  ];
  let where: string;
  if (!state) where = "Ищу хранилище…";
  else if (state.obsidian) where = `Хранилище Obsidian «${state.path.split(/[\\/]/).pop()}» — ${state.path}`;
  else if (state.auto && !state.vaults.length)
    where = `Obsidian не найден — заметки пойдут в папку ${state.path} (её можно открыть в Obsidian как хранилище)`;
  else where = `Папка с заметками: ${state.path}`;
  return (
    <Card
      icon={<BookOpen />}
      title="Хранилище заметок"
      sub="Итоги задач, исследования и замеры агент пишет в заметки Markdown — Obsidian подхватывает их сам"
    >
      <Setting name={<span className={cx("chip", state?.exists && "ok")}>{state?.exists ? "Найдено" : "Будет создано"}</span>} desc={where}>
        <Button size="sm" variant="soft" icon={<FolderOpen />} loading={busy} onClick={setup}>
          Создать папку {bg.folder || "Jarvis"}
        </Button>
      </Setting>
      <Setting name="Хранилище" desc="Какое хранилище Obsidian использовать, или своя папка">
        <Select
          value={state?.vaults.some((v) => v.path === bg.vault) || !bg.vault ? bg.vault : ""}
          options={options}
          onChange={(v) => patch({ background: { vault: v } })}
          width={240}
        />
      </Setting>
      <Setting name="Своя папка" desc="Полный путь к папке с заметками (пусто — как выбрано выше)" wide>
        <TextInput
          value={custom}
          onChange={setCustom}
          onCommit={(v) => v !== bg.vault && patch({ background: { vault: v.trim() } })}
          placeholder="C:\Users\…\Documents\Obsidian\Мои заметки"
        />
      </Setting>
      <Setting name="Папка агента" desc="Агент пишет только в неё: Задачи, Исследования, Замеры, Журнал, Инструкции">
        <TextInput
          value={folder}
          onChange={setFolder}
          onCommit={(v) => v.trim() && v.trim() !== bg.folder && patch({ background: { folder: v.trim() } })}
          placeholder="Jarvis"
        />
      </Setting>
      <Setting name="Читать все заметки" desc="Агент ищет и читает ваши заметки, чтобы пользоваться ими в задачах (писать — только в свою папку)">
        <Toggle checked={bg.read_vault} onChange={(v) => patch({ background: { read_vault: v } })} label="Читать все заметки" />
      </Setting>
      <Setting
        name="Задачи из «Входящих»"
        desc={`Строки, которые вы запишете в ${bg.folder || "Jarvis"}/Входящие.md (можно с телефона, через синхронизацию Obsidian), Jarvis возьмёт в работу и отметит галочкой`}
      >
        <Toggle checked={bg.inbox} onChange={(v) => patch({ background: { inbox: v } })} label="Задачи из «Входящих»" />
      </Setting>
    </Card>
  );
}

function AgentCard() {
  const [settings, patch] = useSettingsPatch();
  const bg = settings?.background;
  if (!bg) return null;
  return (
    <Card icon={<Telescope />} title="Как работает агент" sub="Большие задачи в фоне: поиск, сайты, отчёты — без вашего экрана">
      <Setting name="Самостоятельность" desc={AUTONOMY[bg.autonomy]} wide>
        <Tabs
          value={bg.autonomy}
          items={[
            { value: "careful", label: "Спрашивать чаще" },
            { value: "normal", label: "Обычно" },
            { value: "bold", label: "Спрашивать реже" },
          ]}
          onChange={(v) => patch({ background: { autonomy: v } })}
        />
      </Setting>
      <Setting
        name="Утверждать план"
        desc="Агент показывает план до начала работы: «Начинай», «Отмени задачу» или правка словами — на странице задачи или в Telegram"
        wide
      >
        <Tabs
          value={bg.approve_plan ?? "big"}
          items={[
            { value: "never", label: "Не спрашивать" },
            { value: "big", label: "Большие задачи" },
            { value: "always", label: "Всегда" },
          ]}
          onChange={(v) => patch({ background: { approve_plan: v } })}
        />
      </Setting>
      <Setting name="Задач одновременно" desc="Остальные ждут очереди">
        <Slider value={bg.parallel} min={1} max={4} step={1} format={(v) => String(v)} onCommit={(v) => patch({ background: { parallel: v } })} />
      </Setting>
      <Setting name="Действий на задачу" desc="Больше — дольше работает, но и дороже по ключу API">
        <Slider value={bg.max_steps} min={20} max={500} step={10} format={(v) => String(v)} onCommit={(v) => patch({ background: { max_steps: v } })} />
      </Setting>
      <Setting name="Часов на задачу" desc="Сколько агент работает над одной задачей (без ожидания вашего ответа)">
        <Slider value={bg.max_hours} min={0.5} max={24} step={0.5} format={(v) => `${v} ч`} onCommit={(v) => patch({ background: { max_hours: v } })} />
      </Setting>
    </Card>
  );
}

function NotifyCard() {
  const [settings, patch] = useSettingsPatch();
  const bg = settings?.background;
  if (!bg) return null;
  return (
    <Card icon={<Bell />} title="Уведомления" sub="Как сообщать, что агенту нужен ваш ответ и что задача закончена">
      <Setting name="Агент спрашивает" desc="В Telegram — с кнопками ответа: ответить можно прямо оттуда. Ничего не выбрано — тихо: вопрос ждёт на странице «Фоновые задачи»">
        <Channels value={bg.notify_question} onChange={(v) => patch({ background: { notify_question: v } })} />
      </Setting>
      <Setting name="Задача готова" desc="Telegram — через бота «Пульт в телефоне», если он подключён">
        <Channels value={bg.notify_done} onChange={(v) => patch({ background: { notify_done: v } })} />
      </Setting>
    </Card>
  );
}

function AgentBrowserCard() {
  const q = useApi<AgentBrowserState>("/api/agent-browser");
  const { data, setData } = q;
  useEffect(() => onEvent<AgentBrowserState>("agent_browser.state", (e) => setData(e.data)), [setData]);
  if (q.missing) return null;
  const running = data?.status === "connected";
  const close = async () => {
    try {
      setData(await api.post<AgentBrowserState>("/api/agent-browser/close", {}));
    } catch (e) {
      notifyError(e);
    }
  };
  const openWindow = async () => {
    try {
      setData(await api.post<AgentBrowserState>("/api/agent-browser/window", {}));
    } catch (e) {
      notifyError(e);
    }
  };
  let hint: string;
  if (data && !data.found) hint = "Не нашёл Google Chrome или Microsoft Edge — установите один из них, и у агента будет свой браузер";
  else if (data?.windowed) hint = "Открыт отдельным окном: войдите там на сайты и закройте окно — задачи продолжат с вашим входом";
  else if (running)
    hint = `${data?.app ?? "Браузер"}${data?.version ? ` ${data.version}` : ""} работает без окна — его видно на странице «Фоновые задачи»`;
  else hint = "Запускается сам, когда задаче понадобится сайт. Войдите в нём в нужные сайты — один раз";
  return (
    <Card icon={<Globe />} title="Браузер агента" sub="Свой Chrome или Edge без окна: агент работает в нём, вы видите его в Jarvis и можете вмешаться">
      <Setting
        name={
          <span className={cx("chip", running && "ok")}>
            <span className={cx("status-dot", running && "ok")} />
            {running ? "Работает" : "Закрыт"}
          </span>
        }
        desc={hint}
      >
        <div className="row">
          <Button size="sm" variant="primary" icon={<Globe />} onClick={() => navigate("background", "browser")}>
            Войти на сайты
          </Button>
          {!data?.windowed && data?.found && (
            <Button size="sm" variant="soft" icon={<AppWindow />} onClick={() => void openWindow()} title="Для сайтов, которые не пускают браузер без окна (например, вход Google)">
              Открыть окном
            </Button>
          )}
          {running && (
            <Button size="sm" variant="ghost" onClick={close}>
              Закрыть
            </Button>
          )}
        </div>
      </Setting>
    </Card>
  );
}

export default function BackgroundSection() {
  return (
    <div className="stack">
      <VaultCard />
      <AgentCard />
      <NotifyCard />
      <AgentBrowserCard />
    </div>
  );
}
