// Окно «Импорт»: серверы MCP из других программ на этом ПК — Claude Code, Claude Desktop, Cursor, VS Code,
// Codex, Gemini CLI. Ядро только читает их настройки; выбранные серверы копируются в Jarvis.
import { BrainCircuit, Download, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { McpServer } from "@/api/types";
import { Button, Check, cx, Empty, Modal, notify, notifyError } from "@/ui";
import { McpIcon, joinArgs } from "./mcpKit";
import { plural } from "./pluginKit";

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
                <span className="mcp-import-label">{label}</span>
                <span className="faint mono small ellipsis" title={list[0].file}>
                  {list[0].file}
                </span>
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
