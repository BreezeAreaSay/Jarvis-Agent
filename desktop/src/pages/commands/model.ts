// Раздел «Команды»: общие помощники — категории, каталог действий (запасной), слоты, клавиши, сводки.
import {
  AppWindow,
  Clipboard,
  Command,
  Compass,
  Folder,
  Globe,
  House,
  Info,
  Keyboard,
  LayoutDashboard,
  LayoutGrid,
  type LucideIcon,
  Monitor,
  MousePointer2,
  Music,
  Package,
  Power,
  Puzzle,
  Rocket,
  Search,
  Sparkles,
  Star,
  Timer,
  Volume2,
  Workflow,
} from "lucide-react";
import type { ActionParam, ActionType, AppEntry, CommandAction, CommandInfo } from "@/api/types";

// --- числа и слова --------------------------------------------------------------------------------
/** Русское множественное число: plural(5, ["команда", "команды", "команд"]) → «команд». */
export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

export function countText(n: number, forms: [string, string, string]): string {
  return `${n.toLocaleString("ru-RU")} ${plural(n, forms)}`;
}

export const W_COMMANDS: [string, string, string] = ["команда", "команды", "команд"];
export const W_PACKS: [string, string, string] = ["пак", "пака", "паков"];
export const W_STEPS: [string, string, string] = ["шаг", "шага", "шагов"];
export const W_ACTIONS: [string, string, string] = ["действие", "действия", "действий"];

export function norm(s: string): string {
  return s.toLowerCase().replace(/ё/g, "е").trim();
}

/** Строки textarea → список без пустых и повторов. */
export function parseLines(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim().replace(/\s+/g, " ");
    if (!line || seen.has(norm(line))) continue;
    seen.add(norm(line));
    out.push(line);
  }
  return out;
}

// --- категории ---------------------------------------------------------------------------------------
export interface Category {
  id: string;
  title: string;
  count: number;
}

const CAT_TITLES: Record<string, string> = {
  all: "Все",
  user: "Мои команды",
  mine: "Мои команды",
  custom: "Мои команды",
  pack: "Паки команд",
  packs: "Паки команд",
  system: "Система",
  sound: "Звук",
  volume: "Громкость",
  audio: "Звук",
  media: "Медиа",
  music: "Медиа",
  windows: "Окна",
  window: "Окна",
  apps: "Программы",
  programs: "Программы",
  launch: "Программы",
  sites: "Сайты",
  web: "Сайты",
  search: "Поиск",
  keyboard: "Клавиатура",
  keys: "Клавиатура",
  browser: "Браузер",
  folders: "Папки",
  files: "Папки",
  info: "Информация",
  jarvis: "Джарвис",
  assistant: "Джарвис",
  timers: "Таймеры",
  timer: "Таймеры",
  mouse: "Мышь",
  power: "Питание",
  plugins: "Плагины",
  desks: "Столы",
  smarthome: "Умный дом",
  clipboard: "Буфер обмена",
  dynamic: "Динамические",
};

const CAT_ICON_BY_KEY: Record<string, LucideIcon> = {
  all: LayoutGrid,
  user: Star,
  pack: Package,
  system: Monitor,
  sound: Volume2,
  media: Music,
  windows: AppWindow,
  apps: Rocket,
  sites: Globe,
  search: Search,
  keyboard: Keyboard,
  browser: Compass,
  folders: Folder,
  info: Info,
  jarvis: Sparkles,
  timers: Timer,
  mouse: MousePointer2,
  power: Power,
  plugins: Puzzle,
  desks: LayoutDashboard,
  smarthome: House,
  clipboard: Clipboard,
  scenarios: Workflow,
};

/** Ключ категории по id или русскому названию (для значка и особых фильтров). */
export function categoryKey(id: string, title = ""): string {
  const byId: Record<string, string> = {
    mine: "user",
    custom: "user",
    my: "user",
    packs: "pack",
    volume: "sound",
    audio: "sound",
    music: "media",
    window: "windows",
    programs: "apps",
    launch: "apps",
    web: "sites",
    keys: "keyboard",
    files: "folders",
    assistant: "jarvis",
    timer: "timers",
  };
  const k = id.toLowerCase();
  if (CAT_ICON_BY_KEY[k]) return k;
  if (byId[k]) return byId[k];
  const t = norm(title);
  for (const [key, ru] of Object.entries(CAT_TITLES)) if (norm(ru) === t && CAT_ICON_BY_KEY[key]) return key;
  return k;
}

export function categoryIcon(id: string, title = ""): LucideIcon {
  return CAT_ICON_BY_KEY[categoryKey(id, title)] ?? Command;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function categoryTitle(id: string): string {
  return CAT_TITLES[id.toLowerCase()] ?? capitalize(id);
}

/** Подходит ли команда под категорию: «Мои» и «Паки» — по источнику, остальные — по полю category. */
export function inCategory(cmd: CommandInfo, catId: string): boolean {
  if (catId === "all") return true;
  if (cmd.category === catId) return true;
  const key = categoryKey(catId);
  if (key === "user") return cmd.source === "user";
  if (key === "pack") return cmd.source === "pack";
  return false;
}

/** Список категорий для боковой колонки: «Все», «Мои», «Паки», затем категории ядра. */
export function buildCategories(items: CommandInfo[], fromServer: Category[] | undefined): Category[] {
  const counted = new Map<string, number>();
  for (const c of items) counted.set(c.category, (counted.get(c.category) ?? 0) + 1);
  const base: Category[] =
    fromServer && fromServer.length
      ? fromServer
          .filter((c) => c.id !== "all")
          // ядро без русского названия отдаёт id с большой буквы («Mouse») — подставим своё
          .map((c) => ({ ...c, title: !c.title || c.title === capitalize(c.id) ? categoryTitle(c.id) : c.title }))
      : [...counted.entries()].map(([id, count]) => ({ id, title: categoryTitle(id), count }));
  const mine = base.find((c) => categoryKey(c.id, c.title) === "user");
  const packs = base.find((c) => categoryKey(c.id, c.title) === "pack");
  const rest = base.filter((c) => c !== mine && c !== packs);
  const userCount = items.filter((c) => c.source === "user").length;
  const packCount = items.filter((c) => c.source === "pack").length;
  return [
    { id: "all", title: "Все", count: items.length },
    mine ? { ...mine, count: Math.max(mine.count, userCount) } : { id: "user", title: "Мои команды", count: userCount },
    packs ? { ...packs, count: Math.max(packs.count, packCount) } : { id: "packs", title: "Паки команд", count: packCount },
    ...rest,
  ];
}

export function sourceLabel(cmd: CommandInfo, packName?: string): { text: string; tone: "mine" | "pack" | "base" } {
  if (cmd.source === "user") return { text: "Моя", tone: "mine" };
  if (cmd.source === "pack") return { text: `Пак «${packName || cmd.pack || "без названия"}»`, tone: "pack" };
  if (cmd.source === "dynamic") return { text: "Динамическая", tone: "base" };
  return { text: "Встроенная", tone: "base" };
}

/** Поиск: все слова запроса должны найтись в названии, фразах, id или действиях. */
export function matchesQuery(cmd: CommandInfo, words: string[], packName?: string): boolean {
  if (!words.length) return true;
  const hay = norm(
    [cmd.name, cmd.id, cmd.category, packName ?? cmd.pack ?? "", ...cmd.phrases, ...cmd.optional, ...cmd.actions.map((a) => a.type)].join(
      "\n",
    ),
  );
  return words.every((w) => hay.includes(w));
}

// --- слоты во фразах -------------------------------------------------------------------------------
export const SLOTS: { key: string; label: string; hint: string }[] = [
  { key: "number", label: "Число", hint: "«пятьдесят», «50», «50%»" },
  { key: "app", label: "Программа", hint: "из меню «Пуск»" },
  { key: "site", label: "Сайт", hint: "известный сайт" },
  { key: "text", label: "Текст", hint: "свободный хвост фразы" },
  { key: "query", label: "Запрос", hint: "что искать" },
  { key: "desk", label: "Стол", hint: "название стола" },
];

export function slotOf(value: string): string | null {
  const m = /^\{(\w+)\}$/.exec(value.trim());
  return m ? m[1] : null;
}

export function slotLabel(key: string): string {
  return SLOTS.find((s) => s.key === key)?.label ?? key;
}

// --- клавиши -------------------------------------------------------------------------------------
/** «ctrl+k ctrl+s» → «ctrl + k → ctrl + s» (сочетания через пробел — последовательность). */
export function formatKeys(value: string): string {
  return value
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((combo) =>
      combo
        .split("+")
        .map((k) => k.trim())
        .filter(Boolean)
        .join(" + "),
    )
    .join(" → ");
}

/** Обратно к записи ядра: «ctrl + k → ctrl + s» → «ctrl+k ctrl+s». */
export function normalizeKeys(value: string): string {
  return value
    .toLowerCase()
    .replace(/\s*→\s*/g, " ")
    .replace(/\s*\+\s*/g, "+")
    .replace(/\s+/g, " ")
    .trim();
}

/** e.code → имя клавиши, как его понимает ядро (jarvis/commands/keys.py). */
const CODE_NAMES: Record<string, string> = {
  Enter: "enter",
  NumpadEnter: "enter",
  Space: "space",
  Escape: "esc",
  Tab: "tab",
  Backspace: "backspace",
  Delete: "delete",
  Insert: "insert",
  Home: "home",
  End: "end",
  PageUp: "page_up",
  PageDown: "page_down",
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  PrintScreen: "print_screen",
  Pause: "pause",
  CapsLock: "caps_lock",
  ScrollLock: "scroll_lock",
  NumLock: "num_lock",
  ContextMenu: "menu",
  Minus: "minus",
  Equal: "equal",
  BracketLeft: "bracket_left",
  BracketRight: "bracket_right",
  Semicolon: "semicolon",
  Quote: "quote",
  Backquote: "backquote",
  Comma: "comma",
  Period: "period",
  Slash: "slash",
  Backslash: "backslash",
  IntlBackslash: "backslash",
  NumpadAdd: "num_add",
  NumpadSubtract: "num_subtract",
  NumpadMultiply: "num_multiply",
  NumpadDivide: "num_divide",
  NumpadDecimal: "num_decimal",
  MediaPlayPause: "play_pause",
  MediaTrackNext: "next_track",
  MediaTrackPrevious: "prev_track",
  MediaStop: "media_stop",
  AudioVolumeUp: "volume_up",
  AudioVolumeDown: "volume_down",
  AudioVolumeMute: "volume_mute",
  BrowserBack: "browser_back",
  BrowserForward: "browser_forward",
  BrowserRefresh: "browser_refresh",
  BrowserHome: "browser_home",
  BrowserSearch: "browser_search",
};

const MODIFIER_CODES = new Set([
  "ControlLeft",
  "ControlRight",
  "ShiftLeft",
  "ShiftRight",
  "AltLeft",
  "AltRight",
  "MetaLeft",
  "MetaRight",
  "OSLeft",
  "OSRight",
]);

export function isModifierEvent(e: { code: string; key: string }): boolean {
  return MODIFIER_CODES.has(e.code) || ["Control", "Shift", "Alt", "Meta", "OS"].includes(e.key);
}

/** Сочетание из события клавиатуры: «ctrl+shift+s». По коду клавиши — не зависит от раскладки. */
export function comboFromEvent(e: {
  code: string;
  key: string;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  metaKey: boolean;
}): string {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push("ctrl");
  if (e.shiftKey) parts.push("shift");
  if (e.altKey) parts.push("alt");
  if (e.metaKey) parts.push("win");
  if (!isModifierEvent(e)) {
    const c = e.code;
    let name: string | undefined;
    if (/^Key[A-Z]$/.test(c)) name = c.slice(3).toLowerCase();
    else if (/^Digit[0-9]$/.test(c)) name = c.slice(5);
    else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(c)) name = c.toLowerCase();
    else if (/^Numpad[0-9]$/.test(c)) name = `num${c.slice(6)}`;
    else name = CODE_NAMES[c] ?? (e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase());
    if (name) parts.push(name);
  }
  return parts.join("+");
}

// --- каталог действий ------------------------------------------------------------------------------
const p = (kind: ActionParam["kind"], label: string, placeholder?: string, options?: ActionParam["options"]): ActionParam => ({
  name: "value",
  kind,
  label,
  placeholder,
  options,
});

const a = (
  type: string,
  title: string,
  category: string,
  icon: string,
  params: ActionParam[] = [],
  description = "",
): ActionType => ({ type, title, category, icon, params, description });

/** Запасной каталог действий (как в ядре: jarvis/commands/catalog.py) — пока ядро не отдаёт GET /api/commands/actions. */
export const FALLBACK_ACTIONS: ActionType[] = [
a("Launch.App", "Запустить программу", "Запуск", "apps", [p("app", "Программа", "Spotify")], "Программа из меню «Пуск» по названию («Spotify», «OBS Studio») или путь к exe."),
  a("Launch.File", "Открыть файл", "Запуск", "folder", [p("path", "Файл", "C:\\Program Files\\…\\program.exe")], "Файл или программа по пути. Аргументы — после пути в кавычках: \"C:\\…\\obs64.exe\" --startrecording"),
  a("Launch.Url", "Открыть сайт", "Запуск", "site", [p("url", "Адрес", "https://youtube.com")], "Адрес сайта, ссылка приложения (spotify:, steam://…) или название сайта («ютуб»)."),
  a("Launch.Folder", "Открыть папку", "Запуск", "folder", [p("path", "Папка", "downloads или C:\\Users\\…")], "Путь или особая папка: downloads, documents, desktop, pictures, music, videos."),
  a("Launch.Shell", "Команда консоли", "Запуск", "keyboard", [p("text", "Команда", "shutdown /a")], "Строка для командной оболочки Windows. Только в своих командах."),
  a("App.Close", "Закрыть программу", "Программы", "window", [p("app", "Программа", "Telegram")], "Мягко закрыть окна программы (как крестиком)."),
  a("App.Kill", "Завершить процесс", "Программы", "error", [p("app", "Процесс", "chrome.exe")], "Принудительно завершить процесс — несохранённое пропадёт."),
  a("App.Focus", "Переключиться на программу", "Программы", "window", [p("app", "Программа", "Discord")], "Вывести окно программы на передний план (нужно перед горячими клавишами программы)."),
  a("Ui.Click", "Нажать кнопку в окне", "Программы", "mouse", [p("text", "Кнопка, ссылка или пункт", "Отправить")], "Нажать элемент окна впереди по названию (как его называет Windows); пока его нет — подождать. Не нашёлся — дело передаётся агенту."),
  a("Ui.Type", "Ввести текст в поле", "Программы", "keyboard", [p("text", "Поле | текст", "Кому | ivan@example.com")], "Название поля окна впереди и текст через «|»: содержимое поля заменится."),
  a("Keys.Press", "Нажать клавиши", "Клавиатура", "keyboard", [p("keys", "Сочетание", "ctrl+shift+s")], "Сочетание или несколько через пробел: «ctrl+k ctrl+s», повтор — «down*3»."),
  a("Keys.Type", "Напечатать текст", "Клавиатура", "keyboard", [p("text", "Текст", "Что напечатать")], "Ввести текст посимвольно в активное окно."),
  a("Keys.Paste", "Вставить текст", "Клавиатура", "keyboard", [p("text", "Текст", "Что вставить")], "Вставить текст через буфер обмена (быстро, буфер потом восстанавливается)."),
  a("Mouse.ClickLeft", "Клик левой кнопкой", "Мышь", "mouse", [p("coords", "Точка на экране", "960 540 (пусто — где сейчас курсор)")], "Координаты «x y» или пусто."),
  a("Mouse.ClickRight", "Клик правой кнопкой", "Мышь", "mouse", [p("coords", "Точка на экране", "960 540 (пусто — где сейчас курсор)")], "Координаты «x y» или пусто."),
  a("Mouse.DoubleClick", "Двойной клик", "Мышь", "mouse", [p("coords", "Точка на экране", "960 540 (пусто — где сейчас курсор)")], "Координаты «x y» или пусто."),
  a("Mouse.Move", "Переместить курсор", "Мышь", "mouse", [p("coords", "Точка на экране", "960 540")], "Переместить курсор в точку «x y»."),
  a("Mouse.Scroll", "Прокрутить колесо", "Мышь", "mouse", [p("number", "Щелчков (минус — вниз)", "-5")], "Положительное число — вверх, отрицательное — вниз."),
  a("System.VolumeSet", "Громкость", "Звук", "volume", [p("number", "Громкость, %", "35")], "Установить громкость 0–100."),
  a("System.VolumeUp", "Громче", "Звук", "volume", [p("number", "На сколько, %", "10")], "Прибавить громкость (по умолчанию на 10)."),
  a("System.VolumeDown", "Тише", "Звук", "volume", [p("number", "На сколько, %", "10")], "Убавить громкость (по умолчанию на 10)."),
  a("System.Mute", "Выключить звук", "Звук", "mute"),
  a("System.Unmute", "Включить звук", "Звук", "volume"),
  a("System.MuteToggle", "Звук вкл/выкл", "Звук", "mute"),
  a("Media.PlayPause", "Пауза / продолжить", "Медиа", "media", [p("select", "Что сделать", "", [{ value: "", label: "Переключить" }, { value: "play", label: "Только включить" }, { value: "pause", label: "Только пауза" }])], "Клавиша Play/Pause. «Только включить» запустит музыку, если ничего не играет."),
  a("Media.Next", "Следующий трек", "Медиа", "media"),
  a("Media.Prev", "Предыдущий трек", "Медиа", "media"),
  a("Media.Stop", "Остановить", "Медиа", "media"),
  a("Window.MinimizeAll", "Свернуть все окна", "Окна", "window"),
  a("Window.Minimize", "Свернуть окно", "Окна", "window", [p("app", "Чьё окно", "пусто — активное")], "Пусто — активное окно, иначе — окно программы."),
  a("Window.Maximize", "Развернуть окно", "Окна", "window", [p("app", "Чьё окно", "пусто — активное")]),
  a("Window.Restore", "Восстановить окно", "Окна", "window", [p("app", "Чьё окно", "пусто — активное")]),
  a("Window.Close", "Закрыть окно", "Окна", "window", [p("app", "Чьё окно", "пусто — активное")]),
  a("System.BrightnessSet", "Яркость", "Система", "system", [p("number", "Яркость, %", "70")], "0–100; «+10» / «-10» — изменить относительно текущей."),
  a("System.Lock", "Заблокировать компьютер", "Система", "lock"),
  a("System.Sleep", "Спящий режим", "Система", "power"),
  a("System.Shutdown", "Выключить компьютер", "Система", "power", [p("number", "Через, секунд", "0")], "Задержка в секундах (или «5m» — минуты)."),
  a("System.Restart", "Перезагрузить компьютер", "Система", "power", [p("number", "Через, секунд", "0")]),
  a("System.CancelShutdown", "Отменить выключение", "Система", "power"),
  a("System.Screenshot", "Снимок экрана", "Система", "screenshot", [p("select", "Что снять", "", [{ value: "", label: "Весь экран" }, { value: "window", label: "Активное окно" }])], "Снимок сохраняется в «Изображения\\Screenshots»."),
  a("System.EmptyRecycleBin", "Очистить корзину", "Система", "system"),
  a("Wait", "Пауза", "Сценарий", "timer", [p("number", "Секунд", "2")], "Подождать перед следующим шагом."),
  a("Speak", "Сказать вслух", "Сценарий", "ai", [p("text", "Текст", "Готово, сэр")]),
  a("Notify", "Уведомление", "Сценарий", "bell", [p("text", "Текст", "Текст уведомления")]),
  a("Clipboard.Set", "Скопировать в буфер", "Сценарий", "note", [p("text", "Текст", "Что скопировать")]),
  a("Web.Search", "Поиск в интернете", "Поиск", "search", [p("text", "Запрос", "google: {query}")], "Запрос с приставкой поисковика: google:, yandex:, youtube:, bing:, duckduckgo:, wikipedia:, images:, maps: или сайта (avito:, ozon:…). Без приставки — Google."),
  a("Info.Time", "Сказать время", "Информация", "clock"),
  a("Info.Date", "Сказать дату", "Информация", "calendar"),
  a("Info.Weather", "Сказать погоду", "Информация", "weather", [p("text", "Город", "как в профиле")]),
  a("Info.Volume", "Сказать громкость", "Информация", "volume"),
  a("Info.Media", "Что сейчас играет", "Информация", "media"),
  a("Info.Battery", "Заряд батареи", "Информация", "system"),
  a("Info.System", "Загрузка компьютера", "Информация", "system"),
  a("Jarvis.OpenDesk", "Открыть стол", "Джарвис", "desk", [p("text", "Стол", "Игры")]),
  a("Jarvis.Ask", "Спросить Jarvis AI", "Джарвис", "ai", [p("text", "Вопрос", "{text}")]),
  a("Jarvis.Command", "Выполнить команду", "Джарвис", "command", [p("text", "Команда", "builtin.media.play или «сверни все окна»")], "id другой команды или её фраза."),
  a("Jarvis.Plugin", "Действие плагина", "Джарвис", "plugin", [p("text", "Действие", "dictation:toggle")], "Плагин и действие через двоеточие: dictation:toggle, reader:read, summary:start."),
  a("Jarvis.SilentMode", "Тихий режим", "Джарвис", "mute", [p("select", "Режим", "", [{ value: "toggle", label: "Переключить" }, { value: "on", label: "Включить" }, { value: "off", label: "Выключить" }])]),
  a("Jarvis.ShowWindow", "Окно Jarvis", "Джарвис", "home", [p("select", "Что сделать", "", [{ value: "show", label: "Показать" }, { value: "hide", label: "Скрыть" }, { value: "settings", label: "Открыть настройки" }, { value: "commands", label: "Открыть команды" }, { value: "chat", label: "Открыть чат" }])]),
  a("Jarvis.Timer", "Таймер", "Джарвис", "timer", [p("text", "Через", "5m")], "Длительность: «90» — секунды, «5m» — минуты, «1h» — часы."),
  a("Jarvis.Reminder", "Напоминание", "Джарвис", "note", [p("text", "Через и текст", "10m | Позвонить маме")], "Через сколько и о чём: «10m | Позвонить маме»."),
];

/** Действия, у которых значение обязательно (в ядре — ActionType.required). */
const REQUIRED = new Set([
  "Launch.App",
  "Launch.File",
  "Launch.Url",
  "Launch.Folder",
  "Launch.Shell",
  "App.Close",
  "App.Kill",
  "App.Focus",
  "Ui.Click",
  "Ui.Type",
  "Keys.Press",
  "Keys.Type",
  "Keys.Paste",
  "Mouse.Move",
  "System.VolumeSet",
  "System.BrightnessSet",
  "Wait",
  "Speak",
  "Notify",
  "Clipboard.Set",
  "Web.Search",
  "Jarvis.OpenDesk",
  "Jarvis.Ask",
  "Jarvis.Command",
  "Jarvis.Plugin",
  "Jarvis.Timer",
  "Jarvis.Reminder",
]);

export function isRequired(at: ActionType | undefined): boolean {
  if (!at || !at.params.length) return false;
  const flag = (at as ActionType & { required?: boolean }).required;
  return typeof flag === "boolean" ? flag : REQUIRED.has(at.type);
}

export function unknownAction(type: string): ActionType {
  return { type, title: type, category: "Другое", icon: "command", params: [p("text", "Значение")], description: "" };
}

/** Группы для выпадающего списка: порядок категорий — как в каталоге. */
export function groupCatalog(items: ActionType[]): { category: string; items: ActionType[] }[] {
  const groups = new Map<string, ActionType[]>();
  for (const it of items) {
    const g = groups.get(it.category);
    if (g) g.push(it);
    else groups.set(it.category, [it]);
  }
  return [...groups.entries()].map(([category, list]) => ({ category, items: list }));
}

// --- значения действий ------------------------------------------------------------------------------
export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** Программа из списка «Пуск», которой соответствует путь или имя. */
export function findApp(apps: AppEntry[], value: string): AppEntry | undefined {
  if (!value) return undefined;
  const v = norm(value);
  return (
    apps.find((app) => app.path && norm(app.path) === v) ??
    apps.find((app) => norm(app.app_id) === v) ??
    apps.find((app) => norm(app.name) === v) ??
    apps.find((app) => app.path && norm(basename(app.path)) === v)
  );
}

/** «Spotify.exe» → «Spotify» (в паке app — имя процесса). */
export function appLabel(app: string): string {
  return basename(app || "").replace(/\.(exe|lnk)$/i, "");
}

export function urlHost(url: string): string {
  try {
    return new URL(/^[a-z]+:\/\//i.test(url) ? url : `https://${url}`).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Значение действия для людей: «Spotify», «ctrl + shift + s», «2 с», «из фразы: число». */
export function valueText(action: CommandAction, at: ActionType | undefined, apps: AppEntry[]): string {
  const v = (action.value ?? "").trim();
  if (!v) return "";
  const slot = slotOf(v);
  if (slot) return `из фразы: ${slotLabel(slot).toLowerCase()}`;
  const kind = at?.params[0]?.kind;
  if (kind === "keys") return formatKeys(v);
  if (kind === "path") return findApp(apps, v)?.name ?? basename(v);
  if (kind === "url") return urlHost(v);
  if (kind === "app") return findApp(apps, v)?.name ?? v;
  if (kind === "select") return at?.params[0]?.options?.find((o) => o.value === v)?.label ?? v;
  if (action.type === "Wait") return `${v} с`;
  if (/Volume|Brightness/.test(action.type) && /^\d+$/.test(v)) return `${v}%`;
  return v;
}

export function actionLine(action: CommandAction, at: ActionType | undefined, apps: AppEntry[]): string {
  const title = at?.title ?? action.type;
  const value = valueText(action, at, apps);
  return value ? `${title}: ${value}` : title;
}

/** Значок шага: программа — её значок, остальное — значок типа действия. */
export function actionIcon(action: CommandAction, at: ActionType | undefined, apps: AppEntry[]): string {
  const kind = at?.params[0]?.kind;
  const v = (action.value ?? "").trim();
  if ((kind === "app" || kind === "path") && v && !slotOf(v)) {
    const app = findApp(apps, v);
    return app ? app.name : kind === "app" ? v : basename(v).replace(/\.(exe|lnk)$/i, "") || at?.icon || "command";
  }
  if (action.type === "Launch.Url" && v && !slotOf(v)) return urlHost(v).split(".")[0] || "site";
  return at?.icon || "command";
}

/** Пометка шага в «Создать с помощью AI»: откуда взялось значение («из меню «Пуск»», «медиаклавиша»). */
export function stepNote(action: CommandAction, at: ActionType | undefined, apps: AppEntry[]): string {
  const t = action.type;
  const v = (action.value ?? "").trim();
  if (slotOf(v)) return "из фразы";
  if (t.startsWith("Media.")) return "медиаклавиша";
  if (t === "Launch.App" || t === "App.Focus" || t === "App.Close") {
    const app = findApp(apps, v);
    if (!app) return "по названию";
    return app.source === "start" ? "из меню «Пуск»" : "из установленных";
  }
  if (t === "Launch.File" || t === "Launch.Folder") return "по пути";
  if (t === "Launch.Url") return "сайт";
  if (t === "Keys.Press") return "сочетание клавиш";
  if (t === "Wait") return "как вы сказали";
  return at?.category ? at.category.toLowerCase() : t;
}

/** Ключ для React у действий в редакторе. */
let actionSeq = 0;
export function newKey(): string {
  actionSeq += 1;
  return `a${Date.now().toString(36)}${actionSeq}`;
}

/** Набор значков для выбора (ключи из docs/CONTRACTS.md). */
export const ICON_CHOICES: { key: string; label: string }[] = [
  { key: "", label: "Авто" },
  { key: "command", label: "Команда" },
  { key: "spotify", label: "Spotify" },
  { key: "chrome", label: "Chrome" },
  { key: "telegram", label: "Telegram" },
  { key: "discord", label: "Discord" },
  { key: "obs", label: "OBS" },
  { key: "steam", label: "Steam" },
  { key: "youtube", label: "YouTube" },
  { key: "vk", label: "VK" },
  { key: "yandex", label: "Яндекс" },
  { key: "twitch", label: "Twitch" },
  { key: "photoshop", label: "Photoshop" },
  { key: "vscode", label: "VS Code" },
  { key: "minecraft", label: "Minecraft" },
  { key: "explorer", label: "Проводник" },
  { key: "volume", label: "Звук" },
  { key: "mute", label: "Без звука" },
  { key: "media", label: "Музыка" },
  { key: "window", label: "Окна" },
  { key: "power", label: "Питание" },
  { key: "lock", label: "Блокировка" },
  { key: "screenshot", label: "Снимок" },
  { key: "keyboard", label: "Клавиатура" },
  { key: "mouse", label: "Мышь" },
  { key: "search", label: "Поиск" },
  { key: "weather", label: "Погода" },
  { key: "timer", label: "Таймер" },
  { key: "note", label: "Заметка" },
  { key: "task", label: "Задача" },
  { key: "calendar", label: "Календарь" },
  { key: "desk", label: "Стол" },
  { key: "ai", label: "ИИ" },
  { key: "home", label: "Дом" },
  { key: "plugin", label: "Плагин" },
  { key: "folder", label: "Папка" },
  { key: "site", label: "Сайт" },
  { key: "system", label: "Система" },
];
