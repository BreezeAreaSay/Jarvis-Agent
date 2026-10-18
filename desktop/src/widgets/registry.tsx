// Каталог виджетов: название, значок, размеры по умолчанию и компонент.
import {
  Activity,
  AlarmClock,
  AppWindow,
  BatteryMedium,
  Bitcoin,
  Calendar,
  CandlestickChart,
  CircleDot,
  Clock,
  Coins,
  Cpu,
  FileClock,
  Globe2,
  History,
  Home,
  LayoutGrid,
  ListChecks,
  Music2,
  Newspaper,
  NotebookPen,
  PawPrint,
  Puzzle,
  SlidersHorizontal,
  Sparkles,
  StickyNote,
  Sun,
  Timer,
  Volume2,
  Wifi,
  Zap,
} from "lucide-react";
import type { ComponentType, ReactNode } from "react";
import type { WidgetProps } from "./common";
import {
  AvatarWidget,
  CalendarWidget,
  ClockWidget,
  ControlWidget,
  HistoryWidget,
  MediaWidget,
  RatesWidget,
  SystemWidget,
  TasksWidget,
  WeatherWidget,
} from "./core";
import { EXTRA_WIDGETS } from "./extra";

export interface WidgetDef {
  type: string;
  title: string;
  description: string;
  icon: ReactNode;
  size: [number, number];
  min?: [number, number];
  max?: [number, number];
  component?: ComponentType<WidgetProps>;
}

const DEFS: WidgetDef[] = [
  { type: "avatar", title: "Аватар", description: "Живая сфера Jarvis", icon: <CircleDot />, size: [2, 3], min: [1, 2] },
  { type: "control", title: "Панель управления", description: "Микрофон, префикс, тихий режим", icon: <SlidersHorizontal />, size: [2, 2], min: [1, 2] },
  { type: "history", title: "История команд", description: "Что вы говорили и что выполнено", icon: <History />, size: [2, 2], min: [1, 2] },
  { type: "media", title: "Сейчас играет", description: "Плеер: трек, пауза, переключение", icon: <Music2 />, size: [2, 1], min: [2, 1] },
  { type: "weather", title: "Погода", description: "Температура и прогноз", icon: <Sun />, size: [1, 1] },
  { type: "tasks", title: "Задачи", description: "Список дел с галочками", icon: <ListChecks />, size: [1, 2], min: [1, 2] },
  { type: "notes", title: "Заметки", description: "Последние заметки", icon: <NotebookPen />, size: [1, 2], min: [1, 1] },
  { type: "calendar", title: "Календарь", description: "Месяц и события", icon: <Calendar />, size: [2, 2], min: [2, 2] },
  { type: "clock", title: "Часы", description: "Время и дата", icon: <Clock />, size: [1, 1] },
  { type: "timer", title: "Таймер", description: "Обратный отсчёт и помодоро", icon: <Timer />, size: [1, 1] },
  { type: "stopwatch", title: "Секундомер", description: "Замер времени", icon: <AlarmClock />, size: [1, 1] },
  { type: "rates", title: "Курсы валют", description: "Курсы ЦБ и изменение", icon: <Coins />, size: [1, 2], min: [1, 1] },
  { type: "crypto", title: "Криптовалюта", description: "Bitcoin, Ethereum, TON…", icon: <Bitcoin />, size: [1, 2], min: [1, 1] },
  { type: "stocks", title: "Акции", description: "Мосбиржа: Сбер, Газпром…", icon: <CandlestickChart />, size: [1, 2], min: [1, 1] },
  { type: "news", title: "Новости", description: "Свежие заголовки", icon: <Newspaper />, size: [2, 2], min: [1, 2] },
  { type: "system", title: "Система", description: "Процессор, видеокарта, память", icon: <Cpu />, size: [1, 1], min: [1, 1] },
  { type: "apps", title: "Программы", description: "Быстрый запуск", icon: <AppWindow />, size: [2, 2], min: [1, 1] },
  { type: "quick", title: "Быстрые команды", description: "Кнопки ваших команд", icon: <Zap />, size: [2, 1], min: [1, 1] },
  { type: "recent", title: "Недавние", description: "Последние файлы", icon: <FileClock />, size: [2, 2], min: [1, 1] },
  { type: "volume", title: "Громкость", description: "Звук системы", icon: <Volume2 />, size: [1, 1] },
  { type: "smarthome", title: "Умный дом", description: "Свет, розетки, климат", icon: <Home />, size: [2, 2], min: [1, 1] },
  { type: "pet", title: "Питомец", description: "Пиксельный друг на столе", icon: <PawPrint />, size: [1, 2], min: [1, 1] },
  { type: "ai", title: "Jarvis AI", description: "Быстрый вопрос ассистенту", icon: <Sparkles />, size: [2, 1], min: [2, 1] },
  { type: "worldclock", title: "Мировое время", description: "Часы в других городах", icon: <Globe2 />, size: [1, 2] },
  { type: "countdown", title: "Обратный отсчёт", description: "Дней до события", icon: <CalendarCountdownIcon />, size: [1, 1] },
  { type: "network", title: "Сеть", description: "Скорость загрузки и отдачи", icon: <Wifi />, size: [1, 1] },
  { type: "battery", title: "Батарея", description: "Заряд ноутбука", icon: <BatteryMedium />, size: [1, 1] },
  { type: "sticky", title: "Стикер", description: "Записка на столе", icon: <StickyNote />, size: [1, 1], min: [1, 1] },
  { type: "plugins", title: "Плагины", description: "Быстрые кнопки плагинов", icon: <Puzzle />, size: [2, 1], min: [1, 1] },
  { type: "desk_links", title: "Столы", description: "Переход на другие столы", icon: <LayoutGrid />, size: [2, 1], min: [1, 1] },
  { type: "activity", title: "Активность", description: "Сколько команд за день", icon: <Activity />, size: [1, 1] },
];

function CalendarCountdownIcon() {
  return <Calendar />;
}

const COMPONENTS: Record<string, ComponentType<WidgetProps>> = {
  avatar: AvatarWidget,
  control: ControlWidget,
  history: HistoryWidget,
  media: MediaWidget,
  weather: WeatherWidget,
  tasks: TasksWidget,
  rates: RatesWidget,
  clock: ClockWidget,
  system: SystemWidget,
  calendar: CalendarWidget,
  ...EXTRA_WIDGETS,
};

export const WIDGETS: WidgetDef[] = DEFS.map((d) => ({ ...d, component: COMPONENTS[d.type] }));
export const WIDGET_BY_TYPE: Record<string, WidgetDef> = Object.fromEntries(WIDGETS.map((d) => [d.type, d]));
