// AI Dev System (github.com/BreezeAreaSay/ai-dev-system) — свой сервер MCP: память проекта, skills и проверка задач.
// Ядро находит установку (запись «ai-dev» в Claude/Cursor/VS Code/Codex/Gemini, команда ai-dev или папка клона)
// и подключает её одной кнопкой; профили задают, какие инструменты получит Jarvis AI.
// Карточка пока не показывается: MCP AI Dev System дорабатывается, подключим после (ядро и API уже готовы).
// Здесь же окно «Импорт» — серверы MCP из других программ на этом ПК.
import {
  BrainCircuit,
  ChevronDown,
  Download,
  ExternalLink,
  FolderOpen,
  RefreshCw,
  Trash2,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { onEvent } from "@/api/events";
import type { McpServer } from "@/api/types";
import { bridge, isElectron } from "@/lib/bridge";
import { Button, Check, cx, Empty, IconButton, Modal, notify, notifyError, TextInput, Toggle } from "@/ui";
import { MCP_STATUS, McpIcon, joinArgs } from "./mcpKit";
import { plural } from "./pluginKit";

interface AiDevProfile {
  id: string;
  tools: number;
  description: string;
  enabled: boolean;
}

export interface AiDevState {
  found: boolean;
  install: {
    source: string;
    label: string;
    command: string;
    args: string[];
    root: string;
    version: string;
    docker: boolean;
  } | null;
  server: McpServer | null;
  profiles: AiDevProfile[];
  tools: number;
  github: string;
}

const GITHUB = "https://github.com/BreezeAreaSay/ai-dev-system";

function same(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** Карточка AI Dev System над списком серверов. live — его строка из общего списка (живой статус);
 * подключённый сервер управляется прямо здесь (вкл/выкл, переподключение, удаление). */
export function AiDevCard({
  live,
  onChanged,
  onToggle,
  onReconnect,
  onDelete,
}: {
  live?: McpServer;
  onChanged: (s: McpServer) => void;
  onToggle: (s: McpServer, v: boolean) => void;
  onReconnect: (s: McpServer) => Promise<void>;
  onDelete: (s: McpServer) => void;
}) {
  const [state, setState] = useState<AiDevState | null>(null);
  const [failed, setFailed] = useState("");
  const [picked, setPicked] = useState<string[] | null>(null); // профили, выбранные, но ещё не применённые
  const [busy, setBusy] = useState(false);
  const [folderInput, setFolderInput] = useState<string | null>(null); // в браузере — путь вводом
  const [toolsOpen, setToolsOpen] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);

  const load = useCallback(async () => {
    try {
      setState(await api.get<AiDevState>("/api/mcp/aidev"));
      setFailed("");
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    return onEvent<{ server_id: string }>("mcp.status", (e) => {
      if (e.data.server_id === "ai-dev") load();
    });
  }, [load]);

  const enabled = useMemo(() => state?.profiles.filter((p) => p.enabled).map((p) => p.id) ?? ["core"], [state]);
  const chosen = picked ?? enabled;
  const tools = (state?.profiles ?? []).filter((p) => chosen.includes(p.id)).reduce((n, p) => n + p.tools, 0);
  const dirty = picked !== null && !same([...picked].sort(), [...enabled].sort());

  const connect = async (body: { profiles?: string[]; folder?: string }) => {
    setBusy(true);
    try {
      const next = await api.post<AiDevState>("/api/mcp/aidev", body);
      setState(next);
      setPicked(null);
      setFolderInput(null);
      if (next.server) onChanged(next.server);
      notify(body.folder || !state?.server ? "AI Dev System подключён" : "Профили AI Dev System обновлены", "ok");
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  const pickFolder = async () => {
    if (!isElectron) {
      setFolderInput(folderInput ?? "");
      return;
    }
    try {
      const folder = await bridge.pickFile({
        title: "Папка клона ai-dev-system",
        folder: true,
      });
      if (folder) await connect({ profiles: chosen, folder });
    } catch (e) {
      notifyError(e);
    }
  };

  if (failed && !state) {
    return (
      <div className="aidev-card">
        <div className="aidev-head">
          <McpIcon item={{ name: "ai-dev" }} size={46} className="aidev-icon" />
          <div className="grow">
            <div className="aidev-title">AI Dev System</div>
            <div className="faint small">Не удалось проверить: {failed}</div>
          </div>
          <Button size="sm" onClick={load}>
            Повторить
          </Button>
        </div>
      </div>
    );
  }
  if (!state) return <div className="aidev-card skeleton" aria-hidden />;

  const server = state.server ? { ...state.server, ...(live ?? {}) } : null;
  const st = server ? (MCP_STATUS[server.status] ?? MCP_STATUS.disabled) : null;
  const serverTools = server?.status === "connected" ? (server.tools ?? []) : [];
  const where = state.install
    ? [state.install.label, state.install.version && `v${state.install.version}`].filter(Boolean).join(" · ")
    : "";
  return (
    <div className={cx("aidev-card", server && "connected")}>
      <div className="aidev-head">
        <McpIcon item={{ name: "ai-dev" }} size={46} className="aidev-icon" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <span className="aidev-title">AI Dev System</span>
            <span className="chip mini accent">ваш сервер</span>
            {st && (
              <span className={cx("mcp-status", st.dot)}>
                <span className={cx("status-dot", st.dot)} />
                {st.text}
              </span>
            )}
          </div>
          <div className="aidev-sub">Память проекта, подбор skills и задачи, которые агент обязан довести и доказать</div>
        </div>
        {server ? (
          <>
            <button
              className={cx("mcp-tools-btn", toolsOpen && "open")}
              disabled={!serverTools.length}
              onClick={() => setToolsOpen(!toolsOpen)}
              title={serverTools.length ? "Показать инструменты" : "Инструменты появятся после подключения"}
            >
              <Wrench />
              {serverTools.length} {plural(serverTools.length, "инструмент", "инструмента", "инструментов")}
              {serverTools.length > 0 && <ChevronDown className="chev" />}
            </button>
            <Toggle checked={server.enabled} onChange={(v) => onToggle(server, v)} label="Включить AI Dev System" />
            <div className="mcp-server-tools">
              <IconButton
                icon={reconnecting ? <span className="spinner" /> : <RefreshCw />}
                label="Переподключить"
                size="sm"
                variant="ghost"
                disabled={!server.enabled || reconnecting}
                onClick={async () => {
                  setReconnecting(true);
                  await onReconnect(server);
                  setReconnecting(false);
                }}
              />
              <IconButton
                icon={<Trash2 />}
                label="Отключить и забыть"
                size="sm"
                variant="ghost"
                onClick={() => onDelete(server)}
              />
            </div>
          </>
        ) : (
          <button className="mcp-repo" onClick={() => bridge.openExternal(state.github || GITHUB)} title={state.github || GITHUB}>
            <ExternalLink />
            <span>GitHub</span>
          </button>
        )}
      </div>
      {server?.error && server.status === "error" && (
        <div className="mcp-error">
          <TriangleAlert />
          <span>{server.error}</span>
        </div>
      )}
      {toolsOpen && serverTools.length > 0 && (
        <div className="mcp-toollist">
          {serverTools.map((t) => (
            <div key={t.name} className="mcp-tool">
              <span className="mono">{t.name}</span>
              <span className="faint">{t.description || "Без описания"}</span>
            </div>
          ))}
        </div>
      )}

      <div className="aidev-where">
        {server ? (
          <span className="mono ellipsis" title={[server.command, joinArgs(server.args)].join(" ")}>
            {[server.command, joinArgs(server.args)].join(" ")}
          </span>
        ) : state.found ? (
          <span>
            <b className="ok-text">Найден</b> — {where}
          </span>
        ) : (
          <span>
            <TriangleAlert className="warn-icon" /> На этом ПК не найден. Укажите папку клона или установите его:{" "}
            <span className="mono">npm ci &amp;&amp; npm run setup</span> в <span className="mono">ai-dev-mcp-server</span>
          </span>
        )}
      </div>

      <div className="aidev-profiles">
        <span className="aidev-label">Профили</span>
        {state.profiles.map((p) => (
          <span key={p.id} title={p.description}>
            <Check
              chip
              checked={chosen.includes(p.id)}
              onChange={(v) => {
                if (p.id === "core") return; // ядро всегда включено
                setPicked(v ? [...chosen, p.id] : chosen.filter((x) => x !== p.id));
              }}
            >
              {p.id} <span className="faint">{p.tools}</span>
            </Check>
          </span>
        ))}
      </div>

      <div className="aidev-foot">
        <span className="faint small">
          {tools} {plural(tools, "инструмент", "инструмента", "инструментов")} для Jarvis AI
          {tools > 50 ? " — много: каждый запрос к модели станет дороже" : ""}
        </span>
        <span className="spacer" />
        {folderInput !== null && (
          <div style={{ width: 300 }}>
            <TextInput
              value={folderInput}
              onChange={setFolderInput}
              placeholder="C:\Users\…\ai-dev-system"
              autoFocus
              onEnter={() => folderInput.trim() && connect({ profiles: chosen, folder: folderInput.trim() })}
            />
          </div>
        )}
        <Button size="sm" variant="ghost" icon={<FolderOpen />} onClick={pickFolder} disabled={busy}>
          {state.found || server ? "Другая папка" : "Указать папку"}
        </Button>
        {server ? (
          <Button size="sm" variant="primary" disabled={!dirty} loading={busy} onClick={() => connect({ profiles: chosen })}>
            Применить профили
          </Button>
        ) : (
          <Button
            size="sm"
            variant="primary"
            disabled={!state.found && !folderInput?.trim()}
            loading={busy}
            onClick={() => connect(folderInput?.trim() ? { profiles: chosen, folder: folderInput.trim() } : { profiles: chosen })}
          >
            Подключить
          </Button>
        )}
      </div>
    </div>
  );
}

// --- импорт из других программ -----------------------------------------------------------------------------
interface ImportItem {
  key: string;
  client: string;
  client_label: string;
  file: string;
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  url: string;
  problem: string;
  added: boolean;
}

export function ImportModal({ open, onClose, onImported }: { open: boolean; onClose: () => void; onImported: () => void }) {
  const [items, setItems] = useState<ImportItem[] | null>(null);
  const [error, setError] = useState("");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setItems(null);
    setError("");
    api
      .get<{ items: ImportItem[] }>("/api/mcp/import")
      .then((r) => {
        setItems(r.items ?? []);
        setSel(new Set((r.items ?? []).filter((i) => !i.added && !i.problem).map((i) => i.key)));
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [open]);

  const groups = useMemo(() => {
    const out = new Map<string, ImportItem[]>();
    for (const it of items ?? []) out.set(it.client_label, [...(out.get(it.client_label) ?? []), it]);
    return [...out.entries()];
  }, [items]);

  const run = async () => {
    setBusy(true);
    try {
      const res = await api.post<{
        items: McpServer[];
        skipped: { key: string; reason: string }[];
      }>("/api/mcp/import", {
        keys: [...sel],
      });
      const n = res.items.length;
      notify(
        n
          ? `Добавлено ${n} ${plural(n, "сервер", "сервера", "серверов")}${res.skipped.length ? `, пропущено ${res.skipped.length}` : ""}`
          : `Ничего не добавлено: ${res.skipped[0]?.reason ?? "нечего импортировать"}`,
        n ? "ok" : "info",
      );
      onImported();
      onClose();
    } catch (e) {
      notifyError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Импорт серверов MCP"
      icon={<Download />}
      width={640}
      footer={
        <>
          <span className="faint small grow">Jarvis только читает настройки программ и ничего в них не меняет</span>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" disabled={!sel.size} loading={busy} onClick={run}>
            Импортировать{sel.size ? ` ${sel.size}` : ""}
          </Button>
        </>
      }
    >
      {error ? (
        <Empty icon={<TriangleAlert />}>Не удалось прочитать настройки: {error}</Empty>
      ) : !items ? (
        <div className="center" style={{ padding: 30 }}>
          <span className="spinner" />
        </div>
      ) : !items.length ? (
        <Empty icon={<BrainCircuit />}>
          В Claude Code, Claude Desktop, Cursor, VS Code, Codex и Gemini CLI на этом ПК серверов MCP не нашлось
        </Empty>
      ) : (
        <div className="mcp-import">
          {groups.map(([label, list]) => (
            <div key={label} className="mcp-import-group">
              <div className="mcp-import-client">
                {label} <span className="faint mono small ellipsis">{list[0].file}</span>
              </div>
              {list.map((it) => {
                const disabled = it.added || !!it.problem;
                const where = it.transport === "http" ? it.url : [it.command, joinArgs(it.args)].join(" ");
                return (
                  <div key={it.key} className={cx("mcp-import-row", disabled && "off")}>
                    {disabled ? (
                      <span className="mcp-import-spacer" />
                    ) : (
                      <Check
                        checked={sel.has(it.key)}
                        onChange={(v) =>
                          setSel((s) => {
                            const n = new Set(s);
                            if (v) n.add(it.key);
                            else n.delete(it.key);
                            return n;
                          })
                        }
                      />
                    )}
                    <McpIcon
                      item={{
                        name: it.name,
                        command: it.command,
                        args: it.args,
                        url: it.url,
                        transport: it.transport,
                      }}
                      size={32}
                    />
                    <div className="grow" style={{ minWidth: 0 }}>
                      <div className="row" style={{ gap: 7 }}>
                        <b className="ellipsis">{it.name}</b>
                        <span className="chip mini mono">{it.transport === "http" ? "HTTP" : "stdio"}</span>
                        {it.added && <span className="chip mini ok">Уже есть</span>}
                      </div>
                      <div className="mono faint small ellipsis" title={where}>
                        {where}
                      </div>
                      {it.problem && !it.added && <div className="mcp-import-problem">{it.problem}</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
