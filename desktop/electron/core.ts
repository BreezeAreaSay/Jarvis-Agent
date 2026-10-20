// Ядро Jarvis (Python) как дочерний процесс: запуск, ожидание строки готовности, перезапуск при падении.
import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { app } from "electron";

export interface CoreInfo {
  url: string;
  token: string;
  pid: number;
  version: string;
}

const READY = "JARVIS_CORE_READY ";

/** Ядро перестало запускаться: код выхода, понятная причина и последние строки журнала. */
export interface CoreFailure {
  code: number | null;
  reason: string;
  tail: string[];
  at: number;
}

type Listener = (info: CoreInfo | null) => void;

/** Самая говорящая строка из хвоста журнала. */
function pickReason(tail: string[], code: number | null): string {
  if (code === 3) return "Ядро уже запущено другим процессом — возможно, осталось от прошлого запуска Jarvis";
  const line = [...tail]
    .reverse()
    .find((l) => /error|ошибк|exception|fatal|failed|не удалось|no such file|не найден/i.test(l) && !/^\s*File "/.test(l));
  return (line ?? tail[tail.length - 1] ?? `ядро завершилось с кодом ${code}`).trim().slice(0, 400);
}

export class CoreProcess {
  info: CoreInfo | null = null;
  private proc: ChildProcess | null = null;
  private waiters: ((info: CoreInfo) => void)[] = [];
  private listeners = new Set<Listener>();
  private restarts: number[] = [];
  private stopping = false;
  private log: fs.WriteStream;
  private tail: string[] = [];
  private failed: CoreFailure | null = null;
  private failListeners = new Set<(failure: CoreFailure | null) => void>();

  constructor() {
    const dir = path.join(app.getPath("userData"), "logs");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, "core-process.log");
    const fresh = !fs.existsSync(file) || fs.statSync(file).size === 0;
    this.log = fs.createWriteStream(file, { flags: "a" });
    if (fresh) this.log.write("\ufeff"); // UTF-8 с BOM: Блокнот и PowerShell 5 читают кириллицу правильно
  }

  /** Почему ядро не работает (null — работает или ещё пробуем). */
  failure(): CoreFailure | null {
    return this.failed;
  }

  onFailed(listener: (failure: CoreFailure | null) => void): () => void {
    this.failListeners.add(listener);
    return () => this.failListeners.delete(listener);
  }

  private setFailed(failure: CoreFailure | null) {
    this.failed = failure;
    for (const l of this.failListeners) l(failure);
  }

  onChange(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  ready(): Promise<CoreInfo> {
    if (this.info) return Promise.resolve(this.info);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  private setInfo(info: CoreInfo | null) {
    this.info = info;
    if (info) {
      for (const w of this.waiters.splice(0)) w(info);
    }
    for (const l of this.listeners) l(info);
  }

  /** Команда запуска: внешнее ядро (разработка), Python из установки или venv репозитория. */
  private command(): { cmd: string; args: string[]; env: NodeJS.ProcessEnv; cwd: string } | null {
    const common = ["-X", "utf8", "-m", "jarvis", "--port", "0", "--parent-pid", String(process.pid)];
    const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONNOUSERSITE: "1" };
    if (app.isPackaged) {
      const root = path.join(process.resourcesPath, "core");
      const exe = path.join(root, "python", process.platform === "win32" ? "python.exe" : "bin/python3");
      return { cmd: exe, args: common, env: { ...env, PYTHONPATH: path.join(root, "app") }, cwd: root };
    }
    const coreDir = path.resolve(app.getAppPath(), "..", "core");
    const venv = path.join(coreDir, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
    if (fs.existsSync(venv)) return { cmd: venv, args: common, env, cwd: coreDir };
    const uv = spawnSync(process.platform === "win32" ? "where" : "which", ["uv"], { encoding: "utf8" });
    if (uv.status === 0) {
      return { cmd: "uv", args: ["run", "--project", coreDir, "python", ...common], env, cwd: coreDir };
    }
    return null;
  }

  start() {
    const external = process.env.JARVIS_CORE_URL;
    if (external) {
      this.setInfo({
        url: external.replace(/\/$/, ""),
        token: process.env.JARVIS_CORE_TOKEN ?? "devtoken",
        pid: 0,
        version: "dev",
      });
      return;
    }
    const spec = this.command();
    if (!spec) {
      this.write("не найден Python ядра: соберите core (uv sync) или установите программу заново\n");
      this.setFailed({ code: null, reason: "Не найден Python ядра — установите Jarvis заново", tail: [], at: Date.now() });
      return;
    }
    this.stopping = false;
    this.write(`запуск ядра: ${spec.cmd} ${spec.args.join(" ")}\n`);
    const proc = spawn(spec.cmd, spec.args, {
      cwd: spec.cwd,
      env: spec.env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.proc = proc;
    readline.createInterface({ input: proc.stdout! }).on("line", (line) => {
      if (line.startsWith(READY)) {
        try {
          const data = JSON.parse(line.slice(READY.length));
          this.setInfo({ url: `http://127.0.0.1:${data.port}`, token: data.token, pid: data.pid, version: data.version });
        } catch (e) {
          this.write(`плохая строка готовности: ${line} (${e})\n`);
        }
      } else {
        this.write(line + "\n");
      }
    });
    proc.stderr!.on("data", (chunk: Buffer) => this.write(chunk.toString("utf8")));
    proc.on("exit", (code, signal) => {
      this.write(`ядро завершилось: code=${code} signal=${signal}\n`);
      if (this.proc === proc) this.proc = null;
      this.setInfo(null);
      if (this.stopping) return;
      const now = Date.now();
      this.restarts = this.restarts.filter((t) => now - t < 60_000);
      if (this.restarts.length >= 5) {
        this.write("ядро падает слишком часто — перезапуск остановлен\n");
        this.setFailed({ code, reason: pickReason(this.tail, code), tail: this.tail.slice(-30), at: now });
        return;
      }
      this.restarts.push(now);
      setTimeout(() => this.start(), 1000 * this.restarts.length);
    });
  }

  async restart() {
    await this.stop();
    this.restarts = [];
    this.setFailed(null);
    this.start();
  }

  async stop(): Promise<void> {
    this.stopping = true;
    const proc = this.proc;
    if (!proc || proc.exitCode !== null) return;
    // сначала просим выйти штатно: сервисы закроют базу и файлы (на Windows kill — это TerminateProcess)
    const info = this.info;
    if (info) {
      const exited = new Promise<boolean>((resolve) => {
        proc.once("exit", () => resolve(true));
        setTimeout(() => resolve(false), 3000);
      });
      try {
        await fetch(info.url + "/api/shutdown", {
          method: "POST",
          headers: { "X-Jarvis-Token": info.token },
          signal: AbortSignal.timeout(2500),
        });
        if (await exited) return;
      } catch {
        /* ядро не отвечает — завершаем силой */
      }
    }
    if (proc.exitCode !== null) return;
    return new Promise((resolve) => {
      const done = () => resolve();
      proc.once("exit", done);
      proc.kill();
      setTimeout(() => {
        if (proc.exitCode === null) {
          if (process.platform === "win32" && proc.pid) {
            spawnSync("taskkill", ["/pid", String(proc.pid), "/t", "/f"], { windowsHide: true });
          } else {
            proc.kill("SIGKILL");
          }
        }
        resolve();
      }, 4000);
    });
  }

  private write(text: string) {
    for (const line of text.split(/\r?\n/)) {
      if (line.trim()) this.tail.push(line);
    }
    if (this.tail.length > 80) this.tail.splice(0, this.tail.length - 80);
    try {
      this.log.write(text);
    } catch {
      /* журнал не критичен */
    }
  }
}

/** Небольшой клиент ядра для главного процесса: настройки, события, вызовы. */
export class CoreClient {
  private ws: WebSocket | null = null;
  private handlers = new Map<string, Set<(data: any) => void>>();
  private retry = 0;
  private closed = false;

  constructor(private core: CoreProcess) {
    core.onChange((info) => {
      if (info) this.connect();
    });
  }

  on(type: string, handler: (data: any) => void) {
    let set = this.handlers.get(type);
    if (!set) this.handlers.set(type, (set = new Set()));
    set.add(handler);
  }

  private emit(type: string, data: unknown) {
    for (const h of this.handlers.get(type) ?? []) {
      try {
        h(data);
      } catch (e) {
        console.error("обработчик события ядра упал", type, e);
      }
    }
  }

  private connect() {
    const info = this.core.info;
    if (!info || this.closed) return;
    try {
      this.ws?.close();
    } catch {
      /* старое соединение */
    }
    const ws = new WebSocket(`${info.url.replace(/^http/, "ws")}/ws?token=${encodeURIComponent(info.token)}`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      ws.send(
        JSON.stringify({
          type: "subscribe",
          patterns: [
            "hello",
            "settings.changed",
            "fullscreen.changed",
            "ui.*",
            "desk.open",
            "confirm.request",
            "plugins.changed",
            // значок в трее: что делает голос, идёт ли запись конспекта, включены ли субтитры
            "voice.state",
            "plugin.summary.state",
            "plugin.subtitles.state",
            // пока работает агент, оверлей не попадает на снимки экрана (агент видит, что под ним)
            "agent.state",
          ],
        }),
      );
    };
    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(String(msg.data));
        this.emit(event.type, event.data);
      } catch {
        /* мусор */
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws || this.closed) return;
      this.ws = null;
      setTimeout(() => this.connect(), Math.min(5000, 300 * 2 ** this.retry++));
    };
    ws.onerror = () => ws.close();
  }

  async request(method: string, pathname: string, body?: unknown): Promise<any> {
    const info = await this.core.ready();
    const res = await fetch(info.url + pathname, {
      method,
      headers: { "X-Jarvis-Token": info.token, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`${method} ${pathname}: ${res.status}`);
    return res.json();
  }

  close() {
    this.closed = true;
    this.ws?.close();
  }

  /** Снова на связи — выход отменился (установщик обновления не запустился). */
  reopen() {
    this.closed = false;
    this.retry = 0;
    this.connect();
  }
}
