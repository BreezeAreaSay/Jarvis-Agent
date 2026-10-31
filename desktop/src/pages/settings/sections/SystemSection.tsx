// «Система»: автозапуск, горячие клавиши, оверлей, обновления, папка данных, экспорт/импорт, о программе.
import {
  CircleArrowUp,
  Download,
  Stethoscope,
  ExternalLink,
  FolderOpen,
  Info,
  Keyboard,
  RefreshCw,
  RotateCcw,
  Rocket,
  Trash2,
  Upload,
  Wand,
} from "lucide-react";
import { useRef, useState } from "react";
import { api } from "@/api/client";
import type { DeepPartial, Settings } from "@/api/types";
import { LogoMark } from "@/components/Orb";
import { bridge, isElectron } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { RELEASES_URL, type UpdateState, updates, useUpdates } from "@/lib/updates";
import { useApp } from "@/store/app";
import { Button, Card, Modal, notify, notifyError, Setting, Toggle } from "@/ui";
import { CopyButton, KeyCapture, ProgressBar, useApi, useSettingsPatch } from "../kit";

interface DiagnosticsItem {
  id: string;
  title: string;
  status: "ok" | "warn" | "fail" | "off";
  detail: string;
  hint: string;
  action: string;
}

/** «Диагностика»: состояние частей Jarvis и отчёт для сообщения о проблеме (GET /api/diagnostics). */
function DiagnosticsCard() {
  const q = useApi<{ items: DiagnosticsItem[]; report: string }>("/api/diagnostics");
  const [busy, setBusy] = useState(false);
  const items = q.data?.items ?? [];
  const resetCrashes = async () => {
    setBusy(true);
    try {
      await api.post("/api/diagnostics/crashes/reset");
      await q.reload();
      notify("Попробую снова: эти части загрузятся при следующем использовании");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      icon={<Stethoscope />}
      title="Диагностика"
      sub="Что работает и что нет. Отчёт без ключей и имени пользователя — для сообщения о проблеме"
      actions={
        <div className="row" style={{ gap: 6 }}>
          {q.data && <CopyButton text={q.data.report} label="Скопировать отчёт" />}
          <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={() => void q.reload()}>
            Обновить
          </Button>
        </div>
      }
    >
      {!q.data && !q.error && (
        <div className="row faint small" style={{ padding: 10 }}>
          <span className="spinner" /> Проверяю…
        </div>
      )}
      {q.error && <div className="faint small">Диагностика недоступна: {q.error.message}</div>}
      <div className="diag-list">
        {items.map((i) => (
          <div key={i.id} className={`diag-item diag-${i.status}`}>
            <span className={`status-dot ${i.status === "ok" ? "ok" : i.status === "fail" ? "err" : i.status === "warn" ? "warn" : ""}`} />
            <div className="grow">
              <div className="diag-title">{i.title}</div>
              <div className="diag-detail">{i.detail}</div>
              {i.hint && <div className="diag-hint">{i.hint}</div>}
            </div>
            {i.action === "reset" && (
              <Button size="sm" variant="ghost" icon={<RotateCcw />} disabled={busy} onClick={() => void resetCrashes()}>
                Попробовать снова
              </Button>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

interface Status {
  version: string;
  platform: string;
  data_dir: string;
  uptime_s: number;
}

const DEFAULT_HOTKEYS = { hotkey_show: "Ctrl+Alt+J", hotkey_listen: "Ctrl+Alt+Space", hotkey_mic: "Ctrl+Alt+M" };

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
  const keys = ["profile", "interface", "voice", "tts", "ai", "avatar", "system", "remote", "privacy", "plugins", "packs", "disabled_commands"];
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
    <Card
      icon={<FolderOpen />}
      title="Данные"
      sub={
        <>
          Настройки, история, заметки и модели лежат в папке данных на этом компьютере. Что уходит провайдеру ИИ, сроки
          хранения и удаление —{" "}
          <button type="button" className="link-btn" onClick={() => navigate("settings", "privacy")}>
            «Приватность и данные»
          </button>
        </>
      }
    >
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
      <Setting name="История команд" desc="Список того, что вы говорили и что выполнил Jarvis (и ваши «Разрешить» / «Отклонить»)">
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
  privacy: "Сроки хранения",
  plugins: "Плагины",
  packs: "Паки команд",
  disabled_commands: "Выключенные команды",
};

const LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0";

function AboutCard() {
  const version = useApp((s) => s.version);
  const platform = useApp((s) => s.platform);
  const status = useApi<Status>("/api/status");
  return (
    <Card icon={<Info />} title="О программе">
      <div className="about">
        <LogoMark size={52} />
        <div className="grow">
          <div className="about-name">Jarvis</div>
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
          <span className="faint">Лицензия</span>
          <button type="button" className="link-btn" onClick={() => bridge.openExternal(LICENSE_URL)} title="Бесплатно для некоммерческого использования">
            PolyForm Noncommercial 1.0.0
          </button>
        </div>
      </div>
    </Card>
  );
}

function mb(bytes?: number): string {
  return bytes ? `${(bytes / 1048576).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ` : "";
}

function whenChecked(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts);
  return `проверено в ${d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
}

/** Обновления из GitHub: что сейчас, проверка, скачивание с прогрессом, перезапуск в новую версию. */
function UpdatesCard({
  checkUpdates,
  autoUpdate,
  onCheckUpdates,
  onAutoUpdate,
}: {
  checkUpdates: boolean;
  autoUpdate: boolean;
  onCheckUpdates: (v: boolean) => void;
  onAutoUpdate: (v: boolean) => void;
}) {
  const u: UpdateState = useUpdates();
  const [notesOpen, setNotesOpen] = useState(false);
  let line: React.ReactNode;
  let action: React.ReactNode = null;
  switch (u.status) {
    case "disabled":
      line = isElectron ? "Обновления работают в установленной версии Jarvis" : "Обновления доступны в приложении Jarvis";
      break;
    case "idle":
      line = checkUpdates ? "Скоро проверю, есть ли новая версия" : "Автопроверка выключена — можно проверить вручную";
      break;
    case "checking":
      line = (
        <span className="row" style={{ gap: 8 }}>
          <span className="spinner" /> Проверяю…
        </span>
      );
      break;
    case "available":
      line = <b>Доступна версия {u.version}</b>;
      action = (
        <Button variant="primary" icon={<Download />} onClick={updates.download}>
          Скачать
        </Button>
      );
      break;
    case "downloading":
      line = (
        <span>
          Скачиваю {u.version} — {Math.round(u.percent ?? 0)}%
          {u.total ? ` · ${mb(u.transferred)} из ${mb(u.total)}` : ""}
          {u.speed ? ` · ${mb(u.speed)}/с` : ""}
        </span>
      );
      break;
    case "downloaded":
      line = <b>Версия {u.version} скачана — установится при перезапуске</b>;
      action = (
        <Button variant="primary" icon={<CircleArrowUp />} onClick={updates.install}>
          Перезапустить и обновить
        </Button>
      );
      break;
    case "error":
      line = <span className="err-text">{u.error}</span>;
      action = (
        <Button icon={<RefreshCw />} onClick={updates.check}>
          Попробовать снова
        </Button>
      );
      break;
    default:
      if (u.status === "none" && u.published === false) {
        // на GitHub нет ни одного выпуска: честно говорим, что сравнивать не с чем
        line = (
          <span>
            На GitHub пока нет опубликованных версий — обновляться не на что
            {u.checkedAt ? ` · ${whenChecked(u.checkedAt)}` : ""}
          </span>
        );
        action = (
          <Button size="sm" variant="ghost" icon={<ExternalLink />} onClick={() => bridge.openExternal(RELEASES_URL)}>
            Выпуски
          </Button>
        );
      } else {
        line = `У вас последняя версия${u.checkedAt ? ` · ${whenChecked(u.checkedAt)}` : ""}`;
      }
  }
  const canCheck = u.status === "idle" || u.status === "none"; // в остальных состояниях своя кнопка в рамке
  return (
    <Card
      icon={<CircleArrowUp />}
      title="Обновления"
      sub={`Сейчас установлена версия ${u.current}`}
      actions={
        canCheck && (
          <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={updates.check}>
            Проверить сейчас
          </Button>
        )
      }
    >
      <div className="update-box">
        <div className="update-line">
          <div className="grow">{line}</div>
          {action}
        </div>
        {u.status === "downloading" && <ProgressBar value={(u.percent ?? 0) / 100} />}
        {u.notes && (u.status === "available" || u.status === "downloading" || u.status === "downloaded") && (
          <div className="update-notes">
            <button className="link-btn" onClick={() => setNotesOpen(!notesOpen)}>
              {notesOpen ? "Скрыть, что нового" : "Что нового"}
            </button>
            {notesOpen && <pre>{u.notes}</pre>}
          </div>
        )}
      </div>
      <Setting name="Проверять обновления" desc="При запуске и каждые 6 часов — на GitHub">
        <Toggle checked={checkUpdates} onChange={onCheckUpdates} label="Проверять обновления" />
      </Setting>
      <Setting name="Скачивать сами" desc="Найденная версия скачается в фоне, Jarvis предложит перезапуститься">
        <Toggle checked={autoUpdate} onChange={onAutoUpdate} label="Скачивать обновления сами" disabled={!checkUpdates} />
      </Setting>
    </Card>
  );
}

export default function SystemSection() {
  const [settings, patch] = useSettingsPatch();
  if (!settings) return null;
  const s = settings.system;
  const changed =
    s.hotkey_show !== DEFAULT_HOTKEYS.hotkey_show ||
    s.hotkey_listen !== DEFAULT_HOTKEYS.hotkey_listen ||
    (s.hotkey_mic ?? DEFAULT_HOTKEYS.hotkey_mic) !== DEFAULT_HOTKEYS.hotkey_mic;
  return (
    <div className="stack">
      <Card icon={<Rocket />} title="Запуск и работа" sub="Как Jarvis живёт рядом с Windows">
        <Setting name="Запускать вместе с Windows" desc="Jarvis стартует в трее и сразу слушает">
          <Toggle checked={s.autostart} onChange={(v) => patch({ system: { autostart: v } })} label="Автозапуск" />
        </Setting>
        <Setting name="Строка голоса поверх окон" desc="Что вы сказали, ответы и уведомления — поверх всех программ">
          <Toggle checked={s.overlay} onChange={(v) => patch({ system: { overlay: v } })} label="Оверлей" />
        </Setting>
      </Card>

      <UpdatesCard
        checkUpdates={s.check_updates}
        autoUpdate={s.auto_update ?? true}
        onCheckUpdates={(v) => patch({ system: { check_updates: v } })}
        onAutoUpdate={(v) => patch({ system: { auto_update: v } })}
      />

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
        <Setting name="Сказать команду" desc="Jarvis слушает без слова-активатора; ещё раз — «стоп»">
          <KeyCapture value={s.hotkey_listen} clearable emptyText="Не задано" onChange={(v) => patch({ system: { hotkey_listen: v } })} />
        </Setting>
        <Setting name="Микрофон Jarvis" desc="Выключить или снова включить: пока выключен, Jarvis не слушает совсем">
          <KeyCapture value={s.hotkey_mic ?? ""} clearable emptyText="Не задано" onChange={(v) => patch({ system: { hotkey_mic: v } })} />
        </Setting>
      </Card>

      <DataCard />
      <DiagnosticsCard />
      <AboutCard />
    </div>
  );
}
