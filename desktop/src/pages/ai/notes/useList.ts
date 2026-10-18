// Список с ядра (GET → {items}) с перезагрузкой по событиям *.changed.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import { errText } from "../util";

export function useList<T>(path: string, events: string[]) {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const current = useRef(path);
  current.current = path;

  const load = useCallback(async () => {
    const asked = path;
    try {
      const res = await api.get<{ items: T[] }>(asked);
      if (current.current !== asked) return;
      setItems(res?.items ?? []);
      setError("");
    } catch (e) {
      if (current.current === asked) setError(errText(e));
    } finally {
      if (current.current === asked) setLoading(false);
    }
  }, [path]);

  const eventsKey = events.join(",");
  useEffect(() => {
    setLoading(true);
    load();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(load, 120);
    };
    const offs = eventsKey ? eventsKey.split(",").map((ev) => onEvent(ev, soon)) : [];
    return () => {
      clearTimeout(timer);
      for (const off of offs) off();
    };
  }, [load, eventsKey]);

  return { items, setItems, error, loading, reload: load };
}
