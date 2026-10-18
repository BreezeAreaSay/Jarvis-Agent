// Шина событий оверлея: события ядра (WebSocket) плюс подставные — для демо-режима и своих тостов оверлея.
import { useEffect, useLayoutEffect, useRef } from "react";
import { onEvent } from "@/api/events";

type Handler = (data: any) => void;
const local = new Map<string, Set<Handler>>();

/** Подписка на событие ядра (и на такое же подставное). Вернёт отписку. */
export function onOverlayEvent<T = any>(type: string, fn: (data: T) => void): () => void {
  const off = onEvent<T>(type, (e) => fn((e.data ?? {}) as T));
  let set = local.get(type);
  if (!set) local.set(type, (set = new Set()));
  const handler = fn as Handler;
  set.add(handler);
  return () => {
    off();
    set.delete(handler);
  };
}

/** Подставное событие: демо-сцены и собственные уведомления оверлея (ошибки кнопок). */
export function emitOverlay(type: string, data: unknown = {}) {
  for (const fn of [...(local.get(type) ?? [])]) {
    try {
      fn(data);
    } catch (e) {
      console.error("обработчик события оверлея упал", type, e);
    }
  }
}

/** Подписка из компонента; обработчик всегда свежий, переподписки нет. */
export function useOverlayEvent<T = any>(type: string, fn: (data: T) => void) {
  const ref = useRef(fn);
  useLayoutEffect(() => {
    ref.current = fn;
  });
  useEffect(() => onOverlayEvent<T>(type, (d) => ref.current(d)), [type]);
}
