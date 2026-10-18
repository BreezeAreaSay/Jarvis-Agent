// Живая панель «Поиска по ПК» на карточке плагина: строка поиска с результатами по мере ввода (понимает
// «pdf за неделю», «презентация про бюджет»), чипы по типу и дате, «Открыть», «Показать в папке» и «Скопировать
// путь» у каждой строки, внизу — где ищем: индекс Windows или свой индекс Jarvis (сколько файлов, когда обновлён).
import "@/styles/filesearch.css";
import { Copy, FolderOpen, Search, SquareArrowOutUpRight, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { FileKind, FileSearchResult, FoundFile } from "@/api/types";
import { cx, IconButton, notify, notifyError, timeAgo } from "@/ui";
import { DATE_FILTERS, FILE_SEARCH, FileKindIcon, fileMeta, KIND_FILTERS, kindMeta } from "./fileSearchKit";
import { type Plugin, plural } from "./pluginKit";
import { usePlugins } from "./pluginStore";

const LIMIT = 8;
const EXAMPLES = ["договор с Ивановым", "презентация про бюджет", "pdf за неделю", "фото за вчера"];

function IndexStatus({ plugin }: { plugin: Plugin }) {
  const st = plugin.status ?? {};
  const own = st.mode === "own";
  const files = Number(st.files) || 0;
  const scanning = st.scanning === true;
  const at = Number(st.indexed_at) || 0;
  let detail: string;
  if (!own) detail = "имя, тип, дата и текст внутри файлов";
  else if (scanning) detail = files ? `${files.toLocaleString("ru-RU")} ${plural(files, "файл", "файла", "файлов")} · обновляю…` : "просматриваю папки…";
  else if (files) detail = `${files.toLocaleString("ru-RU")} ${plural(files, "файл", "файла", "файлов")}${at ? ` · обновлён ${timeAgo(at)}` : ""}`;
  else detail = "папки ещё не просмотрены";
  return (
    <div className="fs-status">
      <span className={cx("status-dot", scanning ? "busy" : "ok")} />
      <b>{own ? "Свой индекс Jarvis" : "Индекс Windows"}</b>
      <span className="faint ellipsis">· {detail}</span>
      {own && st.truncated === true && <span className="chip mini">не все папки</span>}
    </div>
  );
}

function ResultRow({ file, onAct }: { file: FoundFile; onAct: (action: "open" | "reveal" | "copy_path", f: FoundFile) => void }) {
  return (
    <div className="fs-row">
      <button type="button" className="fs-row-main" onClick={() => onAct("open", file)} title={file.path}>
        <FileKindIcon kind={file.kind} />
        <span className="fs-row-text">
          <span className="fs-name ellipsis">{file.name}</span>
          <span className="fs-meta ellipsis">{fileMeta(file)}</span>
        </span>
      </button>
      <span className="fs-acts">
        <IconButton icon={<SquareArrowOutUpRight />} label="Открыть" size="sm" variant="ghost" onClick={() => onAct("open", file)} />
        <IconButton icon={<FolderOpen />} label="Показать в папке" size="sm" variant="ghost" onClick={() => onAct("reveal", file)} />
        <IconButton icon={<Copy />} label="Скопировать путь" size="sm" variant="ghost" onClick={() => onAct("copy_path", file)} />
      </span>
    </div>
  );
}

export function FileSearchLive({ plugin }: { plugin: Plugin }) {
  const action = usePlugins((s) => s.action);
  const [text, setText] = useState("");
  const [kind, setKind] = useState<FileKind | "">("");
  const [date, setDate] = useState("");
  const [result, setResult] = useState<FileSearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const running = plugin.enabled && plugin.status?.running !== false;
  const asked = !!text.trim() || !!kind || !!date;

  useEffect(() => {
    const id = ++seq.current;
    if (!running || !asked) {
      setResult(null);
      setBusy(false);
      setError("");
      return;
    }
    setBusy(true);
    const timer = window.setTimeout(async () => {
      try {
        const args = { text: text.trim(), kinds: kind ? [kind] : [], date, limit: LIMIT };
        const res = (await action(FILE_SEARCH, "search", args)) as unknown as FileSearchResult;
        if (id !== seq.current) return;
        setResult(res);
        setError("");
      } catch (e) {
        if (id === seq.current) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (id === seq.current) setBusy(false);
      }
    }, 180);
    return () => clearTimeout(timer);
  }, [text, kind, date, running, asked, action]);

  const act = async (name: "open" | "reveal" | "copy_path", file: FoundFile) => {
    try {
      await action(FILE_SEARCH, name, { path: file.path });
      if (name === "copy_path") notify("Путь скопирован", "ok");
    } catch (e) {
      notifyError(e);
    }
  };

  const items = result?.items ?? [];
  const hotkey = typeof plugin.config?.hotkey === "string" ? plugin.config.hotkey : "";

  return (
    <div className="lv fs-live">
      <div className={cx("fs-field", busy && "busy")}>
        <Search className="fs-field-ico" />
        <input
          className="input"
          value={text}
          disabled={!running}
          spellCheck={false}
          placeholder={running ? "Найти файл или папку: «договор с Ивановым», «pdf за неделю»…" : "Включите плагин, чтобы искать"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && items[0]) act("open", items[0]);
            if (e.key === "Escape") setText("");
          }}
        />
        {busy ? <span className="spinner fs-field-end" /> : text && <IconButton className="fs-field-end" icon={<X />} label="Очистить" size="sm" variant="ghost" onClick={() => setText("")} />}
      </div>

      <div className="fs-chips" role="group" aria-label="Тип файлов">
        <button type="button" className={cx("fs-chip", !kind && "on")} disabled={!running} onClick={() => setKind("")}>
          Все
        </button>
        {KIND_FILTERS.map((k) => {
          const meta = kindMeta(k);
          return (
            <button key={k} type="button" className={cx("fs-chip", kind === k && "on")} disabled={!running} onClick={() => setKind(kind === k ? "" : k)}>
              <meta.Icon style={{ color: meta.color }} />
              {meta.label}
            </button>
          );
        })}
      </div>
      <div className="fs-chips" role="group" aria-label="Когда изменён">
        {DATE_FILTERS.map((d) => (
          <button key={d.id || "any"} type="button" className={cx("fs-chip", date === d.id && "on")} disabled={!running} onClick={() => setDate(d.id)}>
            {d.label}
          </button>
        ))}
      </div>

      <div className="fs-results">
        {!running ? (
          <span className="lv-placeholder">Включите плагин — и ищите файлы голосом: «Джарвис, найди договор с Ивановым», «покажи фото за вчера»</span>
        ) : error ? (
          <span className="fs-error">{error}</span>
        ) : !asked ? (
          <div className="fs-hint">
            <span className="lv-placeholder">Начните вводить имя файла или попробуйте:</span>
            <div className="fs-suggest">
              {EXAMPLES.map((x) => (
                <button key={x} type="button" className="fs-chip ghost" onClick={() => setText(x)}>
                  «{x}»
                </button>
              ))}
            </div>
            {hotkey && (
              <div className="fs-hotkey faint small">
                Быстрый поиск поверх всех окон — <span className="kbd">{hotkey}</span>
              </div>
            )}
          </div>
        ) : items.length ? (
          <>
            <div className="fs-list">
              {items.map((f) => (
                <ResultRow key={f.path} file={f} onAct={act} />
              ))}
            </div>
            {result?.content_skipped && <div className="faint tiny">Текст внутри файлов ищет только индекс Windows — искал в именах.</div>}
          </>
        ) : result && !busy ? (
          <span className="lv-placeholder">Ничего не нашлось — попробуйте другое слово или снимите фильтры</span>
        ) : (
          <span className="lv-placeholder">Ищу…</span>
        )}
      </div>

      {running && <IndexStatus plugin={plugin} />}
    </div>
  );
}
