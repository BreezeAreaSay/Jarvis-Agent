// Дополнительные виджеты (крипта, акции, новости, программы, быстрые команды, таймер и др.).
// Реализация — в папке ./extra; здесь только карта «тип → компонент» для реестра.
import "@/styles/extensions.css";
import "@/styles/widgets-extra.css";
import type { ComponentType } from "react";
import type { WidgetProps } from "./common";
import { CryptoWidget, StocksWidget } from "./extra/finance";
import { AppsWidget, QuickWidget, RecentWidget } from "./extra/launch";
import { AiWidget, DeskLinksWidget, PetWidget, PluginsWidget } from "./extra/misc";
import { NewsWidget } from "./extra/news";
import { NotesWidget, StickyWidget } from "./extra/notes";
import { SmartHomeWidget } from "./extra/smarthome";
import { ActivityWidget, BatteryWidget, NetworkWidget, VolumeWidget } from "./extra/system";
import { CountdownWidget, StopwatchWidget, TimerWidget, WorldClockWidget } from "./extra/time";

export const EXTRA_WIDGETS: Record<string, ComponentType<WidgetProps>> = {
  crypto: CryptoWidget,
  stocks: StocksWidget,
  news: NewsWidget,
  apps: AppsWidget,
  quick: QuickWidget,
  recent: RecentWidget,
  volume: VolumeWidget,
  smarthome: SmartHomeWidget,
  pet: PetWidget,
  ai: AiWidget,
  worldclock: WorldClockWidget,
  countdown: CountdownWidget,
  network: NetworkWidget,
  battery: BatteryWidget,
  sticky: StickyWidget,
  plugins: PluginsWidget,
  desk_links: DeskLinksWidget,
  notes: NotesWidget,
  timer: TimerWidget,
  stopwatch: StopwatchWidget,
  activity: ActivityWidget,
};
