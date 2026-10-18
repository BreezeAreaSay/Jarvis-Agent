// «РАСШИРЕНИЯ»: вкладки «Плагины» (#/extensions/plugins/<id>), «Подключения · MCP» (#/extensions/mcp,
// #/extensions/mcp/catalog) и «Паки команд» — переход в раздел команд.
import "@/styles/extensions.css";
import "@/styles/widgets-extra.css";
import { ArrowUpRight, Package, Plug, Puzzle } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { Tabs } from "@/ui";
import { McpTab } from "./McpTab";
import { PluginsTab } from "./PluginsTab";

type Tab = "plugins" | "mcp" | "packs";

export default function ExtensionsPage({ route }: { route: string[] }) {
  const tab: Tab = route[0] === "mcp" ? "mcp" : "plugins";
  return (
    <div className="page ext-page">
      <PageHeader title="Расширения">
        <Tabs<Tab>
          value={tab}
          onChange={(v) => (v === "packs" ? navigate("commands", "packs") : navigate("extensions", v))}
          items={[
            { value: "plugins", label: "Плагины", icon: <Puzzle /> },
            { value: "mcp", label: "Подключения · MCP", icon: <Plug /> },
            {
              value: "packs",
              label: (
                <>
                  Паки команд <ArrowUpRight className="tab-out" />
                </>
              ),
              icon: <Package />,
            },
          ]}
        />
      </PageHeader>
      <div className="page-body ext-body">
        {tab === "mcp" ? <McpTab sub={route[1]} /> : <PluginsTab selectedId={route[1]} />}
      </div>
    </div>
  );
}
