// «Поиск по ПК» поверх экрана, в стопке под строкой голоса:
// - карточка с номерами после голосового поиска («Нашёл 4 — какой открыть?»): щелчок по строке открывает файл,
//   «В папке» — показывает его в Проводнике, голосом — «открой второй»; уходит сама через ttl секунд;
// - быстрый поиск по горячей клавише (plugin.file_search.box): результаты по мере ввода, ↑/↓ — выбор,
//   Enter — открыть, Shift+Enter — показать в папке, Esc — закрыть.
// Окно оверлея само фокус не берёт: на время ввода быстрый поиск просит его у Electron (overlay:focus).
import "@/styles/filesearch.css";
import { FolderOpen, Search, X } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, useEffect, useRef, useState } from "react";
import type { FileSearchCard, FileSearchResult, FoundFile } from "@/api/types";
import { bridge } from "@/lib/bridge";
import { FILE_SEARCH, FileKindIcon, fileMeta } from "@/pages/extensions/fileSearchKit";
import { PluginGlyph } from "@/pages/extensions/pluginKit";
import { cx } from "@/ui";
import { emitOverlay, useOverlayEvent } from "./bus";
import { DEMO, pluginAction, Slot } from "./common";

const BOX_LIMIT = 8;
const PICKED_HIDE_MS = 1300;
const GLYPH = { id: FILE_SEARCH, icon: "file-search" };

// --- демо-данные (сцены files и files-box в overlay/demo.tsx) --------------------------------------------
const HOME = "C:\\Users\\me";

function demoFile(dir: string, where: string, name: string, kind: FoundFile["kind"], size: number, hoursAgo: number): FoundFile {
  return { path: `${HOME}\\${dir}\\${name}`, name, kind, size, modified: Date.now() / 1000 - hoursAgo * 3600, folder: `${HOME}\\${dir}`, where };
}

const DEMO_FILES: FoundFile[] = [
  demoFile("Documents", "Документы", "Бюджет 2026.xlsx", "spreadsheet", 64_800, 3),
  demoFile("Documents", "Документы", "Презентация бюджет Q3.pptx", "presentation", 5_600_000, 27),
  demoFile("Documents\\Проекты\\Jarvis", "Документы › Проекты › Jarvis", "План запуска и бюджет.docx", "document", 56_000, 98),
  demoFile("Downloads", "Загрузки", "Бюджет — согласование.pdf", "pdf", 182_000, 140),
  demoFile("Documents", "Документы", "Бюджет семьи — сентябрь.xlsx", "spreadsheet", 21_500, 720),
  demoFile("Documents", "Документы", "Договор с Ивановым.docx", "document", 48_200, 22),
  demoFile("Documents", "Документы", "Отчёт за сентябрь.docx", "document", 132_000, 430),
  demoFile("Pictures", "Изображения", "IMG_2042.jpg", "image", 4_200_000, 26),
];

function demoSearch(text: string): FoundFile[] {
  const words = text.toLowerCase().replace(/ё/g, "е").split(/\s+/).filter(Boolean);
  const stem = (w: string) => (w.length > 5 ? w.slice(0, w.length - 2) : w);
  return DEMO_FILES.filter((f) => {
    const name = f.name.toLowerCase().replace(/ё/g, "е");
    return words.every((w) => name.includes(stem(w)));
  }).slice(0, BOX_LIMIT);
}

/** Карточка результатов для демо-сцены files. */
function demoCard(): FileSearchCard {
  const items = DEMO_FILES.slice(0, 4);
  return { query: "бюджет", items, total: items.length, ttl: 3600 };
}

/** Демо-сцены: files — «Джарвис, найди бюджет» и карточка с номерами, files-box — быстрый поиск. */
export function fileSearchDemo(scene: string, at: (ms: number, fn: () => void) => void) {
  if (scene === "files") {
    at(150, () => {
      emitOverlay("voice.wake", {});
      emitOverlay("voice.state", { state: "listening" });
      emitOverlay("voice.partial", { text: "Джарвис найди бюджет", wake: true });
    });
    at(500, () => emitOverlay("plugin.file_search.results", demoCard()));
  }
  if (scene === "files-box") at(300, () => emitOverlay("plugin.file_search.box", { open: true, query: "бюджет" }));
}

// --- строка результата ------------------------------------------------------------------------------------
function Row({
  file,
  n,
  selected,
  done,
  onOpen,
  onReveal,
}: {
  file: FoundFile;
  n?: number;
  selected?: boolean;
  done?: boolean;
  onOpen: () => void;
  onReveal: () => void;
}) {
  return (
    <div className={cx("fs-ov-row", selected && "sel", done && "done")}>
      {n !== undefined && <span className="fs-ov-num">{n}</span>}
      <button type="button" className="fs-ov-open" onClick={onOpen} title={file.path}>
        <FileKindIcon kind={file.kind} size={32} />
        <span className="fs-row-text">
          <span className="fs-name ellipsis">{file.name}</span>
          <span className="fs-meta ellipsis">{fileMeta(file)}</span>
        </span>
      </button>
      <button type="button" className="fs-ov-reveal" onClick={onReveal} title="Показать в папке">
        <FolderOpen />
        В папке
      </button>
    </div>
  );
}

// --- карточка с номерами после голосового поиска -----------------------------------------------------------
function ResultsCard({ hidden }: { hidden: boolean }) {
  const [card, setCard] = useState<FileSearchCard | null>(null);
  const [show, setShow] = useState(false);
  const [picked, setPicked] = useState(-1);
  const [shownAt, setShownAt] = useState(0);
  const hideTimer = useRef(0);

  const hideIn = (ms: number) => {
    clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setShow(false), ms);
  };

  useOverlayEvent<FileSearchCard>("plugin.file_search.results", (d) => {
    if (!d || !Array.isArray(d.items) || !d.items.length) return;
    setCard(d);
    setPicked(-1);
    setShownAt(Date.now());
    setShow(true);
    hideIn(Math.max(3, Number(d.ttl) || 30) * 1000);
  });
  useOverlayEvent<{ path: string; index: number }>("plugin.file_search.opened", (d) => {
    if (!card || !show || !d) return;
    const i = d.index >= 0 && d.index < card.items.length ? d.index : card.items.findIndex((f) => f.path === d.path);
    if (i < 0) return;
    setPicked(i);
    hideIn(PICKED_HIDE_MS);
  });
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  const open = (f: FoundFile, i: number, reveal = false) => {
    setPicked(i);
    hideIn(PICKED_HIDE_MS);
    pluginAction(FILE_SEARCH, reveal ? "reveal" : "open", { path: f.path });
  };

  const ttl = Math.max(3, Number(card?.ttl) || 30);
  const items = card?.items ?? [];
  const more = card && card.total > items.length ? card.total - items.length : 0;
  return (
    <Slot show={show && !hidden && !!card}>
      <div className="ov-card fs-ov ov-anim" data-interactive>
        <div className="fs-ov-head">
          <span className="fs-ov-tile">
            <PluginGlyph plugin={GLYPH} />
          </span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="fs-ov-title ellipsis">Нашёл {items.length} — какой открыть?</div>
            <div className="fs-ov-sub ellipsis">
              Поиск по ПК · «{card?.query}»{more ? ` · ещё ${more} в приложении` : ""}
            </div>
          </div>
          <button type="button" className="ov-icon-btn" aria-label="Закрыть" title="Закрыть" onClick={() => setShow(false)}>
            <X style={{ fill: "none" }} />
          </button>
        </div>
        <div className="fs-ov-list">
          {items.map((f, i) => (
            <Row
              key={f.path}
              file={f}
              n={i + 1}
              done={picked === i}
              onOpen={() => open(f, i)}
              onReveal={() => open(f, i, true)}
            />
          ))}
        </div>
        <div className="fs-ov-foot">
          Скажите <b>«открой {items.length > 1 ? "второй" : "первый"}»</b> или нажмите на строку
        </div>
        {picked < 0 && (
          <span className="fs-ov-countdown" aria-hidden key={shownAt}>
            <i style={{ animationDuration: `${ttl}s` } as CSSProperties} />
          </span>
        )}
      </div>
    </Slot>
  );
}

// --- быстрый поиск по горячей клавише --------------------------------------------------------------------
function QuickBox() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [items, setItems] = useState<FoundFile[]>([]);
  const [searched, setSearched] = useState("");
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const seq = useRef(0);

  useOverlayEvent<{ open: boolean; query?: string }>("plugin.file_search.box", (d) => {
    const next = !!d?.open;
    setOpen(next);
    if (next) {
      setText(typeof d.query === "string" ? d.query : "");
      setSel(0);
    }
  });

  // клавиатура нужна окну оверлея только на время ввода
  useEffect(() => {
    if (!open) return;
    bridge.send("overlay:focus", true);
    const raf = requestAnimationFrame(() => input.current?.focus());
    return () => {
      cancelAnimationFrame(raf);
      bridge.send("overlay:focus", false);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const q = text.trim();
    const id = ++seq.current;
    if (!q) {
      setItems([]);
      setSearched("");
      setBusy(false);
      return;
    }
    setBusy(true);
    const timer = window.setTimeout(async () => {
      const found = DEMO
        ? demoSearch(q)
        : ((await pluginAction<FileSearchResult>(FILE_SEARCH, "search", { text: q, limit: BOX_LIMIT }))?.items ?? []);
      if (id !== seq.current) return;
      setItems(found);
      setSearched(q);
      setSel(0);
      setBusy(false);
    }, 160);
    return () => clearTimeout(timer);
  }, [text, open]);

  const close = () => {
    setOpen(false);
    pluginAction(FILE_SEARCH, "box", { open: false });
  };

  // щёлкнули мимо — окно потеряло фокус: прячемся, как меню «Пуск»
  useEffect(() => {
    if (!open || DEMO) return;
    const onBlur = () => {
      setOpen(false);
      pluginAction(FILE_SEARCH, "box", { open: false });
    };
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, [open]);

  const choose = (f: FoundFile, reveal = false) => {
    pluginAction(FILE_SEARCH, reveal ? "reveal" : "open", { path: f.path });
    close();
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(items.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const f = items[sel];
      if (f) choose(f, e.shiftKey || e.ctrlKey);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };

  const q = text.trim();
  return (
    <Slot show={open}>
      <div className="ov-card fs-ov fs-box ov-anim" data-interactive>
        <div className="fs-box-field">
          <Search />
          <input
            ref={input}
            className="fs-box-input"
            value={text}
            placeholder="Найти файл или папку на компьютере…"
            spellCheck={false}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
            aria-label="Поиск по ПК"
          />
          {busy ? (
            <span className="spinner" />
          ) : (
            <button type="button" className="ov-icon-btn" aria-label="Закрыть" title="Закрыть (Esc)" onClick={close}>
              <X style={{ fill: "none" }} />
            </button>
          )}
        </div>
        {items.length > 0 ? (
          <div className="fs-ov-list">
            {items.map((f, i) => (
              <Row key={f.path} file={f} selected={i === sel} onOpen={() => choose(f)} onReveal={() => choose(f, true)} />
            ))}
          </div>
        ) : q && searched === q && !busy ? (
          <div className="fs-box-empty">Ничего не нашлось — попробуйте другое слово: «договор», «pdf за неделю»</div>
        ) : !q ? (
          <div className="fs-box-empty">Имя файла, тип или дата: «презентация про бюджет», «фото за вчера»</div>
        ) : null}
        <div className="fs-ov-foot">
          <span className="kbd">↑</span>
          <span className="kbd">↓</span> выбрать · <span className="kbd">Enter</span> открыть ·{" "}
          <span className="kbd">Shift+Enter</span> в папке · <span className="kbd">Esc</span> закрыть
        </div>
      </div>
    </Slot>
  );
}

/** «Поиск по ПК» в оверлее: быстрый поиск и карточка голосовых результатов. */
export function FileSearchOverlay({ hidden }: { hidden: boolean }) {
  return (
    <>
      <QuickBox />
      <ResultsCard hidden={hidden} />
    </>
  );
}
