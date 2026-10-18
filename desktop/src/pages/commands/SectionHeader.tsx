// Заголовок раздела: «КОМАНДЫ», счётчик и вкладки «Все команды» / «Паки команд».
import { Package, SquareTerminal } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/PageHeader";
import { navigate } from "@/lib/router";
import { cx } from "@/ui";
import { countText, W_COMMANDS } from "./model";
import { useCommands, usePacks } from "./store";

export function SectionHeader({ tab, actions }: { tab: "list" | "packs"; actions?: ReactNode }) {
  const total = useCommands((s) => Math.max(s.total, s.items.length));
  const ready = useCommands((s) => s.status === "ready");
  const installed = usePacks((s) => s.items.filter((p) => p.installed).length);
  return (
    <PageHeader title="Команды" actions={actions}>
      {ready && <span className="chip accent cmd-total">{countText(total, W_COMMANDS)}</span>}
      <div className="tabs" role="tablist" aria-label="Раздел команд">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "list"}
          className={cx("tab", tab === "list" && "active")}
          onClick={() => navigate("commands")}
        >
          <SquareTerminal />
          Все команды
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "packs"}
          className={cx("tab", tab === "packs" && "active")}
          onClick={() => navigate("commands", "packs")}
        >
          <Package />
          Паки команд
          {installed > 0 && <span className="tab-count">{installed}</span>}
        </button>
      </div>
    </PageHeader>
  );
}
