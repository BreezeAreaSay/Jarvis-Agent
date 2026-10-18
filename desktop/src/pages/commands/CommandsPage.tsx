// Раздел «Команды»: #/commands — список, #/commands/new · edit/<id> · copy/<id> — редактор,
// #/commands/packs[/<id>] — паки команд, #/commands/scenarios[/new|/<id>] — сценарии.
import "@/styles/commands.css";
import { useEffect } from "react";
import { CommandEditor } from "./CommandEditor";
import { CommandList } from "./CommandList";
import { PacksPage } from "./PacksPage";
import { ScenariosRoute } from "./ScenariosPage";
import { bindCommandEvents } from "./store";

export default function CommandsPage({ route }: { route: string[] }) {
  useEffect(() => bindCommandEvents(), []);
  const [section, arg] = route;
  if (section === "new") return <CommandEditor key="new" mode="new" />;
  if ((section === "edit" || section === "copy") && arg) return <CommandEditor key={`${section}:${arg}`} mode={section} id={arg} />;
  if (section === "packs") return <PacksPage packId={arg} />;
  if (section === "scenarios") return <ScenariosRoute route={route.slice(1)} />;
  return <CommandList />;
}
