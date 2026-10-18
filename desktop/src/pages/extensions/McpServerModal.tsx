// Окно «Добавить сервер» / «Изменить сервер» MCP: программа на компьютере (stdio: команда, аргументы,
// переменные окружения) или адрес в сети (HTTP: URL и заголовки). Из каталога приходит заготовка
// с командой и нужными переменными (обязательные отмечены).
import { Globe, Plus, TerminalSquare, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { McpCatalogItem, McpServer } from "@/api/types";
import { Button, Check, cx, IconButton, Modal, notify, notifyError, Tabs, TextInput } from "@/ui";
import { joinArgs, McpIcon, splitArgs } from "./mcpKit";

interface KV {
  key: string;
  value: string;
  required?: boolean;
  hint?: string;
  secret?: boolean;
}

/** Переменная или заголовок из каталога реестра MCP. */
interface VarSpec {
  name: string;
  description?: string;
  required?: boolean;
  secret?: boolean;
  default?: string;
}

type CatalogInstall = NonNullable<McpCatalogItem["install"]> & { env_vars?: VarSpec[]; headers?: VarSpec[] };

interface Draft {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  env: KV[];
  url: string;
  headers: KV[];
  enabled: boolean;
}

const EMPTY: Draft = { name: "", transport: "stdio", command: "", args: "", env: [], url: "", headers: [], enabled: true };

function fromServer(s: McpServer): Draft {
  const headers = (s as McpServer & { headers?: Record<string, string> }).headers ?? {};
  return {
    name: s.name,
    transport: s.transport === "http" ? "http" : "stdio",
    command: s.command ?? "",
    args: joinArgs(s.args),
    env: Object.entries(s.env ?? {}).map(([key, value]) => ({ key, value: String(value ?? "") })),
    url: s.url ?? "",
    headers: Object.entries(headers).map(([key, value]) => ({ key, value: String(value ?? "") })),
    enabled: s.enabled,
  };
}

function specRows(list: VarSpec[] | undefined): KV[] {
  return (list ?? []).map((v) => ({
    key: v.name,
    value: v.default ?? "",
    required: !!v.required,
    hint: v.description,
    secret: !!v.secret,
  }));
}

function fromCatalog(item: McpCatalogItem): Draft {
  const inst = item.install as CatalogInstall | null;
  const fallbackName = item.name.split("/").pop()?.replace(/[-_]?mcp[-_]?(server)?/gi, "") || item.name;
  return {
    ...EMPTY,
    name: item.title || fallbackName,
    transport: inst?.transport === "http" ? "http" : "stdio",
    command: inst?.command ?? "",
    args: joinArgs(inst?.args),
    url: inst?.url ?? "",
    env: specRows(inst?.env_vars),
    headers: specRows(inst?.headers),
  };
}

const SECRETISH = /key|token|secret|password|pass|auth|api/i;

function KvEditor({
  rows,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  addLabel,
  showErrors,
}: {
  rows: KV[];
  onChange: (rows: KV[]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
  addLabel: string;
  showErrors: boolean;
}) {
  const set = (i: number, patch: Partial<KV>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="kv">
      {rows.map((r, i) => {
        const missing = showErrors && r.required && !r.value.trim();
        return (
          <div key={i} className="kv-row">
            <input
              className={cx("input mono", r.required && "req")}
              value={r.key}
              placeholder={keyPlaceholder}
              spellCheck={false}
              readOnly={r.required}
              onChange={(e) => set(i, { key: e.target.value })}
            />
            <input
              className={cx("input", missing && "bad", (r.secret || SECRETISH.test(r.key)) && "secret-input")}
              type="text"
              autoComplete="off"
              value={r.value}
              placeholder={r.hint || (r.required ? "Обязательно" : valuePlaceholder)}
              spellCheck={false}
              onChange={(e) => set(i, { value: e.target.value })}
            />
            {r.required ? (
              <span className="kv-req" title="Обязательная переменная">
                *
              </span>
            ) : (
              <IconButton icon={<X />} label="Убрать" size="sm" variant="ghost" onClick={() => onChange(rows.filter((_, j) => j !== i))} />
            )}
          </div>
        );
      })}
      <div>
        <Button size="sm" variant="ghost" icon={<Plus />} onClick={() => onChange([...rows, { key: "", value: "" }])}>
          {addLabel}
        </Button>
      </div>
    </div>
  );
}

export function McpServerModal({
  open,
  server,
  preset,
  onClose,
  onSaved,
}: {
  open: boolean;
  server?: McpServer;
  preset?: McpCatalogItem;
  onClose: () => void;
  onSaved: (s: McpServer) => void;
}) {
  const [d, setD] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [tried, setTried] = useState(false);
  useEffect(() => {
    if (!open) return;
    setD(server ? fromServer(server) : preset ? fromCatalog(preset) : EMPTY);
    setTried(false);
  }, [open, server, preset]);

  const patch = (p: Partial<Draft>) => setD((x) => ({ ...x, ...p }));
  const stdio = d.transport === "stdio";
  const errors: string[] = [];
  if (!d.name.trim()) errors.push("Укажите название");
  if (stdio && !d.command.trim()) errors.push("Укажите команду запуска");
  if (!stdio && !/^https?:\/\/\S+$/i.test(d.url.trim())) errors.push("Адрес должен начинаться с http:// или https://");
  const missing = (stdio ? d.env : d.headers).filter((r) => r.required && !r.value.trim()).map((r) => r.key);
  if (missing.length) errors.push(`Заполните: ${missing.join(", ")}`);
  const placeholders = stdio && /<[^<>]+>/.test(d.args);

  const toObj = (rows: KV[]) =>
    Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]));

  const save = async () => {
    setTried(true);
    if (errors.length) {
      notify(errors[0], "error");
      return;
    }
    const headers = toObj(d.headers);
    const body: Record<string, unknown> = {
      name: d.name.trim(),
      transport: d.transport,
      command: stdio ? d.command.trim() : "",
      args: stdio ? splitArgs(d.args) : [],
      env: stdio ? toObj(d.env) : {},
      url: stdio ? "" : d.url.trim(),
      enabled: d.enabled,
    };
    body.headers = stdio ? {} : headers;
    setBusy(true);
    try {
      const saved = server
        ? await api.put<McpServer>(`/api/mcp/servers/${encodeURIComponent(server.id)}`, body)
        : await api.post<McpServer>("/api/mcp/servers", body);
      onSaved(saved);
      notify(server ? "Сервер сохранён" : `«${d.name.trim()}» подключается…`, "ok");
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
      width={600}
      icon={<McpIcon item={server ?? preset ?? { name: d.name, command: d.command, url: d.url }} size={30} />}
      title={server ? "Изменить сервер" : preset ? `Подключить «${preset.title || d.name}»` : "Добавить сервер"}
      footer={
        <>
          <Check checked={d.enabled} onChange={(v) => patch({ enabled: v })}>
            Включить сразу
          </Check>
          <span className="spacer" />
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" loading={busy} onClick={save}>
            {server ? "Сохранить" : "Подключить"}
          </Button>
        </>
      }
    >
      <div className="mcp-form">
        {preset?.description && <p className="muted small" style={{ margin: "0 0 4px" }}>{preset.description}</p>}
        <label className="field-label">Название</label>
        <TextInput value={d.name} onChange={(v) => patch({ name: v })} placeholder="Например, GitHub" autoFocus={!preset && !server} />

        <label className="field-label">Как подключаться</label>
        <Tabs
          value={d.transport}
          onChange={(v) => patch({ transport: v })}
          items={[
            { value: "stdio", label: "Программа на компьютере", icon: <TerminalSquare /> },
            { value: "http", label: "Адрес в сети (HTTP)", icon: <Globe /> },
          ]}
        />

        {stdio ? (
          <>
            <div className="mcp-form-row">
              <div style={{ flex: "0 0 150px" }}>
                <label className="field-label">Команда</label>
                <TextInput value={d.command} onChange={(v) => patch({ command: v })} placeholder="npx" className="mono" />
              </div>
              <div className="grow">
                <label className="field-label">Аргументы</label>
                <TextInput
                  value={d.args}
                  onChange={(v) => patch({ args: v })}
                  placeholder="-y @modelcontextprotocol/server-github"
                  className="mono"
                />
              </div>
            </div>
            <div className={cx("field-hint", placeholders && "warn")} style={{ marginTop: 6 }}>
              {placeholders
                ? "Замените значения в угловых скобках <…> на свои — например, путь к папке."
                : "Аргументы — через пробел; путь с пробелами возьмите в кавычки."}
            </div>
            <label className="field-label">Переменные окружения</label>
            <KvEditor
              rows={d.env}
              onChange={(env) => patch({ env })}
              keyPlaceholder="ИМЯ"
              valuePlaceholder="значение"
              addLabel="Переменная"
              showErrors={tried}
            />
          </>
        ) : (
          <>
            <label className="field-label">Адрес сервера</label>
            <TextInput value={d.url} onChange={(v) => patch({ url: v })} placeholder="https://mcp.example.com/mcp" className="mono" />
            <label className="field-label">Заголовки</label>
            <KvEditor
              rows={d.headers}
              onChange={(headers) => patch({ headers })}
              keyPlaceholder="Authorization"
              valuePlaceholder="Bearer …"
              addLabel="Заголовок"
              showErrors={tried}
            />
          </>
        )}
        {tried && errors.length > 0 && <div className="mcp-form-error">{errors.join(" · ")}</div>}
      </div>
    </Modal>
  );
}
