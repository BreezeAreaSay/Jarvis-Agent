// «Умный буфер» поверх экрана. Палитра у курсора (горячая клавиша плагина → plugin.clipboard.palette): поиск
// по истории, ↑/↓ — выбор, Enter или щелчок — вставить запись в окно, где вы работали, Esc — закрыть; ряд
// действий ИИ для выделенного текста. Карточка итога ИИ (plugin.clipboard.result): «Вставить» (на место
// выделенного), «Копировать», «Закрыть». Обе ловят мышь ([data-interactive], см. clickthrough.ts); клавиатуру
// палитре даёт Electron, пока она открыта (overlay:focus), — без неё палитрой можно пользоваться мышью.
import "@/styles/clipboard.css";
import {
  Check,
  ClipboardList,
  Copy,
  CornerDownLeft,
  Mail,
  MousePointer2,
  Pin,
  Search,
  Sparkles,
  TriangleAlert,
  Volume2,
  X,
} from "lucide-react";
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClipboardItem, ClipboardPalette, ClipboardResult } from "@/api/types";
import { bridge } from "@/lib/bridge";
import { CLIPBOARD_ACTIONS, charsLabel, clipboardActionIcon, matchesQuery, oneLine } from "@/lib/clipboard";
import { cx, timeAgo } from "@/ui";
import { emitOverlay, useOverlayEvent } from "./bus";
import { type Bounds, DEMO, Fade, pluginAction, useBounds, useUiZoom } from "./common";

const PALETTE_W = 400;
const PALETTE_H = 530; // высота палитры с полным списком и выделенным текстом (clipboard.css)
const CARD_W = 380;
const CARD_H = 340;
const GAP = 14;
const EDGE = 12;
const MIN_ROOM = 260; // меньше места с обеих сторон курсора — карточка встаёт у края экрана
const IDLE_MS = 60_000; // палитра без дела закрывается сама
const REPLACED_MS = 1800;
const ERROR_MS = 9000;

type At = { x: number; y: number } | null | undefined;

/** Курсор (физические пиксели экрана) → точка оверлея в CSS-пикселях корня (с масштабом интерфейса). */
function toLocal(at: At, b: Bounds, zoom: number): { x: number; y: number } | null {
  if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) return null;
  const scale = b.scale || 1;
  return { x: (at.x / scale - b.x) / zoom, y: (at.y / scale - b.y) / zoom };
}

/** Место карточки у курсора: справа-снизу, у края экрана — с другой стороны. Карточка над курсором привязана
 *  низом: растёт вверх, а не наезжает на то, что выделено. Места мало с обеих сторон — карточка ниже ростом
 *  (список прокручивается). Курсора нет — сверху по центру. */
function usePlacement(at: At, width: number, height: number): CSSProperties {
  const bounds = useBounds();
  const zoom = useUiZoom();
  const vw = window.innerWidth / zoom;
  const vh = window.innerHeight / zoom;
  const p = toLocal(at, bounds, zoom);
  if (!p) {
    const top = Math.min(96, vh / 6);
    return { left: Math.max(EDGE, (vw - width) / 2), top, maxHeight: vh - top - EDGE };
  }
  const x = Math.min(Math.max(p.x, 0), vw);
  const y = Math.min(Math.max(p.y, 0), vh);
  const left = x + GAP + width <= vw - EDGE ? x + GAP : Math.max(EDGE, x - GAP - width);
  const below = vh - EDGE - (y + GAP);
  const above = y - GAP - EDGE;
  if (below >= height) return { left, top: y + GAP };
  if (above >= height) return { left, bottom: vh - y + GAP };
  if (Math.max(below, above) >= MIN_ROOM) {
    return below >= above
      ? { left, top: y + GAP, maxHeight: below }
      : { left, bottom: vh - y + GAP, maxHeight: above };
  }
  const h = Math.min(height, vh - 2 * EDGE);
  return { left, top: Math.max(EDGE, vh - EDGE - h), maxHeight: h };
}

// --- палитра --------------------------------------------------------------------------------------------
type PaletteData = ClipboardPalette & { key: number };

function Palette() {
  const [p, setP] = useState<PaletteData | null>(null);
  const [show, setShow] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const open = useRef(false);
  const hadFocus = useRef(false);
  const idle = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    if (!open.current) return;
    open.current = false;
    setShow(false);
    clearTimeout(idle.current);
    bridge.send("overlay:focus", false);
  }, []);

  /** Что-то нажали или повели мышью — отложить автозакрытие. */
  const poke = useCallback(() => {
    clearTimeout(idle.current);
    idle.current = window.setTimeout(close, IDLE_MS);
  }, [close]);

  useOverlayEvent<ClipboardPalette>("plugin.clipboard.palette", (d) => {
    if (open.current) {
      close(); // та же горячая клавиша ещё раз — закрыть
      return;
    }
    if (!d || !Array.isArray(d.items)) return;
    setP({ ...d, key: Date.now() });
    setQuery("");
    setActive(0);
    open.current = true;
    hadFocus.current = false;
    setShow(true);
    poke();
    bridge.send("overlay:focus", true);
  });
  useOverlayEvent<{ items?: ClipboardItem[] }>("plugin.clipboard.items", (d) => {
    const items = d?.items;
    if (Array.isArray(items)) setP((prev) => (prev ? { ...prev, items } : prev));
  });
  useEffect(() => () => clearTimeout(idle.current), []);

  const shown = useMemo(() => (p ? p.items.filter((i) => matchesQuery(i.text, query)) : []), [p, query]);
  const current = Math.min(active, Math.max(0, shown.length - 1));

  const paste = useCallback(
    (item: ClipboardItem | undefined) => {
      if (!item) return;
      close(); // окно отдаёт клавиатуру — ядро вернёт фокус программе и вставит
      void pluginAction("clipboard", "paste", { id: item.id });
    },
    [close],
  );
  const runAi = (id: string) => {
    close();
    void pluginAction("clipboard", "ai", { action: id });
  };

  // поле поиска — в фокус; щелчок в другую программу (окно потеряло фокус) закрывает палитру
  useEffect(() => {
    if (!show) return;
    const t = window.setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 30);
    // событие focus могло прийти раньше подписки — проверить и так
    const check = window.setTimeout(() => document.hasFocus() && (hadFocus.current = true), 250);
    const onFocus = () => {
      hadFocus.current = true;
    };
    const onBlur = () => {
      if (hadFocus.current) close();
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    return () => {
      clearTimeout(t);
      clearTimeout(check);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
    };
  }, [show, close]);

  useEffect(() => {
    if (!show) return;
    const onKey = (e: KeyboardEvent) => {
      poke();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive(Math.max(0, Math.min(shown.length - 1, current + step)));
      } else if (e.key === "Enter") {
        e.preventDefault();
        paste(shown[current]);
      } else if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [show, shown, current, paste, close, poke]);

  // выбранная строка — в видимой части списка
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${current}"]`)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const style = usePlacement(p?.at, PALETTE_W, PALETTE_H);
  const selection = p?.selection?.trim() ? p.selection : "";
  return (
    <Fade show={show && !!p}>
      {(leaving) => (
        <div
          className={cx("ov-card ov-cbp ov-anim", leaving && "leaving")}
          style={style}
          data-interactive
          role="dialog"
          aria-label="Умный буфер"
          onMouseMove={poke}
        >
          <div className="ov-cb-head">
            <span className="ov-cb-tile">
              <ClipboardList />
            </span>
            <div className="grow">
              <div className="ov-cb-title">Умный буфер</div>
              <div className="ov-cb-sub ellipsis">
                {p?.target?.title ? `Вставлю в «${oneLine(p.target.title, 44)}»` : "История и ИИ для выделенного"}
              </div>
            </div>
            {p?.hotkey && <span className="kbd ov-cb-hotkey">{p.hotkey}</span>}
            <button type="button" className="ov-icon-btn ov-cb-x" aria-label="Закрыть" title="Закрыть" onClick={close}>
              <X />
            </button>
          </div>

          <label className="ov-cbp-search">
            <Search />
            <input
              ref={inputRef}
              value={query}
              placeholder="Найти в истории…"
              spellCheck={false}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
              }}
            />
          </label>

          <div className="ov-cbp-list" ref={listRef} role="listbox" aria-label="История буфера">
            {shown.length ? (
              shown.map((item, i) => (
                <button
                  key={item.id}
                  type="button"
                  role="option"
                  aria-selected={i === current}
                  data-index={i}
                  className={cx("ov-cbp-row", i === current && "on")}
                  onMouseMove={() => i !== current && setActive(i)}
                  onClick={() => paste(item)}
                >
                  <span className="ov-cbp-row-text">{oneLine(item.text, 160) || "—"}</span>
                  <span className="ov-cbp-row-meta">
                    {item.pinned && <Pin />}
                    {timeAgo(item.ts)}
                  </span>
                </button>
              ))
            ) : (
              <div className="ov-cbp-empty">{query ? "Ничего не нашлось" : "История пуста — скопируйте что-нибудь"}</div>
            )}
          </div>

          <div className={cx("ov-cbp-sel", !selection && "empty")}>
            {selection && p ? (
              <>
                <div className="ov-cbp-sel-head">
                  <Sparkles /> Выделено · {charsLabel(p.selection_chars || selection.length)}
                  <button type="button" className="ov-cbp-read" title="Прочитать вслух" onClick={() => runAi("read")}>
                    <Volume2 /> Прочитать
                  </button>
                </div>
                <div className="ov-cbp-sel-text">«{oneLine(selection, 150)}»</div>
                <div className="ov-cb-chips">
                  {CLIPBOARD_ACTIONS.filter((a) => a.id !== "read").map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      className="ov-cb-chip"
                      disabled={!p.ai}
                      title={p.ai ? undefined : "Подключите ИИ в настройках"}
                      onClick={() => runAi(a.id)}
                    >
                      <a.icon />
                      {a.title}
                    </button>
                  ))}
                </div>
                {!p.ai && <div className="ov-cbp-hint">Подключите ИИ в настройках — заработают перевод, правка и ответы</div>}
              </>
            ) : (
              <div className="ov-cbp-hint">
                <Sparkles /> Выделите текст в программе — здесь появятся перевод, правка и ответ ИИ
              </div>
            )}
          </div>

          <div className="ov-cbp-foot">
            <span>
              <span className="kbd">↑</span>
              <span className="kbd">↓</span> выбрать
            </span>
            <span>
              <span className="kbd">
                <CornerDownLeft />
              </span>{" "}
              вставить
            </span>
            <span>
              <span className="kbd">Esc</span> закрыть
            </span>
          </div>
        </div>
      )}
    </Fade>
  );
}

// --- карточка итога ИИ ----------------------------------------------------------------------------------
function ResultCard() {
  const [c, setC] = useState<ClipboardResult | null>(null);
  const [show, setShow] = useState(false);
  const [copied, setCopied] = useState(false);
  const latest = useRef("");
  const dismissed = useRef("");
  const timer = useRef(0);

  useOverlayEvent<ClipboardResult>("plugin.clipboard.result", (d) => {
    if (!d?.id || d.id === dismissed.current) return;
    if (d.state === "working") latest.current = d.id;
    else if (latest.current && latest.current !== d.id) return; // итог прошлого запроса — уже не нужен
    setC(d);
    setShow(true);
    setCopied(false);
    clearTimeout(timer.current);
    if (d.state === "replaced") timer.current = window.setTimeout(() => setShow(false), REPLACED_MS);
    else if (d.state === "error") timer.current = window.setTimeout(() => setShow(false), ERROR_MS);
  });
  useEffect(() => () => clearTimeout(timer.current), []);

  const close = () => {
    clearTimeout(timer.current);
    if (c) dismissed.current = c.id;
    setShow(false);
  };
  const insert = () => {
    if (!c?.text) return;
    close();
    void pluginAction("clipboard", "paste", { text: c.text });
  };
  const copy = async () => {
    if (!c?.text) return;
    const res = await pluginAction("clipboard", "copy", { text: c.text });
    if (res !== null || DEMO) setCopied(true);
  };

  const style = usePlacement(c?.at, CARD_W, CARD_H);
  const Icon = clipboardActionIcon(c?.action ?? "");
  return (
    <Fade show={show && !!c}>
      {(leaving) =>
        c && (
          <div
            className={cx("ov-card ov-cbr ov-anim", leaving && "leaving", `is-${c.state}`)}
            style={style}
            data-interactive
            role="dialog"
            aria-label={c.title}
          >
            <div className="ov-cb-head">
              <span className="ov-cb-tile">
                {c.state === "working" ? (
                  <span className="spinner" />
                ) : c.state === "error" ? (
                  <TriangleAlert />
                ) : c.state === "replaced" ? (
                  <Check />
                ) : (
                  <Icon />
                )}
              </span>
              <div className="grow">
                <div className="ov-cb-title ellipsis">{c.state === "replaced" ? "Готово — заменил выделенное" : c.title}</div>
                {c.source && <div className="ov-cb-sub ellipsis">«{oneLine(c.source, 90)}»</div>}
              </div>
              <button type="button" className="ov-icon-btn ov-cb-x" aria-label="Закрыть" title="Закрыть" onClick={close}>
                <X />
              </button>
            </div>
            {c.state === "working" && (
              <div className="ov-cbr-wait" aria-label="ИИ думает">
                <i />
                <i />
                <i />
              </div>
            )}
            {c.state === "error" && <div className="ov-cbr-error">{c.error || "Не получилось"}</div>}
            {c.state === "done" && c.text && (
              <>
                <div className="ov-cbr-text">{c.text}</div>
                <div className="ov-cbr-actions">
                  <button type="button" className="btn btn-ghost" onClick={close}>
                    Закрыть
                  </button>
                  <span className="spacer" />
                  <button type="button" className="btn ov-btn-deny" onClick={copy}>
                    {copied ? <Check /> : <Copy />}
                    {copied ? "Скопировано" : "Копировать"}
                  </button>
                  <button type="button" className="btn btn-primary" onClick={insert}>
                    <CornerDownLeft />
                    Вставить
                  </button>
                </div>
              </>
            )}
          </div>
        )
      }
    </Fade>
  );
}

/** Палитра «Умного буфера» и карточка итога ИИ (поверх всего, даже над полноэкранными окнами: их зовут
 *  горячей клавишей или голосом). */
export function ClipboardOverlay() {
  return (
    <>
      <Palette />
      <ResultCard />
    </>
  );
}

// --- демо-режим (?demo=clipboard, ?demo=clipboard-result) ---------------------------------------------------
const DEMO_AT = { x: 352, y: 192 };
const DEMO_SELECTION = "Встреча переносится на четверг, 15:00 — захватите, пожалуйста, отчёт за квартал.";

function demoItems(): ClipboardItem[] {
  const now = Date.now() / 1000;
  const texts: [string, number, boolean?][] = [
    ["https://disk.yandex.ru/d/q3Lx7-Report-Q3", 40],
    ["anna.petrova@example.com", 600, true],
    ["Спасибо! Отчёт пришлю до конца дня.", 1500],
    ["Москва, ул. Тверская, 12, офис 405", 7200, true],
    ["npm run build && npm run dist", 26000],
    ["Счёт № 1842 от 14.10 — 38 400 ₽", 90000],
    ["Привет! Созвонимся завтра в 11?", 180000],
  ];
  return texts.map(([text, ago, pinned], i) => ({ id: `demo-${i}`, text, chars: text.length, ts: now - ago, pinned: !!pinned }));
}

/** Подставные события демо-сцен «Умного буфера» (зовёт demo.tsx). */
export function clipboardDemo(scene: string, at: (ms: number, fn: () => void) => void) {
  if (scene === "clipboard") {
    const items = demoItems();
    const palette: ClipboardPalette = {
      items,
      total: items.length,
      pinned: items.filter((i) => i.pinned).length,
      actions: CLIPBOARD_ACTIONS.map(({ id, title }) => ({ id, title })),
      at: DEMO_AT,
      selection: DEMO_SELECTION,
      selection_chars: DEMO_SELECTION.length,
      target: { title: "Письмо — Почта", exe: "olk.exe" },
      ai: true,
      hotkey: "Ctrl+Alt+V",
    };
    at(300, () => emitOverlay("plugin.clipboard.palette", palette));
  } else if (scene === "clipboard-result") {
    const card = { id: "demo-card", action: "translate", source: DEMO_SELECTION, at: DEMO_AT };
    at(200, () => emitOverlay("plugin.clipboard.result", { ...card, state: "working", title: "Перевожу…" }));
    at(2600, () =>
      emitOverlay("plugin.clipboard.result", {
        ...card,
        state: "done",
        title: "Перевод на английский",
        text: "The meeting has been moved to Thursday at 3 pm — please bring the quarterly report.",
      }),
    );
  }
}

/** Окно «Письмо» демо-сцены: выделенная фраза и курсор, у которого появляется палитра. */
export function ClipboardDemoDoc() {
  return (
    <>
      <div className="ov-demo-window ov-cb-demo-doc">
        <div className="ov-demo-window-head">
          <Mail /> Письмо — Почта
        </div>
        <p>Добрый день, коллеги!</p>
        <p>
          <mark>{DEMO_SELECTION}</mark>
        </p>
        <p className="dim">С уважением, Анна</p>
      </div>
      <MousePointer2 className="ov-cb-demo-cursor" style={{ left: DEMO_AT.x - 3, top: DEMO_AT.y - 3 }} />
    </>
  );
}
