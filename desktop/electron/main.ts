// Главный процесс Jarvis: ядро, окна (главное, оверлей поверх всего, аватар), трей, хоткеи, автозапуск.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  screen,
  shell,
  Tray,
  type MenuItemConstructorOptions,
} from "electron";
import { CoreClient, CoreProcess } from "./core";
import { Updater } from "./updater";

const DEV_URL = process.env.JARVIS_DEV_URL ?? (app.isPackaged ? "" : "http://127.0.0.1:5173");
const START_HIDDEN = process.argv.includes("--hidden");
const VERSION = app.getVersion();

app.setAppUserModelId("com.breezeareasay.jarvis");
if (!app.requestSingleInstanceLock()) {
  app.quit();
  process.exit(0);
}

// интерфейс русский — и даты/время в полях ввода тоже по-русски, какой бы ни была система
app.commandLine.appendSwitch("lang", "ru");

const core = new CoreProcess();
const client = new CoreClient(core);

let mainWin: BrowserWindow | null = null;
let overlayWin: BrowserWindow | null = null;
let avatarWin: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let coreStopped = false; // ядро остановлено перед выходом — можно закрываться
let settings: any = null;
let fullscreen = false;
const registered: string[] = [];
const LOGS_DIR = path.join(app.getPath("userData"), "logs");
const updater = new Updater(LOGS_DIR);
let updateNotified = ""; // «статус:версия», о которых уже сказали уведомлением
let updatesStarted = false;

// --- общее ------------------------------------------------------------------------------------------
function resource(...parts: string[]): string {
  return app.isPackaged ? path.join(process.resourcesPath, ...parts) : path.join(app.getAppPath(), "build", ...parts);
}

function iconImage(name = "icon.png") {
  const file = resource(name);
  return fs.existsSync(file) ? nativeImage.createFromPath(file) : nativeImage.createEmpty();
}

function loadRoute(win: BrowserWindow, route: string) {
  if (DEV_URL) {
    win.loadURL(`${DEV_URL}/#/${route}`);
  } else {
    win.loadFile(path.join(app.getAppPath(), "dist", "index.html"), { hash: `/${route}` });
  }
}

function webPreferences(): Electron.WebPreferences {
  return {
    preload: path.join(__dirname, "preload.cjs"),
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    spellcheck: false,
    backgroundThrottling: false,
    additionalArguments: [`--jarvis-version=${VERSION}`],
  };
}

// --- журнал интерфейса: ошибки окон и вылеты процессов отрисовки (logs/ui.log) ----------------------------
const UI_LOG = path.join(LOGS_DIR, "ui.log");

function uiLog(line: string) {
  try {
    fs.mkdirSync(LOGS_DIR, { recursive: true });
    if (fs.existsSync(UI_LOG) && fs.statSync(UI_LOG).size > 2_000_000) fs.renameSync(UI_LOG, path.join(LOGS_DIR, "ui.old.log"));
    fs.appendFileSync(UI_LOG, `${new Date().toISOString()} ${line}\n`);
  } catch {
    /* журнал — не повод падать */
  }
}

app.on("child-process-gone", (_e, d) => {
  if (d.reason !== "clean-exit") uiLog(`процесс ${d.type}${d.name ? ` (${d.name})` : ""} завершился: ${d.reason}, код ${d.exitCode}`);
});

function guard(win: BrowserWindow, kind: string) {
  // ошибки интерфейса — в журнал; вылетел процесс отрисовки — перезагрузить окно, а не оставить пустым
  const reloads: number[] = [];
  win.webContents.on("console-message", (e) => {
    if (e.level === "error") uiLog(`[${kind}] ${e.message.slice(0, 4000)}${e.sourceId ? ` (${path.basename(e.sourceId)}:${e.lineNumber})` : ""}`);
  });
  win.webContents.on("render-process-gone", (_e, d) => {
    uiLog(`[${kind}] процесс отрисовки завершился: ${d.reason}, код ${d.exitCode}`);
    if (quitting || d.reason === "clean-exit" || win.isDestroyed()) return;
    const now = Date.now();
    while (reloads.length && now - reloads[0] > 60_000) reloads.shift();
    if (reloads.length >= 3) return; // падает снова и снова — не крутимся, в журнале уже есть причина
    reloads.push(now);
    setTimeout(() => !win.isDestroyed() && win.webContents.reload(), 800);
  });
  win.webContents.on("unresponsive", () => uiLog(`[${kind}] окно не отвечает`));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    const allowed = DEV_URL ? url.startsWith(DEV_URL) : url.startsWith("file://");
    if (!allowed) {
      e.preventDefault();
      if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    }
  });
}

// --- главное окно ----------------------------------------------------------------------------------------
function createMain() {
  const material = settings?.interface?.window_material ?? "none";
  mainWin = new BrowserWindow({
    width: 1180,
    height: 760,
    minWidth: 940,
    minHeight: 620,
    show: false,
    frame: false,
    title: "Jarvis",
    icon: iconImage(),
    backgroundColor: material !== "none" && process.platform === "win32" ? "#00000000" : "#121419",
    webPreferences: webPreferences(),
  });
  guard(mainWin, "main");
  loadRoute(mainWin, "home");
  mainWin.once("ready-to-show", () => {
    applyMaterial();
    if (!START_HIDDEN && !settings?.interface?.start_minimized) mainWin?.show();
  });
  mainWin.on("close", (e) => {
    if (!quitting && (settings?.interface?.minimize_to_tray ?? true)) {
      e.preventDefault();
      mainWin?.hide();
    }
  });
  mainWin.on("closed", () => (mainWin = null));
}

function showMain(route?: string) {
  if (!mainWin) createMain();
  const win = mainWin!;
  if (win.isMinimized()) win.restore();
  win.show();
  if (process.platform === "win32" && !win.isFocused()) {
    // Windows не отдаёт передний план фоновой программе (щелчок по аватару, уведомление): без этого окно
    // открывается под браузером — «ничего не произошло». На миг «поверх всех» — и окно наверху.
    const pinned = win.isAlwaysOnTop();
    win.setAlwaysOnTop(true);
    win.moveTop();
    win.focus();
    if (!pinned) win.setAlwaysOnTop(false);
  } else {
    win.focus();
  }
  if (route) win.webContents.send("navigate", route);
}

function applyMaterial() {
  if (!mainWin || process.platform !== "win32") return;
  const material = settings?.interface?.window_material ?? "none";
  try {
    mainWin.setBackgroundMaterial(material === "none" ? "none" : material);
  } catch {
    /* Windows 10: материалов нет */
  }
  mainWin.webContents.send("window:material", material);
  mainWin.setAlwaysOnTop(!!settings?.interface?.always_on_top);
}

// --- оверлей: строка голоса, уведомления, подтверждения, агент, субтитры, питомец ----------------------------
function overlayBounds() {
  const display = screen.getPrimaryDisplay();
  return { ...display.workArea, scale: display.scaleFactor };
}

function createOverlay() {
  const b = overlayBounds();
  overlayWin = new BrowserWindow({
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    focusable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    webPreferences: webPreferences(),
  });
  guard(overlayWin, "overlay");
  overlayWin.setAlwaysOnTop(true, "screen-saver");
  overlayWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWin.setIgnoreMouseEvents(true, { forward: true });
  loadRoute(overlayWin, "overlay");
  overlayWin.webContents.on("did-finish-load", () => {
    overlayWin?.webContents.send("overlay:bounds", overlayBounds());
    syncOverlay();
  });
  overlayWin.on("closed", () => (overlayWin = null));
}

function syncOverlay() {
  if (!overlayWin) return;
  const enabled = settings?.system?.overlay ?? true;
  if (enabled && !overlayWin.isVisible()) overlayWin.showInactive();
  if (!enabled && overlayWin.isVisible()) overlayWin.hide();
}

function updateOverlayBounds() {
  if (!overlayWin) return;
  const b = overlayBounds();
  overlayWin.setBounds({ x: b.x, y: b.y, width: b.width, height: b.height });
  overlayWin.webContents.send("overlay:bounds", b);
}

// --- аватар на рабочем столе -------------------------------------------------------------------------------
let dragOffset: { x: number; y: number } | null = null;

function avatarRect() {
  const size = Math.round((settings?.avatar?.size ?? 150) + 40);
  const area = screen.getPrimaryDisplay().workArea;
  const pos = settings?.avatar?.position;
  const x = Array.isArray(pos) ? pos[0] : area.x + area.width - size - 24;
  const y = Array.isArray(pos) ? pos[1] : area.y + area.height - size - 24;
  return { x, y, width: size, height: size };
}

function createAvatar() {
  avatarWin = new BrowserWindow({
    ...avatarRect(),
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: false,
    maximizable: false,
    minimizable: false,
    skipTaskbar: true,
    hasShadow: false,
    focusable: false,
    alwaysOnTop: settings?.avatar?.on_top ?? true,
    webPreferences: webPreferences(),
  });
  guard(avatarWin, "avatar");
  loadRoute(avatarWin, "avatar");
  avatarWin.once("ready-to-show", () => syncAvatar());
  avatarWin.on("closed", () => (avatarWin = null));
}

function syncAvatar() {
  const want = !!settings?.avatar?.desktop && !(fullscreen && (settings?.avatar?.hide_fullscreen ?? true));
  if (want && !avatarWin) {
    createAvatar();
    return;
  }
  if (!avatarWin) return;
  if (!want) {
    avatarWin.hide();
    return;
  }
  const r = avatarRect();
  const [w] = avatarWin.getSize();
  if (w !== r.width) avatarWin.setBounds(r);
  avatarWin.setAlwaysOnTop(settings?.avatar?.on_top ?? true, "floating");
  if (!avatarWin.isVisible()) avatarWin.showInactive();
}

// --- трей и меню -------------------------------------------------------------------------------------------
function api(method: string, pathname: string, body?: unknown) {
  return client.request(method, pathname, body).catch((e) => console.error(e));
}

function patch(body: unknown) {
  return api("PATCH", "/api/settings", body);
}

function quickMenu(): MenuItemConstructorOptions[] {
  return [
    { label: "Открыть Jarvis", click: () => showMain() },
    { label: "Сказать команду", click: () => api("POST", "/api/voice/listen") },
    { label: "Стоп — замолчать и не слушать", click: () => api("POST", "/api/stop") },
    {
      label: "Музыка",
      submenu: [
        { label: "Пауза / продолжить", click: () => api("POST", "/api/media/play_pause") },
        { label: "Следующий трек", click: () => api("POST", "/api/media/next") },
        { label: "Предыдущий трек", click: () => api("POST", "/api/media/prev") },
      ],
    },
    { type: "separator" },
    {
      label: "Микрофон",
      type: "checkbox",
      checked: !!settings?.voice?.enabled,
      // клавиша — только подписью: сам хоткей зарегистрирован глобально (registerShortcuts)
      accelerator: settings?.system?.hotkey_mic || undefined,
      registerAccelerator: false,
      click: (item) => patch({ voice: { enabled: item.checked } }),
    },
    {
      label: "Тихий режим",
      type: "checkbox",
      checked: !!settings?.voice?.silent_mode,
      click: (item) => patch({ voice: { silent_mode: item.checked } }),
    },
    {
      label: "Аватар на рабочем столе",
      type: "checkbox",
      checked: !!settings?.avatar?.desktop,
      click: (item) => patch({ avatar: { desktop: item.checked } }),
    },
  ];
}

function rebuildTray() {
  if (!tray) {
    tray = new Tray(iconImage(process.platform === "win32" ? "tray.ico" : "tray.png"));
    tray.setToolTip("Jarvis");
    tray.on("click", () => (mainWin?.isVisible() && mainWin.isFocused() ? mainWin.hide() : showMain()));
  }
  tray.setContextMenu(
    Menu.buildFromTemplate([
      ...quickMenu(),
      { type: "separator" },
      ...updateMenu(),
      { label: "Перезапустить ядро", click: () => core.restart() },
      { label: "Папка журналов", click: () => shell.openPath(LOGS_DIR) },
      { type: "separator" },
      { label: "Выход", click: () => quit() },
    ]),
  );
}

// --- горячие клавиши и автозапуск ------------------------------------------------------------------------
/** Микрофон Jarvis: выключить или включить (хоткей, меню) — с уведомлением, чтобы было видно, что случилось. */
async function toggleMic() {
  const on = !(settings?.voice?.enabled ?? true);
  await patch({ voice: { enabled: on } });
  if (Notification.isSupported()) {
    new Notification({
      title: "Jarvis",
      body: on ? "Микрофон включён — слушаю «Джарвис»" : "Микрофон выключен — Jarvis не слушает",
      icon: iconImage(),
      silent: true,
    }).show();
  }
}

function registerShortcuts() {
  for (const acc of registered.splice(0)) globalShortcut.unregister(acc);
  const bind = (acc: string | undefined, fn: () => void) => {
    if (!acc) return;
    try {
      if (globalShortcut.register(acc, fn)) registered.push(acc);
    } catch (e) {
      console.error("хоткей не зарегистрирован", acc, e);
    }
  };
  bind(settings?.system?.hotkey_show, () => (mainWin?.isVisible() && mainWin.isFocused() ? mainWin.hide() : showMain()));
  // ещё раз, пока Jarvis слушает или отвечает, — «стоп»
  bind(settings?.system?.hotkey_listen, () => api("POST", "/api/voice/listen", { toggle: true }));
  bind(settings?.system?.hotkey_mic, () => toggleMic());
  // из ядра — действующие настройки с умолчаниями (сохранённые могут не содержать клавишу по умолчанию)
  const hotkeys =
    pluginHotkeys ??
    Object.entries<any>(settings?.plugins ?? {})
      .filter(([, state]) => state?.enabled && state?.config?.hotkey)
      .map(([id, state]) => ({ id, hotkey: String(state.config.hotkey) }));
  for (const { id, hotkey } of hotkeys) {
    bind(hotkey, () => api("POST", `/api/plugins/${id}/action`, { action: "toggle", args: {} }));
  }
}

let pluginHotkeys: { id: string; hotkey: string }[] | null = null;

async function loadPluginHotkeys() {
  const res = await api("GET", "/api/plugins");
  if (Array.isArray(res?.items)) {
    pluginHotkeys = res.items
      .filter((p: any) => p?.enabled && p?.config?.hotkey)
      .map((p: any) => ({ id: String(p.id), hotkey: String(p.config.hotkey) }));
  }
  registerShortcuts();
}

// --- обновления: GitHub Releases через electron-updater (updater.ts) ------------------------------------------
function updateMenu(): MenuItemConstructorOptions[] {
  const u = updater.state;
  if (u.status === "downloaded") return [{ label: `Перезапустить и обновить до ${u.version}`, click: () => installUpdate() }];
  if (u.status === "downloading") return [{ label: `Скачиваю обновление… ${Math.round(u.percent ?? 0)}%`, enabled: false }];
  if (u.status === "available") return [{ label: `Скачать обновление ${u.version}`, click: () => void updater.download() }];
  return [];
}

function installUpdate() {
  void updater.install(shutdown, () => {
    // установщик не запустился (причина — в карточке «Обновления»): работаем дальше
    quitting = false;
    coreStopped = false;
    client.reopen();
    core.start();
    registerShortcuts();
  });
}

function checkUpdates() {
  if (settings?.system?.check_updates ?? true) void updater.check();
}

let trayKey = "";
updater.onChange((state) => {
  mainWin?.webContents.send("update:state", state);
  const key = `${state.status}:${state.status === "downloading" ? Math.floor((state.percent ?? 0) / 10) : ""}`;
  if (key !== trayKey) {
    trayKey = key;
    rebuildTray();
  }
  const auto = settings?.system?.auto_update ?? true;
  if (state.status === "available" && auto) {
    void updater.download(); // скажем, когда будет готово к установке
    return;
  }
  if ((state.status === "available" || state.status === "downloaded") && Notification.isSupported()) {
    const id = `${state.status}:${state.version}`;
    if (updateNotified === id) return;
    updateNotified = id;
    const ready = state.status === "downloaded";
    const n = new Notification({
      title: "Jarvis",
      body: ready
        ? `Обновление ${state.version} готово. Нажмите, чтобы перезапустить Jarvis.`
        : `Доступна версия ${state.version}. Нажмите, чтобы скачать.`,
      icon: iconImage(),
    });
    n.on("click", () => (ready ? installUpdate() : void updater.download()));
    n.show();
  }
});

function applyAutostart() {
  if (!app.isPackaged) return;
  app.setLoginItemSettings({ openAtLogin: !!settings?.system?.autostart, args: ["--hidden"] });
}

// --- настройки от ядра --------------------------------------------------------------------------------------
function applySettings(next: any) {
  const prev = settings;
  settings = next;
  if (!prev || JSON.stringify(prev.interface) !== JSON.stringify(next.interface)) applyMaterial();
  syncOverlay();
  syncAvatar();
  rebuildTray();
  if (
    !prev ||
    JSON.stringify(prev.system) !== JSON.stringify(next.system) ||
    JSON.stringify(prev.plugins) !== JSON.stringify(next.plugins)
  ) {
    registerShortcuts();
    applyAutostart();
  }
}

client.on("hello", (data) => {
  applySettings(data.settings);
  void loadPluginHotkeys();
  if (!updatesStarted) {
    updatesStarted = true;
    setTimeout(checkUpdates, 15_000);
  }
});
client.on("settings.changed", (data) => applySettings(data.settings));
client.on("plugins.changed", () => void loadPluginHotkeys());
client.on("fullscreen.changed", (data) => {
  fullscreen = !!data.fullscreen;
  syncAvatar();
});
// «Джарвис, открой настройки» и т. п.: страница из команды Jarvis.ShowWindow → маршрут интерфейса
const UI_PAGES: Record<string, string> = { settings: "settings", commands: "commands", chat: "ai", ai: "ai", home: "home" };
client.on("ui.show", (data) => showMain(UI_PAGES[String(data?.page ?? "")]));
client.on("ui.hide", () => mainWin?.hide());
client.on("desk.open", () => {
  if (!mainWin?.isVisible()) showMain();
});

// --- IPC ---------------------------------------------------------------------------------------------------
ipcMain.handle("core:info", async () => {
  const info = await core.ready();
  return { url: info.url, token: info.token };
});

ipcMain.on("window:control", (e, action: string) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (!win) return;
  if (action === "minimize") win.minimize();
  else if (action === "maximize") (win.isMaximized() ? win.unmaximize() : win.maximize());
  else if (action === "close") win.close();
  else if (action === "hide") win.hide();
  else if (action === "show") showMain();
  else if (action === "quit") quit();
});

ipcMain.on("shell:open-external", (_e, url: string) => {
  if (/^(https?:|mailto:)/i.test(url)) shell.openExternal(url);
  else if (/^file:/i.test(url)) {
    try {
      shell.openPath(fileURLToPath(url));
    } catch {
      /* битая ссылка — открывать нечего */
    }
  }
});

ipcMain.handle("dialog:pick-file", async (e, options: any) => {
  const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
  const res = await dialog.showOpenDialog(win!, {
    title: options?.title ?? "Выберите файл",
    properties: [options?.folder ? "openDirectory" : "openFile"],
    filters: options?.filters,
  });
  return res.canceled ? null : res.filePaths[0] ?? null;
});

ipcMain.handle("app:file-icon", async (_e, file: string) => {
  try {
    const img = await app.getFileIcon(file, { size: "large" });
    return img.isEmpty() ? null : img.toDataURL();
  } catch {
    return null;
  }
});

// «Указать на экране» в редакторе команд: прозрачная пелена на каждом мониторе, щелчок — точка в пикселях экрана
// (как у ядра, которое понимает DPI), Esc или правая кнопка — отмена.
const PICK_HTML = `<!doctype html><meta charset="utf-8"><title></title><style>
html,body{margin:0;height:100%;cursor:crosshair;background:rgba(6,10,18,.22);user-select:none}
.hint{position:fixed;left:50%;top:28px;transform:translateX(-50%);padding:10px 16px;border-radius:12px;
background:rgba(20,26,38,.94);color:#fff;border:1px solid rgba(255,255,255,.14);box-shadow:0 10px 30px rgba(0,0,0,.45);
font:600 14px "Segoe UI",system-ui,sans-serif}.hint b{color:#60a5fa}</style>
<div class="hint">Щёлкните в нужной точке экрана · <b>Esc</b> — отмена</div>
<script>addEventListener("mousedown",e=>{e.preventDefault();document.title=e.button===0?"pick":"cancel"});
addEventListener("keydown",e=>{if(e.key==="Escape")document.title="cancel"});
addEventListener("contextmenu",e=>e.preventDefault());</script>`;
let picking: Promise<{ x: number; y: number } | null> | null = null;

function pickPoint(): Promise<{ x: number; y: number } | null> {
  if (picking) return picking;
  picking = new Promise((resolve) => {
    const wins: BrowserWindow[] = [];
    const mainShown = !!mainWin?.isVisible();
    let done = false;
    const finish = (point: { x: number; y: number } | null) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      for (const w of wins) if (!w.isDestroyed()) w.destroy();
      if (mainShown) showMain();
      picking = null;
      resolve(point);
    };
    const timer = setTimeout(() => finish(null), 60_000);
    mainWin?.hide(); // окно Jarvis не заслоняет то, во что нужно ткнуть
    for (const d of screen.getAllDisplays()) {
      const w = new BrowserWindow({
        ...d.bounds,
        frame: false,
        transparent: true,
        resizable: false,
        movable: false,
        minimizable: false,
        maximizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        hasShadow: false,
        alwaysOnTop: true,
        show: false,
        webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
      });
      wins.push(w);
      w.setAlwaysOnTop(true, "screen-saver");
      w.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      w.webContents.on("will-navigate", (e) => e.preventDefault());
      w.on("page-title-updated", (e, title) => {
        e.preventDefault();
        if (title === "cancel") finish(null);
        else if (title === "pick") {
          const dip = screen.getCursorScreenPoint();
          const px = process.platform === "win32" ? screen.dipToScreenPoint(dip) : dip;
          finish({ x: Math.round(px.x), y: Math.round(px.y) });
        }
      });
      w.on("closed", () => finish(null));
      w.once("ready-to-show", () => {
        w.show();
        w.focus();
      });
      w.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(PICK_HTML)}`);
    }
  });
  return picking;
}

ipcMain.handle("screen:pick-point", () => pickPoint());

// палитра буфера и быстрый поиск файлов просят клавиатуру на время ввода, потом отдают её обратно
ipcMain.on("overlay:focus", (_e, focus: boolean) => {
  if (!overlayWin) return;
  if (focus) {
    overlayWin.setFocusable(true);
    overlayWin.focus();
  } else {
    overlayWin.blur();
    overlayWin.setFocusable(false);
  }
});

// оверлей подписался на границы позже, чем они ушли при загрузке, — прислать ещё раз
ipcMain.on("overlay:ready", () => {
  overlayWin?.webContents.send("overlay:bounds", overlayBounds());
});

// обновления и состояние ядра для окна настроек и экрана «ядро не запускается»
ipcMain.handle("update:get", () => updater.state);
ipcMain.on("update:check", () => void updater.check());
ipcMain.on("update:download", () => void updater.download());
ipcMain.on("update:install", () => installUpdate());
ipcMain.handle("core:failure", () => core.failure());
ipcMain.on("core:restart", () => void core.restart());
ipcMain.on("logs:open", () => void shell.openPath(LOGS_DIR));

ipcMain.on("overlay:ignore-mouse", (_e, ignore: boolean) => {
  overlayWin?.setIgnoreMouseEvents(!!ignore, { forward: true });
});

ipcMain.on("main:show", () => showMain());

ipcMain.on("avatar:drag-start", () => {
  if (!avatarWin) return;
  const p = screen.getCursorScreenPoint();
  const [x, y] = avatarWin.getPosition();
  dragOffset = { x: p.x - x, y: p.y - y };
});

ipcMain.on("avatar:drag", () => {
  if (!avatarWin || !dragOffset) return;
  const p = screen.getCursorScreenPoint();
  avatarWin.setPosition(Math.round(p.x - dragOffset.x), Math.round(p.y - dragOffset.y));
});

ipcMain.on("avatar:drag-end", () => {
  if (!avatarWin || !dragOffset) return;
  dragOffset = null;
  const [x, y] = avatarWin.getPosition();
  patch({ avatar: { position: [x, y] } });
});

ipcMain.on("avatar:menu", () => {
  Menu.buildFromTemplate([
    ...quickMenu(),
    { type: "separator" },
    { label: "Вернуть аватар в угол", click: () => patch({ avatar: { position: null } }) },
    { label: "Скрыть аватар", click: () => patch({ avatar: { desktop: false } }) },
  ]).popup({ window: avatarWin ?? undefined });
});

// --- жизненный цикл ---------------------------------------------------------------------------------------
/** Перед выходом: хоткеи, связь и само ядро — python.exe держит файлы в папке программы, а установщик
 * обновления (по кнопке или при выходе) их заменяет. */
async function shutdown() {
  quitting = true;
  globalShortcut.unregisterAll();
  client.close();
  try {
    await core.stop();
  } finally {
    coreStopped = true;
  }
}

async function quit() {
  await shutdown();
  app.quit();
}

app.on("second-instance", () => showMain());

app.on("before-quit", (e) => {
  quitting = true;
  if (coreStopped) return;
  e.preventDefault(); // выход не через quit() — сперва остановить ядро
  void quit();
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  /* живём в трее */
});

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  core.onChange((info) => {
    for (const win of [mainWin, overlayWin, avatarWin]) {
      win?.webContents.send(info ? "core:ready" : "core:restarting");
    }
  });
  core.onFailed((failure) => mainWin?.webContents.send("core:failed", failure));
  core.start();
  createMain();
  createOverlay();
  rebuildTray();
  screen.on("display-metrics-changed", updateOverlayBounds);
  screen.on("display-added", updateOverlayBounds);
  screen.on("display-removed", updateOverlayBounds);
  setInterval(checkUpdates, 6 * 3600_000);
});
