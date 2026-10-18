// «Инструменты и разрешения»: что Jarvis AI может делать сам — встроенные инструменты, плагины, серверы MCP и
// действия агента в окнах; для каждого — риск и выбор «Авто / Разрешено / Спрашивать / Запрещено».
import { Bot, Globe, Monitor, NotebookPen, Plug, Puzzle, RotateCcw, Search, ShieldAlert, Wrench } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { onEvent } from "@/api/events";
import { useApp } from "@/store/app";
import { Button, Card, cx, notifyError, Tabs, TextInput } from "@/ui";
import { Note, useApi } from "../kit";

type Decision = "allow" | "ask" | "deny";
type Choice = Decision | "auto";

interface ToolInfo {
  policy_key: string;
  name: string;
  title: string;
  description: string;
  source: string;
  group: string;
  modes: string[];
  available: boolean;
  timeout: number | null;
  risk: "read" | "act" | "risky";
  risk_title: string;
  default: Decision;
  override: Decision | null;
  decision: Decision;
}

interface Catalog {
  items: ToolInfo[];
  groups: { id: string; title: string }[];
  agent_confirm: "risky" | "always";
}

const MODE_TITLES: Record<string, string> = { chat: "Чат", all: "Всё вместе", agent: "Агент" };
const DECISION_TITLES: Record<Decision, string> = { allow: "разрешено", ask: "спрашивает", deny: "запрещено" };

function groupIcon(id: string): ReactNode {
  if (id === "web") return <Globe />;
  if (id === "memory") return <NotebookPen />;
  if (id === "computer") return <Monitor />;
  if (id === "agent") return <Bot />;
  if (id.startsWith("mcp:")) return <Plug />;
  return <Puzzle />;
}

type Filter = "all" | "ask" | "deny" | "changed";

function ToolRow({ tool, onChoose }: { tool: ToolInfo; onChoose: (t: ToolInfo, c: Choice) => void }) {
  const choice: Choice = tool.override ?? "auto";
  return (
    <div className={cx("tool-row", !tool.available && "off")}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="tool-title">
          <span className="ellipsis">{tool.title}</span>
          <span className={cx("chip", "tool-risk", tool.risk)} title={tool.risk_title}>
            {tool.risk === "read" ? "Читает" : tool.risk === "risky" ? "Рискованное" : "Действует"}
          </span>
        </div>
        <div className="tool-desc">{tool.description}</div>
        <div className="tool-meta faint tiny">
          {tool.modes.map((m) => MODE_TITLES[m] ?? m).join(" · ")}
          {tool.timeout ? ` · до ${Math.round(tool.timeout)} с` : ""}
          {!tool.available && " · выключено в «Jarvis AI → Возможности»"}
          {choice === "auto" && ` · сейчас: ${DECISION_TITLES[tool.decision]}`}
        </div>
      </div>
      <Tabs<Choice>
        value={choice}
        items={[
          { value: "auto", label: "Авто" },
          { value: "allow", label: "Разрешено" },
          { value: "ask", label: "Спрашивать" },
          { value: "deny", label: "Запрещено" },
        ]}
        onChange={(c) => onChoose(tool, c)}
      />
    </div>
  );
}

export default function AiToolsSection() {
  const catalog = useApi<Catalog>("/api/ai/tools");
  const patchSettings = useApp((s) => s.patchSettings);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const { reload, setData } = catalog;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(() => void reload(), 400);
    };
    const offs = [onEvent("mcp.status", soon)];
    return () => {
      clearTimeout(timer);
      for (const off of offs) off();
    };
  }, [reload]);

  const choose = (tool: ToolInfo, c: Choice) => {
    const override = c === "auto" ? null : c;
    setData((cur) =>
      cur
        ? {
            ...cur,
            items: cur.items.map((t) =>
              t.policy_key === tool.policy_key ? { ...t, override, decision: override ?? t.default } : t,
            ),
          }
        : cur,
    );
    patchSettings({ ai: { tool_policy: { [tool.policy_key]: c } } }).catch((e) => {
      notifyError(e);
      void reload();
    });
  };

  const items = catalog.data?.items ?? [];
  const changed = items.filter((t) => t.override);
  const resetAll = () => {
    const patch: Record<string, Choice> = {};
    for (const t of changed) patch[t.policy_key] = "auto";
    setData((cur) => (cur ? { ...cur, items: cur.items.map((t) => ({ ...t, override: null, decision: t.default })) } : cur));
    patchSettings({ ai: { tool_policy: patch } }).catch((e) => {
      notifyError(e);
      void reload();
    });
  };

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((t) => {
      if (filter === "ask" && t.decision !== "ask") return false;
      if (filter === "deny" && t.decision !== "deny") return false;
      if (filter === "changed" && !t.override) return false;
      if (!q) return true;
      return `${t.title} ${t.description} ${t.name}`.toLowerCase().includes(q);
    });
  }, [items, query, filter]);

  const groups = catalog.data?.groups ?? [];
  return (
    <div className="stack">
      <Card
        icon={<Wrench />}
        title="Инструменты и разрешения"
        sub="Что Jarvis AI может делать сам — в чате и когда работает агент. «Авто»: читает и делает обычное без вопросов, рискованное спрашивает"
        actions={
          changed.length > 0 && (
            <Button size="sm" variant="ghost" icon={<RotateCcw />} onClick={resetAll}>
              Всё на «Авто»
            </Button>
          )
        }
      >
        <Note icon={<ShieldAlert />}>
          Отправку сообщений и писем, удаление, покупки, оплату и ввод паролей Jarvis спрашивает всегда — даже у
          разрешённого инструмента. «Запрещено» — ИИ не сможет вызвать инструмент совсем.
        </Note>
        {catalog.data?.agent_confirm === "always" && (
          <Note icon={<Bot />}>Агент сейчас спрашивает перед каждым действием («Jarvis AI → Агент → Спрашивать разрешение»).</Note>
        )}
        <div className="tool-toolbar">
          <TextInput value={query} onChange={setQuery} placeholder="Найти инструмент" icon={<Search />} />
          <Tabs<Filter>
            value={filter}
            items={[
              { value: "all", label: "Все" },
              { value: "ask", label: "Спрашивает" },
              { value: "deny", label: "Запрещено" },
              { value: "changed", label: `Изменённые${changed.length ? ` · ${changed.length}` : ""}` },
            ]}
            onChange={setFilter}
          />
        </div>
      </Card>

      {catalog.error && !catalog.data && <Note tone="err">Список инструментов не загрузился: {catalog.error.message}</Note>}
      {catalog.data && !visible.length && <div className="empty">Ничего не нашлось</div>}

      {groups.map((g) => {
        const list = visible.filter((t) => t.group === g.id);
        if (!list.length) return null;
        return (
          <Card key={g.id} icon={groupIcon(g.id)} title={g.title} sub={g.id.startsWith("mcp:") ? "Сервер MCP — видно, пока он подключён" : undefined}>
            <div className="tool-list">
              {list.map((t) => (
                <ToolRow key={t.policy_key} tool={t} onChoose={choose} />
              ))}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
