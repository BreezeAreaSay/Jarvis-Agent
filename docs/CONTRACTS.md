# Контракты Jarvis: ядро ↔ интерфейс, модули ядра

Документ — договор между частями. Меняешь маршрут, событие или формат — правь этот файл в том же коммите.

## 0. Устройство

```
desktop/ (Electron + React)                     core/ (Python 3.12, FastAPI)
  main process: окна, трей, хоткеи, ядро  ──spawn──►  python -m jarvis --port 0 --parent-pid <pid>
  renderer: главное окно, оверлей, аватар  ◄─HTTP /api/*, WebSocket /ws (события)─►  сервисы ядра
```

- Ядро печатает в stdout `JARVIS_CORE_READY {"port": N, "token": "…", "pid": …, "version": "…"}`.
- Все `/api/*` (кроме `/api/health`) требуют заголовок `X-Jarvis-Token: <token>`; `/ws?token=<token>`.
- Ошибки: HTTP 400 `{"detail": "текст для человека"}`, 404, 422 (валидация).
- Ответы — JSON, ключи snake_case, время — unix-секунды (float).

### Правила кода ядра

- Python 3.12, аннотации типов, ruff (line-length 110). Идентификаторы — по-английски; докстринги, комментарии и
  всё, что видит человек, — по-русски.
- `import jarvis.<модуль>` обязан работать на Linux: Windows-библиотеки (`win32*`, `pycaw`, `comtypes`,
  `uiautomation`, `winrt`, `ctypes.windll`) — только лениво внутри функций или под `sys.platform == "win32"`.
  Тяжёлые/системные библиотеки (`sounddevice`, `soundcard`, `pynput`, `sherpa_onnx`, `cv2`) — тоже лениво.
- Действия с ОС — только через `ctx.platform` (протокол `jarvis.platform.base.Platform`) и только из потока
  платформы: `await ctx.run_platform(ctx.platform.volume_set, 50)`. Прочую блокирующую работу —
  `await ctx.run_io(fn, …)`.
- Платформа Windows: координаты — физические пиксели (процесс DPI-aware); `screen_size()` — основной монитор,
  `virtual_screen()` — все мониторы (для снимков); `foreground_window()` и действия без цели не трогают окна Jarvis;
  `close_app`/`kill_process` возвращают 0, если ничего не нашлось; ввод от Jarvis помечен `dwExtraInfo = 0x4A525653`
  (запись действий и «держи и говори» его пропускают); свои процессы — дерево от `JARVIS_ROOT_PID` (= --parent-pid).
- События — `ctx.bus.publish(type, data)` (из любого потока); данные — JSON-сериализуемые.
- Ошибка для человека — `raise UserFacingError("…")` (API отдаст 400 с этим текстом).
- База — `ctx.db` (таблицы в `jarvis/db.py`; новые таблицы не заводить — для своего состояния есть `kv`
  с ключами `<область>:<имя>` и JSON-файлы в `paths.sub_dir("<область>")`).
- Сеть — `httpx.AsyncClient` (прокси для ИИ — `settings.ai.proxy`). В тестах сети нет: `httpx.MockTransport`.
- Тесты — `core/tests/<область>/`, фикстуры из `tests/conftest.py` (`ctx`, `running_ctx`, `client`,
  `events`). Тесты проходят на Linux без микрофона, моделей и сети.
- Новая зависимость: `uv pip install <пакет>` в `.venv` и строка в отчёте (pyproject правит интегратор).

## 1. Ядро (общие маршруты)

| Маршрут | Тело / параметры | Ответ |
|---|---|---|
| `GET /api/health` | — | `{ok, version}` (без токена) |
| `GET /api/status` | — | `{version, platform, data_dir, uptime_s, voice: {state}, ai: {configured}, ready}` |
| `GET /api/settings` | — | `{settings: Settings, secrets: {name: bool}}` |
| `PATCH /api/settings` | частичный Settings | `{settings}` (422 — неверное значение) |
| `PUT /api/secrets` | `{name, value}` (`""` — удалить) | `{secrets}` |
| `POST /api/dispatch` | `{text, source?: "text"\|"ui"\|"voice"\|"remote", speak?: bool}` | итог (см. ниже) |
| `GET /api/history?limit=50` | — | `{items: HistoryItem[]}` (новые первыми) |
| `DELETE /api/history` | — | `{ok}` |
| `GET /api/confirm` | — | `{items: Confirmation[]}` |
| `POST /api/confirm/{id}` | `{allow: bool}` | `{ok}` |

Settings — модель `jarvis/settings.py` (в интерфейсе — `desktop/src/api/types.ts`). Секреты:
`ai_api_key, openai_api_key, fish_api_key, telegram_token, ha_token, yandex_iot_token, remote_token`.

Итог `/api/dispatch`: `{kind: "command", result: ExecResult, history}` | `{kind: "ai", chat_id, text, actions,
sources, error, history}` | `{kind: "unknown", history}` | `{kind: "stop"}` | `{kind: "empty"}`.

`HistoryItem = {id, ts, source, text, kind: command|ai|unknown|system, title, status: ok|error|pending|cancelled,
detail, icon, command_id}`.

`ExecResult = {ok, title, message, icon, say, command_ids, data}`; `data.silent` — не показывать тост,
`data.cancelled` — отменено человеком.

`Confirmation = {id, title, detail, kind: command|agent|plugin, source, created, timeout}`.

### События ядра

| Событие | Данные |
|---|---|
| `hello` | первое сообщение WS: `{version, platform, settings, secrets, voice: {state}, ai: {configured}}` |
| `core.ready` | `{}` |
| `settings.changed` | `{settings}` |
| `secrets.changed` | `{name, set}` |
| `dispatch.started` / `dispatch.done` | `{text, source}` / `{text, kind, ok?}` |
| `history.added` / `history.updated` | `{item: HistoryItem}` |
| `history.cleared` | `{}` |
| `command.executed` | `{text, source, result: ExecResult}` |
| `toast` | `{title, message, status: ok\|error\|info, icon, ts}` — карточка в оверлее («Включить музыку — Выполнено ✓») |
| `confirm.request` | `Confirmation` — карточка «Отклонить / Разрешить» |
| `confirm.resolved` | `{id, allow}` |

Иконки (`icon`): ключ бренда или категории — `spotify, chrome, telegram, discord, obs, steam, youtube, vk,
yandex, photoshop, vscode, minecraft, explorer, volume, mute, media, window, power, lock, screenshot, keyboard,
mouse, search, weather, timer, note, task, calendar, desk, ai, home, plugin, error, unknown, system`.
Интерфейс показывает для ключа значок; неизвестный ключ — общий.

## 2. Команды (`jarvis/commands`, `jarvis/api/commands.py`)

### Формат команды

```json
{
  "id": "user.stream_mode",
  "name": "Режим стрима",
  "category": "user",
  "phrases": ["включи режим стрима", "режим стрима"],
  "optional": ["пожалуйста", "быстро"],
  "actions": [
    {"type": "Launch.File", "value": "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe"},
    {"type": "Keys.Press", "value": "ctrl+shift+s"},
    {"type": "Wait", "value": "2"},
    {"type": "Mouse.ClickLeft", "value": "960 540"},
    {"type": "System.VolumeSet", "value": "35"}
  ],
  "confirm": false,
  "chain": true,
  "icon": "obs",
  "enabled": true,
  "say": ""
}
```

- `phrases` — фразы активации (обязательная часть), `optional` — слова, которые можно добавить («пожалуйста»).
- Шаблоны слотов в фразах: `{number}` (число словами или цифрами: «пятьдесят», «50», «50%», «половину» → 50,
  «на максимум» → 100), `{app}` (программа из меню «Пуск»), `{site}` (известный сайт или адрес «vk.com»), `{desk}`
  (стол), `{text}` / `{query}` (свободный хвост фразы), `{keys}` (клавиши словами: «контрол шифт эс»).
  В `value` действий слоты подставляются: `"value": "{number}"`. В `slots` совпадения: `number` — число,
  `app` — название (+ `app_id`, `exe`), `site` — название (+ `url`), `desk`, `text`/`query`, `keys` — «ctrl+s».
- `confirm` — спросить подтверждение перед выполнением; `chain` («Связывать») — команду можно соединять с другими
  в одной фразе («сверни все окна и включи музыку»; фраза делится, только если каждая часть — команда с `chain`).
- `id`: встроенные — `builtin.<имя>`, паков — `pack.<pack>.<имя>`, свои — `user.<slug>`, динамические —
  `<провайдер>:<ключ>` (в путях API — как есть, с «:»).
- Разбор: порог похожести 82 (фраза с вопросительного слова — 90); при равной похожести фраза без слотов важнее
  шаблона со слотом, затем источник: свои → паки → динамические → встроенные. Шаблон со свободным хвостом
  (`{text}`/`{query}`) уступает фразе без слотов. Вопросы и длинные просьбы, не совпавшие с командой, идут ИИ.
- Динамическая команда получает слоты с `"_source"` (откуда фраза: `voice`, `text`, `ui`…).

### Действия (`type`)

`Launch.File, Launch.App, Launch.Url, Launch.Folder, Launch.Shell, App.Close, App.Kill, App.Focus, Keys.Press,
Keys.Type, Keys.Paste, Mouse.ClickLeft, Mouse.ClickRight, Mouse.DoubleClick, Mouse.Move, Mouse.Scroll,
System.VolumeSet, System.VolumeUp, System.VolumeDown, System.Mute, System.Unmute, System.MuteToggle,
System.BrightnessSet, System.Lock, System.Sleep, System.Shutdown, System.Restart, System.CancelShutdown,
System.Screenshot, System.EmptyRecycleBin, Media.PlayPause, Media.Next, Media.Prev, Media.Stop,
Window.MinimizeAll, Window.Minimize, Window.Maximize, Window.Restore, Window.Close, Wait, Speak, Notify,
Clipboard.Set, Web.Search, Info.Time, Info.Date, Info.Weather, Jarvis.OpenDesk, Jarvis.Ask, Jarvis.Command,
Jarvis.Plugin, Jarvis.SilentMode, Jarvis.ShowWindow` и для встроенных команд `Info.Volume, Info.Media,
Info.Battery, Info.System, Jarvis.Timer, Jarvis.Reminder`. Каталог с описанием параметров —
`GET /api/commands/actions`.

Значения: `Keys.Press` — «ctrl+shift+s», несколько через пробел («ctrl+k ctrl+s»), повтор «down*3»; `Wait`,
`System.Shutdown`/`Restart` — секунды или «5m», «1h»; `System.VolumeSet`/`BrightnessSet` — 0–100 или «+10»/«-10»;
`Mouse.*` — «x y» (пусто — где курсор, `center` — центр экрана); `Media.PlayPause` — «» (переключить), `play`,
`pause`; `Web.Search` — «google: запрос» (`google, yandex, youtube, bing, duckduckgo, wikipedia, images, maps` или
сайт с поиском: `avito, ozon…`); `Launch.Folder` — путь или `downloads, documents, desktop, pictures, music, videos,
screenshots`; `Launch.Shell` — только в своих командах; `Jarvis.Command` — id команды или её фраза;
`Jarvis.Plugin` — «плагин:действие» («dictation:toggle»), аргументы JSON после «|»; `Jarvis.SilentMode` — `on`,
`off`, `toggle`; `Jarvis.ShowWindow` — `show`, `hide` или страница (`settings`, `commands`, `chat`);
`Jarvis.Timer` — «5m»; `Jarvis.Reminder` — «10m | Позвонить маме».

### Маршруты

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/commands` | `?q=&category=` | `{items: CommandInfo[], categories: [{id, title, count}], total}` |
| `GET /api/commands/{id}` | — | `CommandInfo` |
| `POST /api/commands` | `CommandIn` | `CommandInfo` (своя команда) |
| `PUT /api/commands/{id}` | `CommandIn` | `CommandInfo` |
| `PATCH /api/commands/{id}` | `{enabled}` | `CommandInfo` (так выключаются и встроенные) |
| `DELETE /api/commands/{id}` | — | `{ok}` |
| `POST /api/commands/{id}/run` | `{slots?}` | `ExecResult` |
| `POST /api/commands/test` | `{text}` | `{match: {text, parts: [{command_id, title, text, score, slots}]} \| null}` |
| `GET /api/commands/actions` | — | `{items: ActionType[]}` |
| `POST /api/commands/record/start` | — | `{ok}` |
| `POST /api/commands/record/stop` | — | `{actions: Action[]}` |
| `POST /api/commands/build` | `{description}` | `{name, phrases, actions, steps: [{title, detail}], skipped: [string]}` — «Создать с помощью AI» (команда не сохраняется; `skipped` — непонятые части описания) |
| `GET /api/packs` | — | `{items: Pack[], totals: {packs, commands}}` |
| `POST /api/packs/{id}/install` / `DELETE /api/packs/{id}` | — | `Pack` |
| `GET /api/apps?refresh=0` | — | `{items: [{name, app_id, path, source}]}` |

`{id}` — с точками и двоеточиями («desks:abc»); неизвестный id или пак — 404, ошибка проверки — 400 с текстом.
`GET /api/commands`: `items` — с учётом `q` (все слова в названии, id, фразах) и `category` (`user`/`packs` —
по источнику); `categories` и `total` — по всем командам; `title` категории — по-русски («Громкость», «Мышь»,
«Столы», «Плагины»…), незнакомая — id с заглавной буквы.
`CommandInfo = Command + {source: builtin|pack|user|dynamic, pack?, editable}` (у динамических `actions: []`).
`CommandIn = {name, phrases, optional?, actions, confirm?, chain?, icon?, say?, enabled? (по умолчанию true)}` —
`category` своих команд всегда `user`.
`ActionType = {type, title, category, icon, params: [{name, kind: text|number|keys|path|app|url|select|coords,
label, placeholder?, options?, required}], description}`.
`Pack = {id, name, app, icon, description, commands_count, installed, commands: [{id, name, phrases}]}` — `app` —
процесс программы («Spotify.exe»), `name` — для людей.
`CommandService.describe(limit)` (для ИИ и пульта) — `[{id, title, example, phrases, category, icon, slots?}]`.

События: `commands.changed {}`; `record.state {recording, count}`; `packs.changed {}`; от `Jarvis.ShowWindow` —
`ui.show {page?}` (показать окно, открыть страницу) и `ui.hide {}`.

## 3. Голос (`jarvis/voice`, `jarvis/api/voice.py`)

Конвейер: микрофон 16 кГц → Silero VAD → (пока идёт речь) потоковая модель для частичного текста →
по концу фразы GigaAM v3 → слово-активатор (`jarvis.text.find_wake`) → `ctx.dispatcher.handle(text, "voice")`.
Модели — `paths.models_dir()`; скачиваются из релизов `k2-fsa/sherpa-onnx` с проверкой SHA-256 (докачка, атомарная
замена папки). При первом запуске недостающие обязательные модели качаются сами (на платформе `fake` — нет;
`JARVIS_VOICE_AUTODOWNLOAD=1|0` переопределяет).

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/voice` | — | `{state, reason, enabled, error, ptt_error, wake_words, models_ready, input_device}` |
| `POST /api/voice/enabled` | `{enabled}` | то же |
| `POST /api/voice/listen` | — | `{ok}` — слушать команду без активатора (кнопка микрофона, хоткей) |
| `POST /api/voice/stop` | — | `{ok}` |
| `POST /api/voice/capture` | `{timeout?: 15, punct?: true}` | `{text}` — выслушать одну фразу без активатора и без выполнения (диктовка в поле ввода чата) |
| `GET /api/voice/devices` | — | `{inputs: [{id, name, default}], outputs: [...]}` |
| `GET /api/voice/models` | — | `{items: [{id, kind: asr\|stream\|vad\|tts\|whisper, title, description, size_mb, installed, downloading, progress, required, error}]}` |
| `POST /api/voice/models/{id}/download` | — | `{ok}` (прогресс — события) |
| `DELETE /api/voice/models/{id}` | — | `{ok}` |
| `GET /api/tts/voices?engine=` | — | `{items: [{id, name, lang, gender}]}` |
| `POST /api/tts/say` | `{text}` | `{ok}` |
| `POST /api/tts/test` | `{text?, engine?, voice?}` | `{ok}` — прослушать голос |
| `POST /api/tts/stop` | — | `{ok}` |

| Событие | Данные |
|---|---|
| `voice.state` | `{state: off\|loading\|idle\|listening\|processing\|speaking\|error, reason?}` |
| `voice.level` | `{level: 0..1}` — громкость микрофона, ~15 раз в секунду, пока голос включён |
| `voice.wake` | `{}` — услышан активатор |
| `voice.partial` | `{text, wake: bool}` — текст по ходу речи |
| `voice.final` | `{text, wake: bool, command: str}` — фраза целиком и команда без активатора |
| `model.download` | `{id, progress: 0..1, done: bool, error?: str}` |
| `tts.state` | `{speaking: bool, text?}` |
| `tts.level` | `{level: 0..1}` — громкость речи Jarvis (~20 Гц), для пульсации аватара |
| `tts.error` | `{error, engine}` — выбранный голос не сработал, Jarvis говорит запасным (piper или Windows) |

## 4. ИИ, агент, память, заметки (`jarvis/ai`, `jarvis/notes`, `jarvis/mcp`)

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/ai/providers` | — | `{items: [{id, title, base_url, needs_key, local, key_url, default_model, models}]}` |
| `GET /api/ai/models` | — | `{items: [string]}` (с провайдера; ошибка — пресеты) |
| `POST /api/ai/test` | — | `{ok, error?, latency_ms}` |
| `GET /api/ai/usage` | — | `{month: {requests, prompt_tokens, completion_tokens, cost, since}, budget}` |
| `GET /api/chats` | — | `{items: Chat[]}` |
| `POST /api/chats` | `{title?, mode?}` | `Chat` |
| `GET /api/chats/{id}` | — | `{chat: Chat, messages: UiMessage[]}` |
| `PATCH /api/chats/{id}` | `{title?, pinned?, mode?, model?}` | `Chat` |
| `DELETE /api/chats/{id}` | — | `{ok}` |
| `POST /api/chats/{id}/messages` | `{text, images?: [dataURL], mode?}` | `{chat_id, message_id, reply_id}` — ответ идёт событиями (`chat.delta` с `message_id = reply_id`) |
| `POST /api/chats/{id}/stop` | — | `{ok}` |
| `POST /api/agent/run` | `{task}` | `{run_id}` — задача агенту в новом чате; из открытого чата агент запускается сообщением с `mode: "agent"` |
| `POST /api/agent/stop` | — | `{ok}` |
| `GET /api/agent` | — | состояние текущего запуска (как `agent.state`) или `null` |
| `GET/POST /api/memory`, `DELETE /api/memory/{id}` | `{text}` | `{items}` / item |
| `GET/POST /api/notes`, `PATCH/DELETE /api/notes/{id}` | `{title, body, pinned, color}` | `{items}` / item |
| `GET/POST /api/tasks`, `PATCH/DELETE /api/tasks/{id}` | `{title, done, due}` | `{items}` / item |
| `GET /api/events?start=&end=`, `POST`, `PATCH/DELETE /api/events/{id}` | `{title, start, end, all_day, notes, remind_min}` | |
| `GET/POST /api/reminders`, `PATCH/DELETE /api/reminders/{id}` | `{text, at, repeat}` | |
| `GET /api/mcp/servers` | — | `{items: McpServer[]}` |
| `POST /api/mcp/servers`, `PUT/DELETE /api/mcp/servers/{id}` | `McpServerIn` | `McpServer` |
| `POST /api/mcp/servers/{id}/reconnect` | — | `McpServer` |
| `GET /api/mcp/import` | — | `{items: [{key, client, client_label, file, name, transport, command, args, env, url, headers, problem, added}]}` — серверы из Claude Code, Claude Desktop, Cursor, VS Code, Codex, Gemini CLI |
| `POST /api/mcp/import` | `{keys: string[]}` | `{items: McpServer[], skipped: [{key, reason}]}` |
| `GET /api/mcp/catalog?q=&cursor=` | — | `{items: [{name, title, description, version, repository, website, install, installs}], next_cursor, total}` (`install` может содержать `headers`) |

`Chat = {id, title, kind: chat|voice, mode: chat|all|agent, model, pinned, created, updated}`.
`UiMessage = {id, chat_id, role: user|assistant, content, created, meta: {sources?: [{title, url}],
actions?: [{title, ok, detail?}], memory?: [string], images?: [string], error?, usage?, stopped?,
agent?: {run_id, status, steps, plan}}}` — `stopped` — ответ оборван «Стопом», `agent` — итог запуска агента.
`McpServer = {id, name, transport: stdio|http, command, args, env, url, headers, enabled, status: connected|connecting|
error|disabled, error, tools: [{name, description}]}` — секретные `env`/`headers` приходят замаскированными; маска,
вернувшаяся при сохранении, оставляет прежнее значение.

| Событие | Данные |
|---|---|
| `chat.created` / `chat.updated` | `{chat}` |
| `chat.deleted` | `{id}` |
| `chat.message` | `{chat_id, message: UiMessage}` — сообщение человека и итоговое ассистента |
| `chat.delta` | `{chat_id, message_id, delta}` — кусок текста ответа |
| `chat.status` | `{chat_id, message_id, status: thinking\|searching\|tool\|done\|error, text}` — «Ищу актуальную информацию…» |
| `chat.tool` | `{chat_id, message_id, call_id, name, title, status: running\|ok\|error, detail?}` |
| `chat.done` | `{chat_id, message_id, text, sources, actions, usage, stopped?}` |
| `chat.error` | `{chat_id, message_id?, error}` |
| `memory.added` | `{item: {id, text, source, created}}` — «Запомнил: Аня не ест мясо» (при замене факта — тот же id) |
| `memory.deleted` | `{id}` |
| `notes.changed` / `tasks.changed` / `events.changed` / `reminders.changed` | `{op: add\|update\|delete, item?, id?}` |
| `reminder.fired` | `{item: {…, kind: reminder\|event, missed}}` |
| `agent.state` | `{run_id, task, status: planning\|running\|waiting\|done\|stopped\|error, step, total, text}` |
| `agent.step` | `{run_id, index, title, status: running\|ok\|error}` |
| `mcp.status` | `{server_id, status, error?, tools_count}` |
| `ai.configured` | `{configured}` — изменилось «ИИ настроен» (ключ, провайдер, адрес, вход в ChatGPT) |

Подтверждения агента — общий механизм `ctx.confirmations.request(..., kind="agent")` (события `confirm.*`).
Фраза «агент, …» отправляет задачу агенту в любом режиме чата; «стоп» останавливает и ответ ИИ, и агента.

## 5. Виджеты, столы, плагины, пульт (`jarvis/widgets`, `jarvis/desks`, `jarvis/plugins`, `jarvis/remote`)

| Маршрут | Ответ |
|---|---|
| `GET /api/widgets/{kind}?…` | данные виджета: `weather` (`city`), `rates`, `crypto`, `stocks`, `news`, `system`, `media`, `volume`, `recent`, `apps`, `history`, `smarthome`; при сбое сети — прежние данные с `stale: true` или `{error}` |
| `POST /api/widgets/{kind}/{action}` | действие виджета: `apps/launch {app_id\|name}`, `recent/open {path}`, `quick/run {command_id\|text\|url}`, `smarthome/<действие> {…}` |
| `POST /api/media/{action}` | `play_pause\|next\|prev\|stop\|volume_up\|volume_down\|mute\|unmute\|volume {level}` → `{ok, action, volume?, muted?}` |
| `GET /api/desks` | `{items: Desk[], current}` |
| `GET /api/desks/{id}` | `Desk` |
| `GET /api/desks/widgets` | `{items: [{type, title, w, h, min, max}]}` — каталог виджетов |
| `POST /api/desks/{id}/widgets`, `PATCH/DELETE /api/desks/{id}/widgets/{widget_id}` | добавить / изменить / убрать виджет (сервер раздвигает соседей) |
| `PUT /api/desks` | `{items: Desk[]}` — сохранить порядок и раскладку |
| `POST /api/desks` | `{name, preset}` → `Desk` |
| `PATCH /api/desks/{id}`, `DELETE /api/desks/{id}` | `Desk` / `{ok}` |
| `POST /api/desks/{id}/open` | `{ok}` |
| `GET /api/desks/presets` | `{items: [{id, name, description, icon, widgets}]}` (12 заготовок) |
| `GET /api/plugins` | `{items: Plugin[]}` |
| `PUT /api/plugins/{id}` | `{enabled?, config?}` → `Plugin` |
| `POST /api/plugins/{id}/action` | `{action, args}` → `{ok, …}` |
| `GET /api/remote` | `{lan: {enabled, port, urls, qr_svg, status, error}, telegram: {enabled, status, bot, owner_id, pairing_code, pairing_expires, pairing_link, token_set, error}}` |
| `POST /api/remote/telegram/pair` | `{pairing_code, expires, bot, link}` — код живёт 10 минут |
| `POST /api/remote/token` | `{ok}` — новый токен страницы пульта (старые ссылки перестают работать) |

`Desk = {id, name, icon, preset, position, widgets: [{id, type, x, y, w, h, config}]}` (значки столов: `home, desk, sunrise, briefcase, book, clock, chart, music, gamepad, monitor, paw, avatar`) — сетка 4 колонки, высота строки
задаёт интерфейс. Типы виджетов: `avatar, history, control, weather, media, tasks, notes, calendar, clock,
timer, stopwatch, rates, crypto, stocks, news, system, apps, quick, recent, volume, smarthome, pet, ai, worldclock,
countdown, network, battery, sticky, plugins, desk_links`.

`Plugin = {id, name, description, icon, color: [от, до], enabled, config, schema, status: {running, error?, …},
actions: [{id, title}], examples: [фраза]}`. Плагины: `smart_home, dictation, subtitles, summary, reader, autolock,
second_pc, notify_voice, pet`.

Поле схемы: `{key, kind: text|number|bool|select|secret|hotkey|device|list, label, hint?, placeholder?, default?,
options?: [{value, label}], min?, max?, step?, device?: input|output (список — из /api/voice/devices),
fields?: [поля элемента списка] (kind list), secret?: имя, set?: bool (kind secret — задан ли)}`. В
`PUT /api/plugins/{id}` секрет: строка — сохранить, `""` — не трогать, `null` — удалить.

| Плагин | Действия (`POST …/action`) | События `plugin.<id>.*` |
|---|---|---|
| `smart_home` | `states`, `toggle`, `turn_on`, `turn_off`, `set`, `refresh` | `devices {items}` |
| `dictation` | `toggle`, `start`, `stop` | `state {state: listening\|processing\|idle, text}` |
| `subtitles` | `start`, `stop` | `text {id, text, translation, lang, pending}`, `state` |
| `summary` | `start`, `stop`, `retry` | `state {state: recording\|transcribing\|summarizing\|done\|error, seconds}` |
| `reader` | `read {text?\|url?}`, `pause`, `resume`, `stop`, `speed {speed}`, `seek {index}` | `progress {state, index, total, sentence, sentences?}` |
| `autolock` | `pause`, `resume`, `check` | `state {present, last_seen, armed}` |
| `second_pc` | `ping`, `dispatch {pc, text}` | `state` |
| `notify_voice` | `test`, `request_access` | `state` |
| `pet` | `feed`, `pet`, `eat {path}` | `state {playing, recycle_count, windows, fullscreen}`, `action {action}` |

Голосовые команды плагинов — динамические, id `plugin.<id>:<ключ>`; хоткей из `config.hotkey` регистрирует Electron
и вызывает действие `toggle`.

| Событие | Данные |
|---|---|
| `desks.changed` | `{items}` |
| `desk.open` | `{id, name}` — интерфейс показывает стол (голосом: «открой стол Игры») |
| `media.changed` | `MediaInfo` (`jarvis/platform/base.py`) |
| `plugins.changed` | `{items}` |
| `plugin.<id>.<имя>` | события плагинов: `plugin.subtitles.text {text, translation}`, `plugin.dictation.state {state, text}`, `plugin.reader.progress {state, index, total, sentence}`, `plugin.summary.state {state, seconds}`, `plugin.pet.action {action}` … |
| `fullscreen.changed` | `{fullscreen: bool}` — прятать оверлей и аватар над играми |
| `remote.changed` | то же, что `GET /api/remote` |
