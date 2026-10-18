// «Умный буфер» на карточке плагина: история буфера с поиском (закрепить, скопировать, удалить, очистить)
// и ИИ для выбранной записи — перевести, исправить, сократить… Итог появляется здесь же, с «Копировать».
// Данные — действие list при открытии, дальше события plugin.clipboard.items (pluginStore.live).
import "@/styles/clipboard.css";
import { ClipboardList, Copy, Eraser, Pin, PinOff, Search, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ClipboardItem, ClipboardItems } from "@/api/types";
import { CLIPBOARD_ACTIONS, charsLabel, matchesQuery, oneLine } from "@/lib/clipboard";
import { useApp } from "@/store/app";
import { Button, cx, IconButton, notify, notifyError, timeAgo } from "@/ui";
import type { Plugin } from "./pluginKit";
import { usePlugins } from "./pluginStore";

type Filter = "all" | "pinned";

interface AiResult {
  action: string;
  title: string;
  text: string;
}

function isItems(v: unknown): v is ClipboardItems {
  return !!v && typeof v === "object" && Array.isArray((v as ClipboardItems).items);
}

export function ClipboardLive({ plugin }: { plugin: Plugin }) {
  const running = plugin.enabled && plugin.status?.running === true;
  const live = usePlugins((s) => s.live[plugin.id]?.items);
  const act = usePlugins((s) => s.action);
  const aiReady = useApp((s) => s.aiConfigured);
  const [data, setData] = useState<ClipboardItems | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<AiResult | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const clearTimer = useRef(0);

  useEffect(() => {
    if (!running) {
      setData(null);
      return;
    }
    let alive = true;
    act(plugin.id, "list")
      .then((res) => alive && isItems(res) && setData(res))
      .catch((e) => alive && notifyError(e));
    return () => {
      alive = false;
    };
  }, [running, plugin.id, act]);

  useEffect(() => {
    const d = live?.data;
    if (running && isItems(d)) setData((prev) => ({ ...d, skipped: prev?.skipped ?? d.skipped }));
  }, [live, running]);

  useEffect(() => () => clearTimeout(clearTimer.current), []);

  const items = data?.items ?? [];
  const pinnedCount = items.filter((i) => i.pinned).length;
  const shown = useMemo(
    () => items.filter((i) => (filter === "all" || i.pinned) && matchesQuery(i.text, query)),
    [items, filter, query],
  );
  const current = items.find((i) => i.id === selected) ?? null;
  const target = current ?? shown[0] ?? null;
  const skipped = Number(plugin.status?.skipped ?? data?.skipped ?? 0) || 0;
  const hotkey = typeof plugin.config?.hotkey === "string" ? plugin.config.hotkey : "";

  const run = async (name: string, args: Record<string, unknown>) => {
    try {
      return await act(plugin.id, name, args);
    } catch (e) {
      notifyError(e);
      return null;
    }
  };

  const patchItems = (fn: (list: ClipboardItem[]) => ClipboardItem[]) =>
    setData((d) => (d ? { ...d, items: fn(d.items) } : d));

  const pin = (item: ClipboardItem) => {
    patchItems((list) => list.map((i) => (i.id === item.id ? { ...i, pinned: !item.pinned } : i)));
    void run("pin", { id: item.id, pinned: !item.pinned });
  };
  const copy = async (item: ClipboardItem) => {
    if (await run("copy", { id: item.id })) notify("Скопировано в буфер", "ok");
  };
  const remove = (item: ClipboardItem) => {
    patchItems((list) => list.filter((i) => i.id !== item.id));
    if (selected === item.id) setSelected(null);
    void run("delete", { id: item.id });
  };
  const clear = async () => {
    if (!confirmClear) {
      setConfirmClear(true);
      clearTimeout(clearTimer.current);
      clearTimer.current = window.setTimeout(() => setConfirmClear(false), 3500);
      return;
    }
    setConfirmClear(false);
    const res = await run("clear", {});
    if (res) {
      patchItems((list) => list.filter((i) => i.pinned));
      notify(Number(res.kept) ? "История очищена — закреплённые остались" : "История очищена", "ok");
    }
  };
  const runAi = async (id: string) => {
    if (!target || busy) return;
    setBusy(id);
    try {
      const res = await act(plugin.id, "ai", { action: id, id: target.id });
      if (id === "read") notify("Читаю вслух", "info");
      else setResult({ action: id, title: String(res.title ?? ""), text: String(res.text ?? "") });
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy("");
    }
  };
  const copyResult = async () => {
    if (result && (await run("copy", { text: result.text }))) notify("Итог скопирован в буфер", "ok");
  };

  if (!running) {
    return (
      <div className="lv lv-center">
        <div className="lv-round">
          <ClipboardList />
        </div>
        <div className="lv-caption-text">
          {plugin.enabled
            ? "Запускаю слежение за буфером…"
            : `Включите — Jarvis начнёт запоминать, что вы копируете (пароли — нет), а ${hotkey || "Ctrl+Alt+V"} откроет палитру с историей и ИИ`}
        </div>
      </div>
    );
  }

  return (
    <div className="lv cb-live">
      <div className="cb-bar">
        <div className="input-wrap grow">
          <Search />
          <input
            className="input"
            value={query}
            placeholder="Поиск по истории…"
            spellCheck={false}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="cb-filters" role="tablist" aria-label="Какие записи показать">
          {(["all", "pinned"] as const).map((f) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              title={f === "all" ? "Все записи" : "Закреплённые"}
              className={cx("cb-filter", filter === f && "on")}
              onClick={() => setFilter(f)}
            >
              {f === "all" ? "Все" : <Pin />} <b>{f === "all" ? items.length : pinnedCount}</b>
            </button>
          ))}
        </div>
        <Button
          size="sm"
          variant={confirmClear ? "danger" : "ghost"}
          icon={<Eraser />}
          disabled={!items.some((i) => !i.pinned)}
          onClick={clear}
          title="Закреплённые записи останутся"
        >
          {confirmClear ? "Точно очистить?" : "Очистить"}
        </Button>
      </div>

      <div className="cb-list" role="listbox" aria-label="История буфера">
        {data === null ? (
          <div className="cb-empty">
            <span className="spinner" />
          </div>
        ) : shown.length ? (
          shown.map((item) => (
            <div
              key={item.id}
              role="option"
              aria-selected={item.id === selected}
              tabIndex={0}
              className={cx("cb-row", item.id === selected && "on", item.pinned && "pinned")}
              onClick={() => setSelected(item.id === selected ? null : item.id)}
              onKeyDown={(e) => e.key === "Enter" && setSelected(item.id === selected ? null : item.id)}
            >
              <div className="cb-row-main">
                <div className="cb-row-text">{oneLine(item.text, 320) || "—"}</div>
                <div className="cb-row-meta">
                  {item.pinned && <Pin className="cb-pin-mark" />}
                  {timeAgo(item.ts)} · {charsLabel(item.chars)}
                </div>
              </div>
              <div className="cb-row-tools" onClick={(e) => e.stopPropagation()}>
                <IconButton
                  size="sm"
                  variant="ghost"
                  active={item.pinned}
                  icon={item.pinned ? <PinOff /> : <Pin />}
                  label={item.pinned ? "Открепить" : "Закрепить"}
                  onClick={() => pin(item)}
                />
                <IconButton size="sm" variant="ghost" icon={<Copy />} label="Скопировать" onClick={() => copy(item)} />
                <IconButton size="sm" variant="ghost" icon={<Trash2 />} label="Удалить" onClick={() => remove(item)} />
              </div>
            </div>
          ))
        ) : (
          <div className="cb-empty">
            {query || filter === "pinned"
              ? query
                ? "Ничего не нашлось"
                : "Закреплённых пока нет — нажмите на булавку у записи"
              : "Скопируйте что-нибудь — запись появится здесь"}
          </div>
        )}
      </div>

      <div className="cb-ai">
        <div className="cb-ai-head">
          <Sparkles />
          <span className="ellipsis">
            {target ? (
              <>
                ИИ для {current ? "записи" : "последней записи"}: <b>«{oneLine(target.text, 60)}»</b>
              </>
            ) : (
              "ИИ для записи из истории"
            )}
          </span>
          {!aiReady && <span className="cb-ai-note">ИИ не подключён</span>}
        </div>
        <div className="cb-chips">
          {CLIPBOARD_ACTIONS.map((a) => (
            <button
              key={a.id}
              type="button"
              className={cx("cb-chip", busy === a.id && "busy")}
              disabled={!target || (!!busy && busy !== a.id)}
              onClick={() => runAi(a.id)}
            >
              {busy === a.id ? <span className="spinner" /> : <a.icon />}
              {a.title}
            </button>
          ))}
        </div>
        {result && (
          <div className="cb-result">
            <div className="cb-result-head">
              <b>{result.title}</b>
              <span className="spacer" />
              <Button size="sm" variant="soft" icon={<Copy />} onClick={copyResult}>
                Копировать
              </Button>
              <IconButton size="sm" variant="ghost" icon={<X />} label="Закрыть" onClick={() => setResult(null)} />
            </div>
            <div className="cb-result-text">{result.text}</div>
          </div>
        )}
      </div>

      <div className="cb-foot">
        <ShieldCheck />
        <span>
          Пароли из менеджеров паролей не запоминаются{skipped ? ` · пропущено: ${skipped}` : ""}
        </span>
        {hotkey && (
          <span className="cb-foot-key">
            <span className="kbd">{hotkey}</span> — палитра в любой программе
          </span>
        )}
      </div>
    </div>
  );
}
