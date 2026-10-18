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
let settings: any = null;
let fullscreen = false;
const registered: string[] = [];
let update: { version: string; url: string } | null = null;
const RELEASES_API = "https://api.github.com/repos/BreezeAreaSay/Jarvis-Agent/releases/latest";

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

function guard(win: BrowserWindow) {
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
  guard(mainWin);
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
  win.focus();
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
  guard(overlayWin);
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
  guard(avatarWin);
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
    { type: "separator" },
    {
      label: "Микрофон",
      type: "checkbox",
      checked: !!settings?.voice?.enabled,
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
      ...(update
        ? ([{ label: `Скачать обновление ${update.version}`, click: () => shell.openExternal(update!.url) }] as MenuItemConstructorOptions[])
        : []),
      { label: "Перезапустить ядро", click: () => core.restart() },
      { label: "Папка журналов", click: () => shell.openPath(path.join(app.getPath("userData"), "logs")) },
      { type: "separator" },
      { label: "Выход", click: () => quit() },
    ]),
  );
}

// --- горячие клавиши и автозапуск ------------------------------------------------------------------------
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
  bind(settings?.system?.hotkey_listen, () => api("POST", "/api/voice/listen"));
  const plugins = settings?.plugins ?? {};
  for (const [id, state] of Object.entries<any>(plugins)) {
    const hotkey = state?.enabled ? state?.config?.hotkey : "";
    if (hotkey) bind(String(hotkey), () => api("POST", `/api/plugins/${id}/action`, { action: "toggle", args: {} }));
  }
}

// --- обновления: только проверка и ссылка, скачивает и ставит человек ------------------------------------------
function newer(a: string, b: string): boolean {
  const pa = a.replace(/^v/, "").split(".").map((x) => Number.parseInt(x, 10) || 0);
  const pb = b.replace(/^v/, "").split(".").map((x) => Number.parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) > (pb[i] ?? 0);
  }
  return false;
}

async function checkUpdates() {
  if (!app.isPackaged || !(settings?.system?.check_updates ?? true) || update) return;
  try {
    const res = await fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) return;
    const data = (await res.json()) as { tag_name?: string; html_url?: string };
    if (data.tag_name && data.html_url && newer(data.tag_name, VERSION)) {
      update = { version: data.tag_name, url: data.html_url };
      rebuildTray();
      if (Notification.isSupported()) {
        const n = new Notification({ title: "Jarvis", body: `Доступна версия ${data.tag_name}. Нажмите, чтобы скачать.`, icon: iconImage() });
        n.on("click", () => shell.openExternal(update!.url));
        n.show();
      }
    }
  } catch {
    /* нет сети — проверим в следующий раз */
  }
}

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
  setTimeout(checkUpdates, 15_000);
});
client.on("settings.changed", (data) => applySettings(data.settings));
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

// оверлей подписался на границы позже, чем они ушли при загрузке, — прислать ещё раз
ipcMain.on("overlay:ready", () => {
  overlayWin?.webContents.send("overlay:bounds", overlayBounds());
});

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
async function quit() {
  quitting = true;
  globalShortcut.unregisterAll();
  client.close();
  await core.stop();
  app.quit();
}

app.on("second-instance", () => showMain());

app.on("before-quit", () => {
  quitting = true;
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
  core.start();
  createMain();
  createOverlay();
  rebuildTray();
  screen.on("display-metrics-changed", updateOverlayBounds);
  screen.on("display-added", updateOverlayBounds);
  screen.on("display-removed", updateOverlayBounds);
  setInterval(checkUpdates, 6 * 3600_000);
});
