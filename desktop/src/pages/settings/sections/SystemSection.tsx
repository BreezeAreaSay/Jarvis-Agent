// «Система»: автозапуск, горячие клавиши, оверлей, обновления, папка данных, экспорт/импорт, о программе.
import { Download, FolderOpen, Info, Keyboard, RotateCcw, Rocket, Trash2, Upload, Wand } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "@/api/client";
import type { DeepPartial, Settings } from "@/api/types";
import { LogoMark } from "@/components/Orb";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, Card, Modal, notify, notifyError, Setting, Toggle } from "@/ui";
import { CopyButton, KeyCapture, useApi, useSettingsPatch } from "../kit";

interface Status {
  version: string;
  platform: string;
  data_dir: string;
  uptime_s: number;
}

const DEFAULT_HOTKEYS = { hotkey_show: "Ctrl+Alt+J", hotkey_listen: "Ctrl+Alt+Space" };

/** Путь → file:///C:/… для bridge.openExternal. */
export function fileUrl(path: string): string {
  const p = path.replace(/\\/g, "/");
  return "file:///" + encodeURI(p.replace(/^\/+/, ""));
}

function fmtUptime(s: number): string {
  if (s < 60) return "меньше минуты";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} мин`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч ${m % 60} мин`;
  return `${Math.floor(h / 24)} дн ${h % 24} ч`;
}

const PLATFORMS: Record<string, string> = { windows: "Windows", win32: "Windows", fake: "Демо (без Windows)", linux: "Linux", darwin: "macOS" };

/** Только известные разделы настроек — из чужого файла не принимаем лишнего. */
function pickSettings(raw: unknown): DeepPartial<Settings> | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const src = (raw as { settings?: unknown }).settings ?? raw;
  if (!src || typeof src !== "object") return null;
  const keys = ["profile", "interface", "voice", "tts", "ai", "avatar", "system", "remote", "plugins", "packs", "disabled_commands"];
  const out: Record<string, unknown> = {};
  for (const k of keys) if (k in (src as Record<string, unknown>)) out[k] = (src as Record<string, unknown>)[k];
  return Object.keys(out).length ? (out as DeepPartial<Settings>) : null;
}

function DataCard() {
  const [settings, patch] = useSettingsPatch();
  const patchSettings = useApp((s) => s.patchSettings);
  const status = useApi<Status>("/api/status");
  const file = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<{ name: string; data: DeepPartial<Settings> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [clearAsk, setClearAsk] = useState(false);
  if (!settings) return null;
  const dir = status.data?.data_dir ?? "";

  const exportSettings = () => {
    const blob = new Blob([JSON.stringify({ app: "jarvis", exported: new Date().toISOString(), settings }, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `jarvis-settings-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    notify("Настройки сохранены в файл. Ключи API в файл не попадают", "ok");
  };

  const readFile = (f: File | undefined) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = pickSettings(JSON.parse(String(reader.result)));
        if (!data) throw new Error("В файле нет настроек Jarvis");
        setPending({ name: f.name, data });
      } catch (e) {
        notifyError(e instanceof SyntaxError ? new Error("Файл не похож на настройки Jarvis (нужен .json)") : e);
      }
    };
    reader.readAsText(f);
  };

  const applyImport = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await patchSettings(pending.data);
      notify("Настройки загружены", "ok");
      setPending(null);
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  const clearHistory = async () => {
    setClearAsk(false);
    try {
      await api.del("/api/history");
      notify("История команд очищена", "ok");
    } catch (e) {
      notifyError(e);
    }
  };

  return (
    <Card icon={<FolderOpen />} title="Данные" sub="Настройки, история, заметки и модели хранятся только на этом компьютере">
      <Setting name="Папка данных" desc={dir ? <span className="mono path-text">{dir}</span> : status.error ? "Нет связи с ядром" : "…"} wide>
        {dir && <CopyButton text={dir} label="Скопировать путь" />}
        <Button size="sm" icon={<FolderOpen />} disabled={!dir} onClick={() => bridge.openExternal(fileUrl(dir))}>
          Открыть папку
        </Button>
      </Setting>
      <Setting name="Настройки в файл" desc="Перенести настройки на другой компьютер. Ключи API не сохраняются">
        <Button size="sm" icon={<Download />} onClick={exportSettings}>
          Экспорт
        </Button>
        <Button size="sm" icon={<Upload />} onClick={() => file.current?.click()}>
          Импорт
        </Button>
        <input
          ref={file}
          type="file"
          accept=".json,application/json"
          className="visually-hidden"
          tabIndex={-1}
          onChange={(e) => {
            readFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
      </Setting>
      <Setting name="Знакомство с Jarvis" desc="Пройти мастер первого запуска ещё раз">
        <Button size="sm" icon={<Wand />} onClick={() => patch({ onboarding_done: false })}>
          Запустить
        </Button>
      </Setting>
      <Setting name="История команд" desc="Список того, что вы говорили и что выполнил Jarvis">
        {clearAsk ? (
          <>
            <Button size="sm" variant="danger" onClick={clearHistory}>
              Да, очистить
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setClearAsk(false)}>
              Отмена
            </Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" icon={<Trash2 />} onClick={() => setClearAsk(true)}>
            Очистить
          </Button>
        )}
      </Setting>
      <Modal
        open={!!pending}
        onClose={() => setPending(null)}
        title="Загрузить настройки?"
        width={460}
        footer={
          <>
            <Button onClick={() => setPending(null)}>Отмена</Button>
            <Button variant="primary" loading={busy} onClick={applyImport}>
              Загрузить
            </Button>
          </>
        }
      >
        <p className="muted small" style={{ marginTop: 0 }}>
          Настройки из файла <b>{pending?.name}</b> заменят текущие: интерфейс, голос, ИИ, пульт и остальное. Ключи API
          останутся прежними.
        </p>
        <div className="chips-edit">
          {Object.keys(pending?.data ?? {}).map((k) => (
            <span key={k} className="chip">
              {SECTION_NAMES[k] ?? k}
            </span>
          ))}
        </div>
      </Modal>
    </Card>
  );
}

const SECTION_NAMES: Record<string, string> = {
  profile: "Профиль",
  interface: "Интерфейс",
  voice: "Голос",
  tts: "Синтез речи",
  ai: "Jarvis AI",
  avatar: "Аватар",
  system: "Система",
  remote: "Пульт",
  plugins: "Плагины",
  packs: "Паки команд",
  disabled_commands: "Выключенные команды",
};

function AboutCard() {
  const version = useApp((s) => s.version);
  const platform = useApp((s) => s.platform);
  const status = useApi<Status>("/api/status");
  return (
    <Card icon={<Info />} title="О программе">
      <div className="about">
        <LogoMark size={52} />
        <div className="grow">
          <div className="about-name">Jarvis 2.0</div>
          <div className="faint small">Голосовой ассистент для Windows</div>
        </div>
        <div className="about-grid">
          <span className="faint">Приложение</span>
          <span>{bridge.version}</span>
          <span className="faint">Ядро</span>
          <span>{version || status.data?.version || "—"}</span>
          <span className="faint">Платформа</span>
          <span>{PLATFORMS[platform] ?? (platform || "—")}</span>
          <span className="faint">Работает</span>
          <span>{status.data ? fmtUptime(status.data.uptime_s) : "—"}</span>
        </div>
      </div>
    </Card>
  );
}

export default function SystemSection() {
  const [settings, patch] = useSettingsPatch();
  if (!settings) return null;
  const s = settings.system;
  const changed = s.hotkey_show !== DEFAULT_HOTKEYS.hotkey_show || s.hotkey_listen !== DEFAULT_HOTKEYS.hotkey_listen;
  return (
    <div className="stack">
      <Card icon={<Rocket />} title="Запуск и работа" sub="Как Jarvis живёт рядом с Windows">
        <Setting name="Запускать вместе с Windows" desc="Jarvis стартует в трее и сразу слушает">
          <Toggle checked={s.autostart} onChange={(v) => patch({ system: { autostart: v } })} label="Автозапуск" />
        </Setting>
        <Setting name="Строка голоса поверх окон" desc="Что вы сказали, ответы и уведомления — поверх всех программ">
          <Toggle checked={s.overlay} onChange={(v) => patch({ system: { overlay: v } })} label="Оверлей" />
        </Setting>
        <Setting name="Проверять обновления" desc="Сообщить, когда выйдет новая версия">
          <Toggle checked={s.check_updates} onChange={(v) => patch({ system: { check_updates: v } })} label="Проверять обновления" />
        </Setting>
      </Card>

      <Card
        icon={<Keyboard />}
        title="Горячие клавиши"
        sub="Работают из любой программы. Нажмите на сочетание, чтобы задать новое"
        actions={
          changed && (
            <Button size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => patch({ system: DEFAULT_HOTKEYS })}>
              По умолчанию
            </Button>
          )
        }
      >
        <Setting name="Показать Jarvis" desc="Открыть или спрятать главное окно">
          <KeyCapture value={s.hotkey_show} clearable emptyText="Не задано" onChange={(v) => patch({ system: { hotkey_show: v } })} />
        </Setting>
        <Setting name="Сказать команду" desc="Jarvis слушает без слова-активатора">
          <KeyCapture value={s.hotkey_listen} clearable emptyText="Не задано" onChange={(v) => patch({ system: { hotkey_listen: v } })} />
        </Setting>
      </Card>

      <DataCard />
      <AboutCard />
    </div>
  );
}
