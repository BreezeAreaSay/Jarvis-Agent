// Вкладка «Подключения · MCP»: орбита с подключёнными серверами и счётчиками, список своих серверов
// (состояние, инструменты, включение, переподключение, правка, удаление) и каталог серверов MCP с поиском.
import {
  ChevronDown,
  ExternalLink,
  Library,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Server,
  Trash2,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, qs } from "@/api/client";
import { onEvent } from "@/api/events";
import type { McpCatalogItem, McpServer } from "@/api/types";
import { bridge } from "@/lib/bridge";
import { navigate } from "@/lib/router";
import { Button, cx, Empty, IconButton, Modal, notify, notifyError, Tabs, TextInput, Toggle } from "@/ui";
import { CountUp, McpOrbit } from "./McpOrbit";
import { GITHUB_PATH, MCP_STATUS, McpIcon, joinArgs, shortRepo } from "./mcpKit";
import { McpServerModal } from "./McpServerModal";
import { plural } from "./pluginKit";

interface CatalogPage {
  items: McpCatalogItem[];
  next_cursor: string | null;
  total: number;
}

/** Сколько серверов в каталоге; null — неизвестно (реестр отдал только размер страницы). */
let catalogTotal: number | null | undefined;

function exactTotal(page: CatalogPage): number | null {
  if (typeof page.total !== "number") return null;
  const n = page.items?.length ?? 0;
  return page.next_cursor && page.total <= n ? null : page.total;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// --- доска с орбитой ------------------------------------------------------------------------------------
function McpBoard({ servers, total }: { servers: McpServer[]; total: number | null | undefined }) {
  const connected = servers.filter((s) => s.status === "connected");
  const tools = connected.reduce((n, s) => n + (s.tools?.length ?? 0), 0);
  return (
    <section className="mcp-board">
      <div className="mcp-eyebrow">Расширения → Подключения</div>
      <h2 className="mcp-title">Подключения · MCP</h2>
      <McpOrbit servers={servers} size={300} />
      <div className="mcp-counter">
        {typeof total === "number" ? (
          <>
            <CountUp value={total} className="mcp-big" />
            <span className="mcp-counter-label">{plural(total, "сервер", "сервера", "серверов")} в каталоге MCP</span>
          </>
        ) : (
          <>
            <span className="mcp-big words">Реестр MCP</span>
            <span className="mcp-counter-label">Открытый каталог серверов — ищите во вкладке «Каталог»</span>
          </>
        )}
      </div>
      <div className="mcp-stats">
        <div>
          <b>{connected.length}</b>
          <span>{plural(connected.length, "подключён", "подключено", "подключено")}</span>
        </div>
        <div>
          <b>{tools}</b>
          <span>{plural(tools, "инструмент", "инструмента", "инструментов")}</span>
        </div>
        <div>
          <b>{servers.length}</b>
          <span>{plural(servers.length, "сервер", "сервера", "серверов")} всего</span>
        </div>
      </div>
    </section>
  );
}

// --- свои серверы ---------------------------------------------------------------------------------------
function ServerRow({
  server,
  onToggle,
  onReconnect,
  onEdit,
  onDelete,
}: {
  server: McpServer;
  onToggle: (v: boolean) => void;
  onReconnect: () => Promise<void>;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const st = MCP_STATUS[server.status] ?? MCP_STATUS.disabled;
  const tools = server.tools ?? [];
  const where = server.transport === "http" ? server.url : [server.command, joinArgs(server.args)].filter(Boolean).join(" ");
  return (
    <div className={cx("mcp-server", server.status, !server.enabled && "off")}>
      <div className="mcp-server-main">
        <McpIcon item={server} size={42} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <span className="mcp-server-name">{server.name}</span>
            <span className="chip mini mono">{server.transport === "http" ? "HTTP" : "stdio"}</span>
            <span className={cx("mcp-status", st.dot)}>
              <span className={cx("status-dot", st.dot)} />
              {st.text}
            </span>
          </div>
          <div className="mcp-where mono ellipsis" title={where}>
            {where || "—"}
          </div>
        </div>
        <button
          className={cx("mcp-tools-btn", open && "open")}
          disabled={!tools.length}
          onClick={() => setOpen(!open)}
          title={tools.length ? "Показать инструменты" : "Инструменты появятся после подключения"}
        >
          <Wrench />
          {tools.length} {plural(tools.length, "инструмент", "инструмента", "инструментов")}
          {tools.length > 0 && <ChevronDown className="chev" />}
        </button>
        <Toggle checked={server.enabled} onChange={onToggle} label={`Включить «${server.name}»`} />
        <div className="mcp-server-tools">
          <IconButton
            icon={busy ? <span className="spinner" /> : <RefreshCw />}
            label="Переподключить"
            size="sm"
            variant="ghost"
            disabled={!server.enabled || busy}
            onClick={async () => {
              setBusy(true);
              await onReconnect();
              setBusy(false);
            }}
          />
          <IconButton icon={<Pencil />} label="Изменить" size="sm" variant="ghost" onClick={onEdit} />
          <IconButton icon={<Trash2 />} label="Удалить" size="sm" variant="ghost" onClick={onDelete} />
        </div>
      </div>
      {server.error && server.status === "error" && (
        <div className="mcp-error">
          <TriangleAlert />
          <span>{server.error}</span>
        </div>
      )}
      {open && tools.length > 0 && (
        <div className="mcp-toollist">
          {tools.map((t) => (
            <div key={t.name} className="mcp-tool">
              <span className="mono">{t.name}</span>
              <span className="faint">{t.description || "Без описания"}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- каталог --------------------------------------------------------------------------------------------
function CatalogCard({
  item,
  installed,
  onConnect,
}: {
  item: McpCatalogItem;
  installed: boolean;
  onConnect: () => void;
}) {
  return (
    <div className="mcp-card">
      <div className="row" style={{ gap: 11, alignItems: "flex-start" }}>
        <McpIcon item={item} size={38} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 7 }}>
            <span className="mcp-card-title ellipsis">{item.title || item.name.split("/").pop()}</span>
            {item.version && <span className="chip mini">v{item.version.replace(/^v/, "")}</span>}
          </div>
          <div className="mcp-card-name mono ellipsis" title={item.name}>
            {item.name}
          </div>
        </div>
      </div>
      <p className="mcp-card-desc">{item.description || "Описания нет"}</p>
      <div className="row" style={{ gap: 8, marginTop: "auto" }}>
        {item.repository ? (
          <button className="mcp-repo" onClick={() => bridge.openExternal(item.repository)} title={item.repository}>
            {/github\.com/i.test(item.repository) ? (
              <svg viewBox="0 0 24 24" aria-hidden>
                <path d={GITHUB_PATH} fill="currentColor" />
              </svg>
            ) : (
              <ExternalLink />
            )}
            <span className="ellipsis">{shortRepo(item.repository)}</span>
          </button>
        ) : (
          <span />
        )}
        <span className="spacer" />
        {installed ? (
          <span className="chip ok mini">Добавлен</span>
        ) : (
          <Button
            size="sm"
            variant="soft"
            disabled={!item.install}
            title={item.install ? undefined : "У сервера нет готовой инструкции — добавьте его вручную"}
            onClick={onConnect}
          >
            Подключить
          </Button>
        )}
      </div>
    </div>
  );
}

function Catalog({ servers, onConnect }: { servers: McpServer[]; onConnect: (item: McpCatalogItem) => void }) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<McpCatalogItem[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const totalRef = useRef<number | null>(null);
  const [error, setError] = useState("");
  const req = useRef(0);

  const load = useCallback(async (query: string, cursor: string) => {
    const my = ++req.current;
    setLoading(true);
    try {
      const page = await api.get<CatalogPage>(`/api/mcp/catalog${qs({ q: query.trim(), cursor })}`);
      if (my !== req.current) return;
      setItems((prev) => (cursor && prev ? [...prev, ...(page.items ?? [])] : page.items ?? []));
      setNext(page.next_cursor ?? null);
      if (!cursor) totalRef.current = exactTotal(page);
      setTotal(totalRef.current);
      setError("");
    } catch (e) {
      if (my === req.current) setError(message(e));
    } finally {
      if (my === req.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => load(q, ""), q ? 350 : 0);
    return () => clearTimeout(t);
  }, [q, load]);

  const installed = useMemo(() => {
    const names = new Set(servers.map((s) => s.name.toLowerCase()));
    const sig = new Set(servers.map((s) => (s.transport === "http" ? s.url : s.args.join(" ")).toLowerCase()));
    return (item: McpCatalogItem) =>
      names.has((item.title || "").toLowerCase()) ||
      (!!item.install &&
        sig.has((item.install.transport === "http" ? item.install.url ?? "" : (item.install.args ?? []).join(" ")).toLowerCase()));
  }, [servers]);

  return (
    <div className="mcp-catalog">
      <div className="row" style={{ gap: 10 }}>
        <div className="grow">
          <TextInput value={q} onChange={setQ} placeholder="Поиск: github, заметки, база данных…" icon={<Search />} />
        </div>
        {loading && <span className="spinner" />}
        {!loading && items && items.length > 0 && (
          <span className="faint small" style={{ whiteSpace: "nowrap" }}>
            {total !== null ? `${q ? "Найдено" : "В каталоге"} ${total.toLocaleString("ru-RU")}` : `Показано ${items.length}`}
          </span>
        )}
      </div>
      {error && !items?.length ? (
        <Empty icon={<TriangleAlert />}>
          Каталог не открылся: {error}
          <div style={{ marginTop: 10 }}>
            <Button size="sm" onClick={() => load(q, "")}>
              Повторить
            </Button>
          </div>
        </Empty>
      ) : items && !items.length && !loading ? (
        <Empty icon={<Search />}>Ничего не нашлось{q ? ` по запросу «${q}»` : ""}</Empty>
      ) : (
        <div className="mcp-grid">
          {(items ?? []).map((it) => (
            <CatalogCard key={it.name} item={it} installed={installed(it)} onConnect={() => onConnect(it)} />
          ))}
          {!items &&
            Array.from({ length: 6 }, (_, i) => <div key={i} className="mcp-card skeleton" aria-hidden />)}
        </div>
      )}
      {next && items && items.length > 0 && (
        <div className="center" style={{ marginTop: 14 }}>
          <Button onClick={() => load(q, next)} loading={loading}>
            Показать ещё
          </Button>
        </div>
      )}
    </div>
  );
}

// --- вкладка --------------------------------------------------------------------------------------------
export function McpTab({ sub }: { sub?: string }) {
  const view = sub === "catalog" ? "catalog" : "servers";
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [error, setError] = useState("");
  const [total, setTotal] = useState<number | null | undefined>(catalogTotal);
  const [modal, setModal] = useState<{ server?: McpServer; preset?: McpCatalogItem } | null>(null);
  const [removing, setRemoving] = useState<McpServer | null>(null);
  const [deleting, setDeleting] = useState(false);
  const serversRef = useRef<McpServer[] | null>(null);
  serversRef.current = servers;

  const load = useCallback(async () => {
    try {
      const res = await api.get<{ items: McpServer[] }>("/api/mcp/servers");
      setServers(res.items ?? []);
      setError("");
    } catch (e) {
      setError(message(e));
    }
  }, []);

  useEffect(() => {
    load();
    if (catalogTotal === undefined) {
      api
        .get<CatalogPage>("/api/mcp/catalog")
        .then((r) => {
          catalogTotal = exactTotal(r);
          setTotal(catalogTotal);
        })
        .catch(() => undefined);
    }
    return onEvent<{ server_id: string; status: McpServer["status"]; error?: string; tools_count?: number }>("mcp.status", (e) => {
      const d = e.data;
      const known = serversRef.current?.find((x) => x.id === d.server_id);
      // новый сервер или изменился набор инструментов — перечитать список целиком
      if (!known || (typeof d.tools_count === "number" && d.tools_count !== (known.tools?.length ?? 0))) load();
      setServers((list) =>
        list?.map((x) =>
          x.id === d.server_id ? { ...x, status: d.status, error: d.error ?? (d.status === "error" ? x.error : "") } : x,
        ) ?? list,
      );
    });
  }, [load]);

  const upsert = (s: McpServer) =>
    setServers((list) => {
      if (!s || !s.id) return list;
      const cur = list ?? [];
      return cur.some((x) => x.id === s.id) ? cur.map((x) => (x.id === s.id ? s : x)) : [...cur, s];
    });

  const toggle = async (s: McpServer, enabled: boolean) => {
    upsert({ ...s, enabled, status: enabled ? "connecting" : "disabled" });
    try {
      const saved = await api.put<McpServer>(`/api/mcp/servers/${encodeURIComponent(s.id)}`, {
        name: s.name,
        transport: s.transport,
        command: s.command,
        args: s.args,
        env: s.env,
        url: s.url,
        headers: (s as McpServer & { headers?: Record<string, string> }).headers ?? {},
        enabled,
      });
      upsert(saved);
    } catch (e) {
      upsert(s);
      notifyError(e);
    }
  };

  const reconnect = async (s: McpServer) => {
    try {
      upsert({ ...s, status: "connecting", error: "" });
      upsert(await api.post<McpServer>(`/api/mcp/servers/${encodeURIComponent(s.id)}/reconnect`));
    } catch (e) {
      upsert(s);
      notifyError(e);
    }
  };

  const remove = async () => {
    if (!removing) return;
    setDeleting(true);
    try {
      await api.del(`/api/mcp/servers/${encodeURIComponent(removing.id)}`);
      setServers((list) => list?.filter((x) => x.id !== removing.id) ?? list);
      notify(`«${removing.name}» удалён`, "ok");
      setRemoving(null);
    } catch (e) {
      notifyError(e);
    } finally {
      setDeleting(false);
    }
  };

  const list = servers ?? [];
  return (
    <div className="ext-layout">
      <div className="ext-side">
        <McpBoard servers={list} total={total} />
      </div>
      <div className="ext-main">
        <div className="mcp-bar">
          <Tabs
            value={view}
            onChange={(v) => navigate("extensions", "mcp", v === "catalog" ? "catalog" : undefined)}
            items={[
              {
                value: "servers",
                label: (
                  <>
                    Мои серверы {list.length > 0 && <span className="tab-count">{list.length}</span>}
                  </>
                ),
                icon: <Server />,
              },
              { value: "catalog", label: "Каталог", icon: <Library /> },
            ]}
          />
          <span className="spacer" />
          <Button variant="primary" icon={<Plus />} onClick={() => setModal({})}>
            Добавить сервер
          </Button>
        </div>

        {view === "catalog" ? (
          <Catalog servers={list} onConnect={(item) => setModal({ preset: item })} />
        ) : error && !servers ? (
          <Empty icon={<TriangleAlert />}>
            Список серверов не загрузился: {error}
            <div style={{ marginTop: 10 }}>
              <Button size="sm" onClick={load}>
                Повторить
              </Button>
            </div>
          </Empty>
        ) : servers && !servers.length ? (
          <div className="mcp-empty">
            <div className="mcp-empty-title">Пока нет подключений</div>
            <p className="muted small">
              MCP-серверы дают Jarvis AI новые умения: GitHub, Notion, файлы, базы данных, браузер и тысячи других.
              Выберите сервер в каталоге или добавьте свой.
            </p>
            <div className="row" style={{ gap: 8, justifyContent: "center" }}>
              <Button variant="soft" icon={<Library />} onClick={() => navigate("extensions", "mcp", "catalog")}>
                Открыть каталог
              </Button>
              <Button icon={<Plus />} onClick={() => setModal({})}>
                Добавить вручную
              </Button>
            </div>
          </div>
        ) : (
          <div className="mcp-list">
            {!servers && Array.from({ length: 3 }, (_, i) => <div key={i} className="mcp-server skeleton" aria-hidden />)}
            {list.map((s) => (
              <ServerRow
                key={s.id}
                server={s}
                onToggle={(v) => toggle(s, v)}
                onReconnect={() => reconnect(s)}
                onEdit={() => setModal({ server: s })}
                onDelete={() => setRemoving(s)}
              />
            ))}
          </div>
        )}
      </div>

      <McpServerModal
        open={!!modal}
        server={modal?.server}
        preset={modal?.preset}
        onClose={() => setModal(null)}
        onSaved={(s) => {
          upsert(s);
          if (view === "catalog") navigate("extensions", "mcp");
          setTimeout(load, 300);
        }}
      />
      <Modal
        open={!!removing}
        onClose={() => setRemoving(null)}
        title="Удалить сервер?"
        width={420}
        footer={
          <>
            <Button onClick={() => setRemoving(null)}>Отмена</Button>
            <Button variant="danger" icon={<Trash2 />} loading={deleting} onClick={remove}>
              Удалить
            </Button>
          </>
        }
      >
        <p className="muted" style={{ margin: 0 }}>
          Jarvis отключится от «{removing?.name}» и забудет его настройки. Инструменты сервера станут недоступны.
        </p>
      </Modal>
    </div>
  );
}
