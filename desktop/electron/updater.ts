// Обновления из GitHub Releases (electron-updater): проверка, скачивание с прогрессом, установка с перезапуском.
// Релиз — тег vX.Y.Z с файлами из desktop/release: Jarvis-Setup-X.Y.Z.exe, его .blockmap и latest.yml
// (скрипт scripts/publish-release.ps1). Без подписи кода: проверяется sha512 из latest.yml.
import fs from "node:fs";
import path from "node:path";
import { app } from "electron";
import { autoUpdater, type ProgressInfo, type UpdateInfo } from "electron-updater";

export type UpdateStatus = "disabled" | "idle" | "checking" | "none" | "available" | "downloading" | "downloaded" | "error";

export interface UpdateState {
  status: UpdateStatus;
  current: string;
  version?: string;
  notes?: string;
  date?: string;
  percent?: number;
  transferred?: number;
  total?: number;
  speed?: number;
  error?: string;
  checkedAt?: number;
  /** false — на GitHub ещё нет ни одного выпуска: сравнивать не с чем (не «у вас последняя»). */
  published?: boolean;
}

/** Текст релиза (HTML с GitHub или список версий) → обычный текст для окна «Что нового». */
function plainNotes(notes: UpdateInfo["releaseNotes"]): string {
  const raw = Array.isArray(notes) ? notes.map((n) => `${n.version}\n${n.note ?? ""}`).join("\n\n") : (notes ?? "");
  return raw
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|li|h\d|ul|ol)>/gi, "\n")
    .replace(/<li[^>]*>/gi, "• ")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, "&")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

const OFFLINE = /ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ETIMEDOUT|ECONNRESET|net::ERR|getaddrinfo|socket hang up/i;

/** На GitHub ещё нет ни одного релиза — это не ошибка, но и не «у вас последняя версия». */
function nothingPublished(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : String(e);
  return !OFFLINE.test(msg) && /No published versions|Unable to find latest version/i.test(msg);
}

function humanError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  if (OFFLINE.test(msg)) return "Нет связи с GitHub — проверю позже";
  if (/Cannot find latest\.yml|ERR_UPDATER_CHANNEL_FILE_NOT_FOUND/i.test(msg)) {
    return "В последнем релизе на GitHub нет файла latest.yml — версия опубликована не полностью";
  }
  if (/sha512|checksum/i.test(msg)) return "Скачанный файл повреждён — попробуйте ещё раз";
  if (/\b403\b|rate limit/i.test(msg)) return "GitHub временно ограничил запросы — проверю позже";
  if (/\b404\b/.test(msg)) return "Файл обновления не найден на GitHub — проверю позже";
  return `Не получилось: ${msg.split("\n")[0].slice(0, 180)}`;
}

export class Updater {
  state: UpdateState;
  private listeners = new Set<(state: UpdateState) => void>();
  private checking: Promise<void> | null = null;

  constructor(logDir: string) {
    this.state = { status: app.isPackaged ? "idle" : "disabled", current: app.getVersion() };
    if (!app.isPackaged) return; // в разработке обновлять нечего
    const file = path.join(logDir, "updater.log");
    const write =
      (level: string) =>
      (...args: unknown[]) =>
        fs.appendFile(file, `${new Date().toISOString()} ${level} ${args.map(String).join(" ")}\n`, () => undefined);
    autoUpdater.logger = { info: write("INFO"), warn: write("WARN"), error: write("ERROR"), debug: () => undefined };
    autoUpdater.autoDownload = false; // качаем по настройке «Скачивать сами» или по кнопке
    autoUpdater.autoInstallOnAppQuit = true;
    autoUpdater.allowPrerelease = false;
    autoUpdater.on("checking-for-update", () => this.set({ status: "checking", error: undefined }));
    autoUpdater.on("update-available", (info: UpdateInfo) =>
      this.set({
        status: "available",
        version: info.version,
        notes: plainNotes(info.releaseNotes),
        date: info.releaseDate,
        checkedAt: Date.now(),
      }),
    );
    autoUpdater.on("update-not-available", () => this.set({ status: "none", published: true, checkedAt: Date.now() }));
    autoUpdater.on("download-progress", (p: ProgressInfo) =>
      this.set({ status: "downloading", percent: p.percent, transferred: p.transferred, total: p.total, speed: p.bytesPerSecond }),
    );
    autoUpdater.on("update-downloaded", (info: UpdateInfo) => this.set({ status: "downloaded", version: info.version, percent: 100 }));
    autoUpdater.on("error", (e: unknown) => this.fail(e));
  }

  onChange(listener: (state: UpdateState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Ошибка приходит дважды: событием error и отказом промиса — разбор одинаковый. */
  private fail(e: unknown) {
    if (this.state.status !== "downloading" && nothingPublished(e)) {
      this.set({ status: "none", published: false, error: undefined, checkedAt: Date.now() });
    } else {
      this.set({ status: "error", error: humanError(e) });
    }
  }

  private set(patch: Partial<UpdateState>) {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  /** Есть ли новая версия. Пока качается или уже скачана — не перепроверяем. */
  async check(): Promise<UpdateState> {
    const { status } = this.state;
    if (status === "disabled" || status === "downloading" || status === "downloaded") return this.state;
    this.checking ??= autoUpdater
      .checkForUpdates()
      .then(() => undefined)
      .catch((e: unknown) => this.fail(e))
      .finally(() => {
        this.checking = null;
      });
    await this.checking;
    return this.state;
  }

  async download(): Promise<void> {
    if (this.state.status === "downloading" || this.state.status === "downloaded" || this.state.status === "disabled") return;
    // после ошибки или «нет обновлений» — сперва узнать, что качать
    if (this.state.status !== "available" && (await this.check()).status !== "available") return;
    this.set({ status: "downloading", percent: 0, error: undefined });
    try {
      await autoUpdater.downloadUpdate();
    } catch (e) {
      this.fail(e);
    }
  }

  /** Перезапуститься в новую версию: beforeQuit останавливает ядро и снимает хоткеи; не вышло — onFail. */
  async install(beforeQuit: () => Promise<void>, onFail: () => void): Promise<void> {
    if (this.state.status !== "downloaded") return;
    await beforeQuit();
    autoUpdater.quitAndInstall(true, true); // тихая установка поверх и запуск новой версии
    // установщик не найден или не запустился — electron-updater сообщает об этом сразу (событие error)
    if (this.failed()) onFail();
  }

  private failed(): boolean {
    return this.state.status === "error";
  }
}
