// Мост между окнами интерфейса и главным процессом Electron (window.jarvis). Только разрешённые каналы.
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

const LISTEN = new Set([
  "overlay:bounds",
  "overlay:pointer",
  "overlay:main-focus",
  "window:material",
  "window:focus",
  "core:restarting",
  "core:ready",
  "core:failed",
  "update:state",
  "navigate",
]);
const SEND = new Set([
  "avatar:drag-start",
  "avatar:drag",
  "avatar:drag-end",
  "avatar:menu",
  "main:show",
  "overlay:ignore-mouse",
  "overlay:ready",
  "overlay:focus",
  "core:restart",
  "logs:open",
  "update:check",
  "update:download",
  "update:install",
]);

function arg(name: string): string {
  const prefix = `--${name}=`;
  const found = process.argv.find((a) => a.startsWith(prefix));
  return found ? found.slice(prefix.length) : "";
}

contextBridge.exposeInMainWorld("jarvis", {
  core: () => ipcRenderer.invoke("core:info"),
  windowControl: (action: string) => ipcRenderer.send("window:control", action),
  setIgnoreMouse: (ignore: boolean) => ipcRenderer.send("overlay:ignore-mouse", !!ignore),
  openExternal: (url: string) => ipcRenderer.send("shell:open-external", String(url)),
  pickFile: (options: unknown) => ipcRenderer.invoke("dialog:pick-file", options),
  fileIcon: (path: string) => ipcRenderer.invoke("app:file-icon", String(path)),
  pickPoint: () => ipcRenderer.invoke("screen:pick-point"),
  coreFailure: () => ipcRenderer.invoke("core:failure"),
  updateState: () => ipcRenderer.invoke("update:get"),
  on: (channel: string, listener: (...args: unknown[]) => void) => {
    if (!LISTEN.has(channel)) return () => undefined;
    const wrapped = (_: IpcRendererEvent, ...args: unknown[]) => listener(...args);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
  send: (channel: string, ...args: unknown[]) => {
    if (SEND.has(channel)) ipcRenderer.send(channel, ...args);
  },
  platform: process.platform,
  version: arg("jarvis-version") || "2.0.0",
});
