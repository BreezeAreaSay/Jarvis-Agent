// Маршруты по hash: #/home, #/commands/edit/<id>, #/ai/<chat>, #/settings/voice … и отдельные окна #/overlay, #/avatar.
import { useSyncExternalStore } from "react";

function current(): string[] {
  const hash = location.hash.replace(/^#\/?/, "");
  return hash ? hash.split("/").map(decodeURIComponent) : [];
}

let cache = current();
let cacheHash = location.hash;
const listeners = new Set<() => void>();
window.addEventListener("hashchange", () => {
  cache = current();
  cacheHash = location.hash;
  for (const l of listeners) l();
});

export function useRoute(): string[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => (location.hash === cacheHash ? cache : (cache = current())),
  );
}

export function navigate(...parts: (string | undefined)[]) {
  const path = parts.filter((p): p is string => !!p).map(encodeURIComponent).join("/");
  location.hash = `#/${path}`;
}

export function windowKind(): "main" | "overlay" | "avatar" {
  const first = current()[0];
  return first === "overlay" ? "overlay" : first === "avatar" ? "avatar" : "main";
}
