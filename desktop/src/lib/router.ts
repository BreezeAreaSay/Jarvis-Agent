// Маршруты по hash: #/home, #/commands/edit/<id>, #/ai/<chat>, #/settings/voice … и отдельные окна #/overlay, #/avatar.
import { useSyncExternalStore } from "react";

function current(): string[] {
  const hash = location.hash.replace(/^#\/?/, "");
  return hash ? hash.split("/").map(decodeURIComponent) : [];
}

let cache = current();
let cacheHash = location.hash;
const listeners = new Set<() => void>();

/** Маршрут — один и тот же массив, пока не сменился адрес. После location.replace() адрес уже новый, а
 * hashchange ещё не пришёл: если тут не запомнить и адрес, каждое чтение даёт новый массив, React считает это
 * изменением и зацикливается (пустое окно при возврате в «Jarvis AI» к последнему чату). */
function snapshot(): string[] {
  if (location.hash !== cacheHash) {
    cacheHash = location.hash;
    cache = current();
  }
  return cache;
}

window.addEventListener("hashchange", () => {
  snapshot();
  for (const l of listeners) l();
});

export function useRoute(): string[] {
  return useSyncExternalStore((cb) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }, snapshot);
}

export function navigate(...parts: (string | undefined)[]) {
  const path = parts.filter((p): p is string => !!p).map(encodeURIComponent).join("/");
  location.hash = `#/${path}`;
}

export function windowKind(): "main" | "overlay" | "avatar" {
  const first = current()[0];
  return first === "overlay" ? "overlay" : first === "avatar" ? "avatar" : "main";
}
