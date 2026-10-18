// Мост к Electron (preload). В браузере (режим разработки/демо) — запасные значения из адресной строки:
// ?core=http://127.0.0.1:8799&token=devtoken

export interface JarvisBridge {
  core: () => Promise<{ url: string; token: string } | null>;
  windowControl: (action: "minimize" | "maximize" | "close" | "hide" | "show" | "quit") => void;
  setIgnoreMouse: (ignore: boolean) => void;
  openExternal: (url: string) => void;
  pickFile: (options: { title?: string; filters?: { name: string; extensions: string[] }[]; folder?: boolean }) => Promise<string | null>;
  fileIcon: (path: string) => Promise<string | null>;
  on: (channel: string, listener: (...args: unknown[]) => void) => () => void;
  send: (channel: string, ...args: unknown[]) => void;
  platform: string;
  version: string;
}

declare global {
  interface Window {
    jarvis?: JarvisBridge;
  }
}

export const isElectron = typeof window !== "undefined" && !!window.jarvis;

export const bridge: JarvisBridge = window.jarvis ?? {
  core: async () => {
    const q = new URLSearchParams(location.search);
    const url = q.get("core") ?? import.meta.env.VITE_CORE_URL ?? "http://127.0.0.1:8799";
    const token = q.get("token") ?? import.meta.env.VITE_CORE_TOKEN ?? "devtoken";
    return { url, token };
  },
  windowControl: () => undefined,
  setIgnoreMouse: () => undefined,
  openExternal: (url) => window.open(url, "_blank", "noopener"),
  pickFile: async () => null,
  fileIcon: async () => null,
  on: () => () => undefined,
  send: () => undefined,
  platform: "web",
  version: "2.0.0",
};
