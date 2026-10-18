// Поток событий ядра (WebSocket /ws) с переподключением. Подписка: onEvent("voice.*", cb).
import { coreToken, coreUrl } from "./client";
import type { CoreEvent } from "./types";

type Listener = (event: CoreEvent<any>) => void;

const listeners = new Map<string, Set<Listener>>();
const statusListeners = new Set<(connected: boolean) => void>();
let socket: WebSocket | null = null;
let connected = false;
let retry = 0;
let started = false;

function matches(pattern: string, type: string): boolean {
  if (pattern === "*" || pattern === type) return true;
  if (pattern.endsWith("*")) return type.startsWith(pattern.slice(0, -1));
  return false;
}

function emit(event: CoreEvent) {
  for (const [pattern, set] of listeners) {
    if (!matches(pattern, event.type)) continue;
    for (const fn of set) {
      try {
        fn(event);
      } catch (e) {
        console.error("обработчик события упал", event.type, e);
      }
    }
  }
}

function setConnected(value: boolean) {
  if (connected === value) return;
  connected = value;
  for (const fn of statusListeners) fn(value);
}

function connect() {
  const url = coreUrl().replace(/^http/, "ws") + `/ws?token=${encodeURIComponent(coreToken())}`;
  const ws = new WebSocket(url);
  socket = ws;
  ws.onopen = () => {
    retry = 0;
    setConnected(true);
  };
  ws.onmessage = (msg) => {
    try {
      emit(JSON.parse(msg.data as string) as CoreEvent);
    } catch (e) {
      console.error("плохое событие ядра", e);
    }
  };
  ws.onclose = () => {
    setConnected(false);
    if (socket === ws) {
      socket = null;
      const delay = Math.min(5000, 300 * 2 ** retry++);
      setTimeout(connect, delay);
    }
  };
  ws.onerror = () => ws.close();
}

export function startEvents() {
  if (started) return;
  started = true;
  connect();
  // пинг держит соединение живым через прокси и спящий режим
  setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
  }, 25000);
}

export function onEvent<T = any>(pattern: string, listener: (event: CoreEvent<T>) => void): () => void {
  let set = listeners.get(pattern);
  if (!set) listeners.set(pattern, (set = new Set()));
  set.add(listener as Listener);
  return () => set!.delete(listener as Listener);
}

export function onConnection(listener: (connected: boolean) => void): () => void {
  statusListeners.add(listener);
  listener(connected);
  return () => statusListeners.delete(listener);
}

export function isConnected(): boolean {
  return connected;
}
