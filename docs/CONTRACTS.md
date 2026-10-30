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
  `virtual_screen()` — все мониторы (для снимков), `monitors()` — прямоугольник каждого монитора;
  `foreground_window()` и действия без цели не трогают окна Jarvis;
  `close_app`/`kill_process` возвращают 0, если ничего не нашлось; ввод от Jarvis помечен `dwExtraInfo = 0x4A525653`
  (запись действий и «держи и говори» его пропускают); свои процессы — дерево от `JARVIS_ROOT_PID` (= --parent-pid).
  `microphone_users()` — программы, которые прямо сейчас пишут микрофон (звонок), кроме Jarvis: реестр
  «Конфиденциальность → Микрофон» (`CapabilityAccessManager\ConsentStore\microphone`, `LastUsedTimeStop = 0`), без
  прав администратора; не прочиталось — `[]`. `system_stats().extra`: `disk_free_gb`, `disk_total_gb`, `cpu_count` и
  `gpu_temp_c` — температура видеокарты NVIDIA из `nvidia-smi` (идёт с драйвером, без прав администратора; замер в
  фоне раз в 30 с; нет NVIDIA — ключа нет). Температуру процессора Windows без драйвера честно не отдаёт (WMI
  `MSAcpi_ThermalZoneTemperature` — права администратора и датчик ACPI платы, часто постоянные 27,8 °C) — её нет.
- События — `ctx.bus.publish(type, data)` (из любого потока); данные — JSON-сериализуемые.
- Ошибка для человека — `raise UserFacingError("…")` (API отдаст 400 с этим текстом).
- База — `ctx.db` (таблицы в `jarvis/db.py`; новые таблицы не заводить — для своего состояния есть `kv`
  с ключами `<область>:<имя>` и JSON-файлы в `paths.sub_dir("<область>")`).
- Сеть — `httpx.AsyncClient` (прокси для ИИ — `settings.ai.proxy`; системный прокси Windows от VPN httpx берёт
  через `jarvis.netproxy.system_proxies()` — негодный пропускается, а не роняет клиента). В тестах сети нет:
  `httpx.MockTransport`.
- Тесты — `core/tests/<область>/`, фикстуры из `tests/conftest.py` (`ctx`, `running_ctx`, `client`,
  `events`). Тесты проходят на Linux без микрофона, моделей и сети.
- Новая зависимость: `uv pip install <пакет>` в `.venv` и строка в отчёте (pyproject правит интегратор).

## 1. Ядро (общие маршруты)

| Маршрут | Тело / параметры | Ответ |
|---|---|---|
| `GET /api/health` | — | `{ok, version}` (без токена) |
| `GET /api/status` | — | `{version, platform, data_dir, uptime_s, voice: {state}, ai: {configured}, ready}` |
| `POST /api/stop` | — | `{ok}` — «Стоп»: замолчать, бросить ответ ИИ и агента, отклонить открытые подтверждения, остановить идущие сценарии, перестать слушать (как «Джарвис, стоп») |
| `POST /api/shutdown` | — | `{ok}` — штатный выход ядра (сервисы закрывают базу и файлы); 409 — запущено не сервером |
| `GET /api/settings` | — | `{settings: Settings, secrets: {name: bool}}` |
| `PATCH /api/settings` | частичный Settings | `{settings}` (422 — неверное значение) |
| `PUT /api/secrets` | `{name, value}` (`""` — удалить) | `{secrets}` |
| `POST /api/dispatch` | `{text, source?: "text"\|"ui"\|"voice"\|"remote", speak?: bool}` | итог (см. ниже) |
| `GET /api/history?limit=50` | — | `{items: HistoryItem[]}` (новые первыми) |
| `DELETE /api/history` | — | `{ok}` |
| `GET /api/confirm` | — | `{items: Confirmation[]}` |
| `POST /api/confirm/{id}` | `{allow: bool}` | `{ok}` |
| `GET /api/diagnostics` | — | `{items: [{id, title, status: ok\|warn\|fail\|off, detail, hint, action}], report}` — «Система» → «Диагностика» (`jarvis/diagnostics.py`): ядро и свободное место, Jarvis AI и запасной ИИ, распознавание, голос и словарь ударений, заметки и браузер фоновых задач, пульт в Telegram, аварийные остановки, журналы; `report` — то же текстом без ключей и личной папки (`~`) |
| `POST /api/diagnostics/crashes/reset` | — | то же, что `GET /api/diagnostics` — забыть аварийные остановки (`crashguard.reset()`; пункт с `action: "reset"`) |

Settings — модель `jarvis/settings.py` (в интерфейсе — `desktop/src/api/types.ts`). Секреты:
`ai_api_key, openai_api_key, fish_api_key, elevenlabs_api_key, telegram_token, ha_token, yandex_iot_token,
remote_token`.

Итог `/api/dispatch`: `{kind: "command", result: ExecResult, history}` | `{kind: "ai", chat_id, text, actions,
sources, error, history}` | `{kind: "unknown", history}` | `{kind: "stop"}` | `{kind: "empty"}`.

Готовая команда уступает агенту (если ИИ настроен, режим не «Чат» и включено «Сразу запускать агента»): когда
она не справилась (`ExecResult.data.agent_task` — исключение `NeedsAgent`: «включи музыку», а трек не выбран и
музыка не заиграла; остаток цепочки — в той же задаче, «Потом: …») — тост «… — передаю агенту» и сразу агент с этой
задачей (`ai.ask(text, agent_task=…)`, итог — `{kind: "ai", …}`); и когда человек сам научил, как это делать: в
памяти правило «когда я прошу включить музыку, надо…» и его начало («включить музыку») — та же команда, — агент
получает фразу вместе с правилом, команда не выполняется; и «включи / поставь на ютубе …», «включи видео …»
(команда поиска на YouTube или RuTube с глаголом «включи», «поставь», «запусти») — поиск только покажет
результаты, а ролик включит агент: «… Найди на YouTube видео по запросу «…» и включи его (не рекламу)»; «найди на
ютубе …» — по-прежнему команда.

Пока агент работает, фраза — ему, а не новый запрос (`Agent.interject`), если это не готовая команда или агент
задал вопрос (`ask_user`; тогда любая фраза — ответ): ответ на вопрос, «да» / «нет», когда он ждёт разрешения (как
кнопка на карточке; «нет, напиши лучше Маше» — ещё и поправка), иначе поправка к задаче («нет, не Ане, а Маше»:
вслух «Понял, учту.»). Итог — `{kind: "ai", chat_id, text: "Ответ агенту" | "Решение для агента" | "Агенту —
поправка к задаче", …}`; «стоп» — как всегда, стоп. Так же — сообщение в чат, где работает агент
(`POST /api/chats/{id}/messages` без картинок, `reply_id` — будущий итог агента; «стоп» там — тоже стоп, не
поправка). Пока работает агент, ждёт ответа «Разрешить?» или Jarvis говорит (`Host.stoppable`), «стоп» слышен и
без «Джарвис» и важнее ожидания «да» / «нет» и ответа агенту: «стоп», «стоп-стоп», «стой, хватит» — сразу
`dispatcher.stop()`; пока звучит речь Jarvis (эхо) — только фраза целиком из стоп-слов.

`HistoryItem = {id, ts, source, text, kind: command|ai|unknown|system|confirm, title, status: ok|error|pending|cancelled|
denied, detail, icon, command_id}` — `confirm`: итог подтверждения (`title` — вопрос, `status`: `ok` разрешено, `denied`
отклонено, `cancelled` нет ответа или отменено «Стопом»; `detail` — «Разрешено · Будет отправлено: …», секреты
замаскированы).

Самопроверка сборки — `python -X utf8 -m jarvis --selftest [--strict] [--json]` (`jarvis/selftest.py`): без сети и
настоящих данных (временный каталог, платформа `fake`) проверяет версию Python, импорт всех модулей `jarvis.*`,
библиотеки с нативными частями (на Windows — ещё pywin32, comtypes, pycaw, uiautomation, pynput, soundcard, winrt),
sherpa-onnx нужной версии и PortAudio, паки команд и словарь ударений (SHA-256 по `ru_dict.json`, рядом
`LICENSE.md`; `--strict` — без словаря ✗), подъём ядра со всеми сервисами и ответы API с токеном и без него. Код
выхода 0 — всё ✓; `scripts/build-win.ps1` запускает её встроенным Python после сборки ядра и без ✓ установщик не
собирает.

`ExecResult = {ok, title, message, icon, say, command_ids, data}`; `data.silent` — не показывать тост,
`data.cancelled` — отменено человеком.

`Confirmation = {id, title, detail, kind: command|agent|plugin, source, created, timeout, risk: normal|high}`.
`risk: high` — необратимое (оплата, перевод, покупка, отправка, удаление, публикация, ввод пароля или данных карты,
форматирование, выключение; имя сайта, похожее на подделку; `jarvis.confirm.high_risk` по заголовку и подробности):
голосовое «да» такое не разрешает — только кнопка (Jarvis так и говорит); «нет» голосом работает всегда.
`settings.voice.confirm_high_by_voice: true` — разрешать голосом и необратимое.

### События ядра

| Событие | Данные |
|---|---|
| `hello` | первое сообщение WS: `{version, platform, settings, secrets, voice: {state}, ai: {configured}}` |
| `core.ready` | `{}` |
| `settings.changed` | `{settings}` |
| `secrets.changed` | `{name, set}` |
| `dispatch.started` / `dispatch.done` | `{text, source}` / `{text, kind, ok?}` |
| `history.added` / `history.updated` | `{item: HistoryItem}` (`ms` — сколько длилось от фразы до итога; 0 — сразу) |
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

`Launch.File, Launch.App, Launch.Url, Launch.Folder, Launch.Shell, App.Close, App.Kill, App.Focus, Ui.Click, Ui.Type, Keys.Press,
Keys.Type, Keys.Paste, Mouse.ClickLeft, Mouse.ClickRight, Mouse.DoubleClick, Mouse.Move, Mouse.MoveInWindow, Mouse.Scroll,
System.VolumeSet, System.VolumeUp, System.VolumeDown, System.Mute, System.Unmute, System.MuteToggle,
System.BrightnessSet, System.Lock, System.Sleep, System.Shutdown, System.Restart, System.CancelShutdown,
System.Screenshot, System.EmptyRecycleBin, Media.PlayPause, Media.Next, Media.Prev, Media.Stop, Media.Close,
Window.MinimizeAll, Window.Minimize, Window.Maximize, Window.Restore, Window.Close, Wait, Speak, Notify,
Clipboard.Set, Web.Search, Info.Time, Info.Date, Info.Weather, Jarvis.OpenDesk, Jarvis.Ask, Jarvis.Command,
Jarvis.Plugin, Jarvis.SilentMode, Jarvis.Microphone, Jarvis.ShowWindow` и для встроенных команд `Info.Volume, Info.Media,
Info.Battery, Info.System, Jarvis.Timer, Jarvis.Reminder, Jarvis.ReminderAt, Jarvis.Reminders, Jarvis.ReminderCancel,
Jarvis.Undo, App.CloseLast`. Каталог с описанием параметров — `GET /api/commands/actions`.

**Вопрос — не просьба.** Фраза со знаком «?» («открыть браузер?», «выключить компьютер?», «можешь закрыть
окно?») команду-действие не запускает — отвечает ИИ; команды-справки (`Info.*`, `Jarvis.Reminders`,
`Jarvis.Briefing`, `Speak`: «какая погода?», «сколько времени?») работают и вопросом (`Commands.match`). Запрос
поиска (`{query}`) не забирает составное дело: «найди отели и забронируй», «найди файл и отправь его» — не поиск в
браузере, а дело ИИ и агенту. Набор фраз с трудными отрицательными примерами — `core/evals/routing.py` (0 ложных
запусков).

**Время словами и «отмени».** Слот `{when}` (`jarvis/commands/when.py`) — время напоминания: «завтра в 9», «в
18:30», «сегодня вечером», «в пятницу в 10 утра», «в эту субботу», «послезавтра в полдень», «31 октября в 15:00», «в
девять тридцать вечера»; подходит, только если слова целиком — время не в прошлом (иначе фраза уходит дальше, к ИИ);
час без «утра/вечера» — ближайший такой на часах («в 7» в 15:00 — 19:00); два слота подряд разделяются, если один из
них `{when}` (время — самое длинное). `Jarvis.ReminderAt` — «когда | текст» («завтра в 9 | Позвонить маме»);
`Jarvis.Reminders` — ближайшие напоминания (до 10 на экране, 3 голосом); `Jarvis.ReminderCancel` — пусто: последнее
поставленное, «все» — все (команда с подтверждением), иначе — то, в тексте которого есть эти слова. «Отмени»
(`Jarvis.Undo`, `jarvis/commands/undo.py`) возвращает последнее обратимое действие Jarvis не старше 5 минут:
громкость (прежний уровень), звук, «сверни все окна» (`window_action("undo_minimize_all")`), свернуть/развернуть окно
(окно — как было), таймер и напоминание (удалить), открытую программу (закрыть); шагов нет — Ctrl+Z в активном окне,
как раньше («контрол зет» — всегда Ctrl+Z). `App.CloseLast` («закрой его») — программа, которую Jarvis открыл не
раньше 5 минут назад; нет такой — ошибка, а не закрытие чего попало.

Значения: `Ui.Click` — название кнопки, ссылки или пункта окна впереди («Отправить»), `Ui.Type` — «поле | текст»
(«Кому | ivan@example.com»; содержимое поля заменится): элемент ищется UI Automation по названию (точное, начало,
вхождение, похожее; доступные — первыми), пока окно догружается — до 8 с; не нашёлся — `NeedsAgent` с задачей
«<название сценария>. (Готовый сценарий не справился: …)» — дело берёт агент. `Mouse.MoveInWindow` — «программа |
x y»: курсор в точку окна программы (exe или часть заголовка) от левого и верхнего края, минус — от правого и
нижнего («Telegram.exe | 150 -40»): окно сдвинули или растянули — точка та же; окно не впереди — выходит вперёд;
нет окна — `NeedsAgent`; дальше — `Mouse.Click*`/`Mouse.Scroll` без координат. Такие шаги записывает агент (§4,
«Запомнить как сценарий»). `Keys.Press` — «ctrl+shift+s», несколько через пробел («ctrl+k ctrl+s»), повтор «down*3»; `Wait`,
`System.Shutdown`/`Restart` — секунды или «5m», «1h»; `System.VolumeSet`/`BrightnessSet` — 0–100 или «+10»/«-10»;
`Mouse.*` — «x y» (пусто — где курсор, `center` — центр экрана); `Media.PlayPause` — «» (переключить: играет —
пауза, нет — включить), `play`, `pause`; включить — с проверкой, что заиграло (до ≈2,4 с по сеансу воспроизведения
Windows): ничего не играет и плеера нет — сначала запускается своя музыкальная программа (или та, что сценарий
только что открыл), не заиграло — `NeedsAgent` (агент откроет программу и нажмёт кнопку сам); `Launch.App`, за
которым в команде или сценарии есть ещё действия, ждёт окно программы (до 15 с, новое окно — ещё 1,5 с на
догрузку, уже открытое — 0,3 с), последнее действие — без ожидания; `Web.Search` — «google: запрос» (`google, yandex, youtube, bing, duckduckgo, wikipedia, images, maps` или
сайт с поиском: `avito, ozon…`); `Launch.Folder` — путь или `downloads, documents, desktop, pictures, music, videos,
screenshots`; `Launch.Shell` — только в своих командах; значение слота со спецсимволами оболочки (`&`, `|`, `<`,
`>`, `^`, `%`, `!`, `$`, `;`, кавычки, скобки, обратная кавычка, перевод строки) не подставляется — команда не
запускается; код выхода ждём до 15 с: не 0 — ошибка с последней строкой stderr, дольше — «Запущено»;
`Jarvis.Command` — id команды или её фраза;
`Jarvis.Plugin` — «плагин:действие» («dictation:toggle»), аргументы JSON после «|»; `Jarvis.SilentMode` и
`Jarvis.Microphone` (микрофон Jarvis: «не слушай меня»; голосом обратно не включить) — `on`, `off`, `toggle`; `Jarvis.ShowWindow` — `show`, `hide` или страница (`settings`, `commands`, `chat`);
`Jarvis.Timer` — «5m»; `Jarvis.Reminder` — «10m | Позвонить маме»; `Jarvis.ReminderAt` — «завтра в 9 | Позвонить маме».
`Jarvis.Briefing` («Сводка дня», для сценариев) — `""` (утренняя сводка), `short`, `tasks`, `today`, `missed`,
`welcome`, `hello` — см. §6.
`Media.Close` («закрой музыку») — закрыть программу, которая играет (сеанс воспроизведения Windows), нет её —
запущенную музыкальную (Spotify, Яндекс Музыка, AIMP, foobar2000, VLC); музыка в браузере — только пауза (вкладки
не закрываем); ничего не запущено — ошибка «Музыкальная программа не запущена». `Info.System` — `""` (процессор и
память) или `full` («как дела», «статус систем»: ещё свободное место на системном диске, заряд батареи, температура
видеокарты, если известна); `data.system = {cpu, ram, disk, disk_free_gb, battery, charging, gpu, gpu_temp}`.

Что известно об итоге (`ExecResult.data`) для ответа голосом: `Launch.App` — `app`, `app_key` (известная программа),
`game: true` (запускается игра: известная игра или ссылка `steam://rungameid/…`, `com.epicgames.launcher://apps/…`;
лаунчер — не игра); `Media.PlayPause` — `media: playing|launched|already_playing|paused|already_paused`
(`launched` — музыкальную программу пришлось запустить); `App.Close`/`Media.Close` — `closed` (название);
`System.Shutdown`/`Restart` — `delay` (секунды); `Jarvis.Timer`/`Reminder` — `seconds`; `Info.Weather` —
`weather: {city, temp, condition, feels}`. Встроенная команда без действий («спасибо») — только ответ голосом:
`data.silent`, без тоста.

Встроенные команды характера (категория «Джарвис»): `builtin.jarvis.thanks` «спасибо», «благодарю», «молодец» —
только ответ; `builtin.jarvis.status` «как дела», «как системы», «статус систем», «доложи обстановку» —
`Info.System full`; `builtin.jarvis.welcome` «я вернулся», «я дома», «я тут», «а вот и я» — `Jarvis.Briefing
welcome`; `builtin.jarvis.hello` «доброе утро», «добрый день», «привет» — `Jarvis.Briefing hello`;
`builtin.jarvis.today` «озвучь задачи на сегодня», «что у меня на сегодня» — `Jarvis.Briefing today`;
`builtin.media.close` «закрой музыку», «закрой плеер» — `Media.Close`. «Покажись», «появись» — `Jarvis.ShowWindow
show`; «скройся», «исчезни» — `hide`. Фраза сценария человека («доброе утро» из шаблона) важнее встроенной.
Выключение с задержкой («выключи компьютер через 10 минут», «… через час») — без «Да или нет?»: его отменяет
«отмени выключение», Windows сама предупреждает; немедленное выключение и перезагрузка — с подтверждением, а
`System.Shutdown`/`Restart` по просьбе ИИ или агента (`source: ai|agent`) — всегда с подтверждением.

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
Итог фразы начинается заранее: второй VAD с короткой паузой (0,18 с) замечает, что речь стихла, — обращённую
к Jarvis фразу ПК распознаёт, пока основной VAD ещё ждёт `end_silence_ms`; речь продолжилась — догадка
выброшена. В журнал — «текст фразы через N мс после конца речи (тишина …, распознавание …)».
Модели — `paths.models_dir()`; скачиваются из релизов `k2-fsa/sherpa-onnx` с проверкой SHA-256 (докачка, атомарная
замена папки). При первом запуске недостающие обязательные модели качаются сами (на платформе `fake` — нет;
`JARVIS_VOICE_AUTODOWNLOAD=1|0` переопределяет).

Настройки голоса, которые меняют конвейер на ходу:
- `voice.language` — язык команд: `ru` (GigaAM), иначе `auto|en|uk|de|fr|es|it|pt|pl|tr|ja|ko|zh` — итог фразы
  распознаёт Whisper (обязательная модель: уже скачанный Whisper или `whisper-base`), потоковой модели нет (она
  русская — частичного текста и раннего «Джарвис» тоже нет). Пока Whisper не скачан — распознаётся по-русски.
  Встроенные команды — русские: фраза на другом языке уходит Jarvis AI.
- `voice.end_silence_ms` (300–2000, по умолчанию 450) — тишина после речи, по которой фраза закончилась;
  стихла на «открой…» или предлоге — ещё 0,5 с ждём продолжения.
- `voice.asr_model` — итог фразы: `gigaam-v3` (по умолчанию), `gigaam-v3-punct`, `vosk-small` — на ПК;
  `elevenlabs` — ElevenLabs Scribe v2 Realtime (`jarvis.voice.scribe`) по ключу `elevenlabs_api_key` (тот же,
  что у голоса; у ключа нужно право Speech to Text). Звук уходит по WebSocket
  `wss://api.elevenlabs.io/v1/speech-to-text/realtime` (`model_id=scribe_v2_realtime`, PCM 16 кГц,
  `commit_strategy=manual`, `language_code` — язык команд) только у обращённой фразы: «Джарвис» услышан
  потоковой моделью, клавиша «держи и говори», просьба выслушать, окно продолжения, выключенный режим
  префикса; с первого звука фразы, а на паузе в речи — commit заранее. VAD, «Джарвис» и чужие разговоры — на ПК;
  GigaAM в этом режиме не грузится и не обязательна. Сервис не ответил за 2 с, нет сети, ключа, кредитов —
  текст Vosk; после серьёзной ошибки облако не зовётся (кредиты — час, ключ, условия, регион — 10 минут,
  сеть и перегрузка — 30 с), тост «Распознавание ElevenLabs» — не чаще раза в минуту; новый ключ или другая
  модель снимают паузу. Прокси — поле «Прокси» Jarvis AI, иначе системный (VPN, SOCKS5 тоже).
- Слово-активатор: своё слово узнаётся и с окончаниями («пятницу»), фраза из нескольких слов — целиком
  («эй, Джарвис»), латиница — на слух («Jarvis» = «джарвис»).

Голос Jarvis (`tts.engine`): `piper` (локально), `windows`, `edge`, `openai`, `fish`, `elevenlabs`. ElevenLabs —
по ключу `elevenlabs_api_key` (бесплатный тариф — около 10 000 кредитов в месяц): `tts.elevenlabs_voice_id`
(пусто — стандартный George), `elevenlabs_model` (`eleven_flash_v2_5` по умолчанию), `elevenlabs_stability`,
`elevenlabs_similarity`, `elevenlabs_style` (0–1). Движок может сказать, сколько его не звать (кончились кредиты —
до их обновления, неверный ключ или отключённый за VPN бесплатный тариф — 10 минут, обычный сбой — минуту); удачное
прослушивание (`POST /api/tts/test`) снимает паузу. Короткие (до 160 символов) фразы облачных движков
(edge, openai, fish, elevenlabs) Jarvis помнит в памяти — повтор не тратит кредиты. На диск (`cache/tts`, до 300
фраз, старые удаляются первыми) — только фразы без личного: частые ответы (`Tts.add_stock`: «Готово, сэр.», «Не понял
команду…», «Понял, учту.», «Сейчас сделаю…» — их звук выбранного облачного голоса готовится заранее, через 3 с после
запуска и после смены голоса, темпа, обращения или ключа) и «Открываю <программа>.» (`Tts.keep`); после перезапуска
они звучат сразу. Остальное — только в памяти: в фразах бывает личное. Синтез при подготовке — не больше 1200
символов за раз (`WARM_MAX_CHARS`; фразы с диска не в счёт): кредиты облачного голоса тратятся понемногу — первые
варианты всех частых ответов успевают сразу, остальные — при следующих запусках (всего ≈2,6 тыс. символов на голос,
у ElevenLabs Flash — ≈1,3 тыс. кредитов один раз). `Tts.is_ready(text)` — прозвучит ли фраза сразу (локальный голос
или звук всех её предложений уже в памяти).

**Ударения и «ё» голоса piper** — словарь Jarvis (`jarvis/voice/stress.py`; сборка и лицензии — `scripts/ru_dict`:
GPL-3.0-or-later и CC BY-SA 4.0, не PolyForm): согласие ударений с OpenRussian 94,2 → 99,6 %, «ё» в 106 тыс. слов,
омографы по знаку ударения. Установщик несёт `jarvis/resources/voice/{ru_dict, ru_dict.json, LICENSE.md}`
(`scripts/build-core-win.sh`, `scripts/build-win.ps1`; без сети — `SKIP_RU_DICT=1` / `-SkipStressDict`). Перед
загрузкой голоса ядро ставит словарь в его `espeak-ng-data` (исходный — рядом, `ru_dict.orig`), если фонемные
таблицы голоса (`phontab`, `phonindex`, `phondata`) — те, с которыми словарь собран (`ru_dict.json.phoneme_tables`);
`settings.tts.stress_dict: false`, другие таблицы или вылет ядра при загрузке голоса с этим словарём (этап
crashguard `ru-dict`) — исходный словарь. espeak-ng читает словарь один раз на процесс: смена — со следующего
запуска. В ответе голосом модель пишет «ё» и знак ударения U+0301 в словах с двояким ударением
(`prompts.SOURCE_VOICE`): на экран, в `chat.delta`/`chat.done` и в чат ответ уходит без знаков (`Ai._delta`), в речь —
со знаками (`AskResult.spoken`); знак получает только piper со словарём Jarvis (`engine.accents`), остальным
голосам — без него (`textprep.prepare(accents=False)`). `Tts.status().stress_dict = {bundled, loaded:
jarvis|espeak|""}`.

Характер Джарвиса (`jarvis/voice/character.py`, `ctx.character`) — с персоной «Джарвис» (`ai.persona: jarvis`):
ответ голосом на встроенную команду в его духе, коротко и с обращением из профиля (`profile.address`; пусто — без
обращения): «Сворачиваю все окна, сэр.», «Я здесь, сэр.», «Приятного просмотра, сэр.», «Библиотека игр будет доступна
через мгновение, сэр.», игра — «Запускаю протокол игры, сэр.», «Таймер установлен, сэр. Сработает через 3 минуты.»,
«Выключаю компьютер, сэр. Отключение через 10 минут.», «Температура за бортом плюс 19 градусов, ясно, сэр.», «как
дела» — «В полном порядке, сэр. Все системы функционируют в штатном режиме. Процессор загружен на 23 процента, память
— на 46.» (что-то не так — нагрузка от 85%, память от 90%, на диске меньше 10 ГБ, заряд до 15% без зарядки, видеокарта
от 85 °C — «Обратите внимание: …»), сведения — с обращением («Сегодня вторник, двадцатое октября, сэр.»). Ключ ответа —
id встроенной команды или что известно об итоге (`ExecResult.data`, §2). Вариантов у ключа 1–5: случайный, но не тот,
что прозвучал в прошлый раз, и — если есть — из тех, что `is_ready`. Все варианты без данных — частые ответы
(`add_stock`, первые варианты — первыми); с названием программы или сайта — `keep`. Своя фраза команды человека,
пака или сценария, ошибки и цепочки команд — как раньше; с «Озвучивать итог команд: не озвучивать» персонаж молчит
там же, где молчал бы без него. Команда думает дольше 0,35 с (погода из сети, «включи музыку» проверяет, заиграла
ли) — сразу «Секунду, сэр.» / «Уточняю, сэр.» (готовый звук), а итог — следом, не перебивая (не в тихом режиме, не
пока ждём «да / нет»). Напоминания — «Сэр, напоминаю: позвонить маме.», таймер — «Сэр, время вышло: таймер на 3
минуты.», пропущенное — «Сэр, пока вас не было, сработало напоминание: …». Персона «Обычный» — всё как раньше.
От фразы до первого звука ответа с частым ответом — 2–6 мс (облачный голос, 0,4 с на предложение: без подготовки —
≈405 мс; `tests/voice/test_reply_speed.py`).

Приветствие по возвращении (`jarvis/voice/presence.py`, `ctx.presence`): `voice.presence_greeting` — `off` (по
умолчанию), `news` (только если есть что сказать: напоминания пропущенные или на ближайшие 3 часа), `always`;
`voice.presence_away_min` (5–240, по умолчанию 20) — столько минут без ввода (`platform.idle_seconds`) или сон
компьютера — «отошёл». Вернулся (ввод за последние 10 с) — тост «С возвращением» и голосом сводка `welcome` (§6).
Никогда не заговорит сам: в тихом режиме, над игрой или видео во весь экран (`is_fullscreen_app`), во время звонка
(`microphone_users` не пуст), пока Jarvis говорит, слушает, ждёт «да / нет» или работает агент, пока идёт «Фокус»
(он сам говорит «С возвращением»), при своём включённом сценарии с триггером `idle_return`. Выключено по умолчанию:
Jarvis заговорит без просьбы и прочтёт напоминания вслух, а рядом могут быть люди; на «я вернулся» он отвечает и так.

Окно продолжения (`Voice.follow_up(seconds=None, *, question=False)`): после ответа Jarvis следующая фраза — без
активатора; подряд — не больше 2 таких команд, ответы на уточняющие вопросы (`question=True`, окно не короче 8 с)
в этот счёт не входят, но всего окон подряд — не больше 4.

«Что-нибудь ещё, сэр?» (`settings.voice.ask_more`, по умолчанию вкл): после удачной команды или ответа ИИ голосом
Jarvis добавляет вопрос и открывает окно продолжения как для вопроса — только если оно правда откроется
(`Voice.can_follow(question=True)`: голос слушает, `follow_up_s` > 0, не играет музыка, лимит окон не исчерпан).
«Нет», «всё», «ничего», «нет, спасибо» в ответ (до 60 с) — «Как скажете, сэр.», и больше не слушает (диспетчер
отвечает `{kind: "end"}`). После «спасибо», «скройся», блокировки, сна, выключения и перезагрузки — не спрашивает.

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/voice` | — | `{state, reason, enabled, error, ptt_error, wake_words, models_ready, input_device, cloud_error}` — `cloud_error`: выбрано распознавание ElevenLabs, но звать его сейчас нельзя (нет ключа, пауза после ошибки); иначе `""` |
| `POST /api/voice/enabled` | `{enabled}` | то же |
| `POST /api/voice/listen` | `{toggle?}` | `{ok}` — слушать команду без активатора (кнопка микрофона, хоткей); `toggle` и Jarvis уже слушает/отвечает или работает агент — «стоп»: `{ok, stopped: true}` |
| `POST /api/voice/stop` | — | `{ok}` |
| `POST /api/voice/capture` | `{timeout?: 15, punct?: true}` | `{text}` — выслушать одну фразу без активатора и без выполнения (диктовка в поле ввода чата) |
| `GET /api/voice/devices` | — | `{inputs: [{id, name, default}], outputs: [...]}` |
| `GET /api/voice/models` | — | `{items: [{id, kind: asr\|stream\|vad\|tts\|whisper, title, description, size_mb, installed, downloading, progress, required, error}]}` |
| `POST /api/voice/models/{id}/download` | — | `{ok}` (прогресс — события) |
| `DELETE /api/voice/models/{id}` | — | `{ok}` |
| `GET /api/tts/voices?engine=` | — | `{items: [{id, name, lang, gender, category?}]}` (у ElevenLabs без ключа — пусто) |
| `GET /api/tts/account?engine=` | — | `{supported: false}` — у движка нет счёта; ElevenLabs: `{supported: true, tier, status, used, limit, reset_at: ISO\|null, models: [{id, name, cost?}], error}` — израсходовано и лимит кредитов, когда обновятся, модели с русским |
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
| `tts.error` | `{error, engine}` — выбранный голос не сработал (нет ключа или сети, кончились кредиты ElevenLabs…), Jarvis говорит запасным (piper или Windows) |
| `presence.greeted` | `{away_min, news}` — поздоровался, когда человек вернулся (`news` — были напоминания) |
| `presence.skipped` | `{reason: silent\|fullscreen\|call\|busy\|focus\|scenario\|nothing}` — человек вернулся, но Jarvis промолчал |

## 4. ИИ, агент, память, заметки (`jarvis/ai`, `jarvis/notes`, `jarvis/mcp`)

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/ai/providers` | — | `{items: [{id, title, base_url, needs_key, local, key_url, default_model, models, login, component, cli, group, country, regions, folder}]}` — `login`: вход в аккаунт вместо ключа, `component`: нужен скачиваемый компонент (у `chatgpt` оба `true`), `cli`: работает через программу человека (`claude_code`, `gemini_cli`, `qwen_code`), `group`: `account\|key\|local\|custom`, `country`: `cn\|ru\|""`, `regions`: `[{id, title, base_url, key_url}]` (первый — по умолчанию; выбранный регион = `settings.ai.base_url`, пусто — первый), `folder`: нужен `settings.ai.folder_id` (YandexGPT) |
| `GET /api/ai/models` | — | `{items: [string]}` (с провайдера; у `chatgpt` — каталог Codex; ошибка — пресеты) |
| `POST /api/ai/test` | — | `{ok, error?, latency_ms, model}` (у `chatgpt` — крошечный ход Codex) |
| `GET /api/ai/usage` | — | `{month: {requests, prompt_tokens, completion_tokens, cost, since}, budget}` |
| `GET /api/ai/fallback` | — | `{configured, provider, model, paused: [{provider, reason, text, seconds}]}` — запасной ИИ и кто сейчас на паузе |
| `GET /api/ai/chatgpt` | — | `ChatGptState` (запускает Codex, если компонент есть) |
| `POST /api/ai/chatgpt/install` | — | `{ok, installing: true}` — скачивание в фоне (события `ai.chatgpt`), или `{ok, installed: true}` |
| `POST /api/ai/chatgpt/login` | `{device?: bool}` | `{ok, logged_in, opened?, auth_url?}` — ядро само открывает браузер (вход OAuth ChatGPT); `device: true` — `{verification_url, user_code}` для входа с другого устройства; итог — событием `ai.chatgpt`; 400 — нет компонента или вход не начался |
| `POST /api/ai/chatgpt/login/cancel` | — | `{ok}` |
| `POST /api/ai/chatgpt/logout` | — | `{ok}` |
| `GET /api/ai/cli/{provider}?refresh=` | — | `CliState` программы-провайдера (`claude_code`, `gemini_cli`, `qwen_code`; иначе 404); `refresh=true` — найти программу заново и перепроверить вход |
| `POST /api/ai/cli/{provider}/install` | — | `{ok, installing: true}` — официальный пакет npm в фоне (события `ai.cli`), или `{ok, installed: true}`; 400 — Claude Code (ставит сам человек официальным установщиком), нет Node.js нужной версии или npm |
| `POST /api/ai/cli/{provider}/login` | — | `{ok, opened, command, hint}` — окно терминала с самой программой: вход человек выполняет в ней (Jarvis не видит паролей и токенов); 400 — не установлена |
| `GET\|POST\|DELETE /api/ai/mcp` | MCP | сервер MCP Jarvis (Streamable HTTP, без сессий, ответы JSON) для программ-провайдеров: кроме `X-Jarvis-Token` нужен `X-Jarvis-Turn` — токен одного хода ИИ (нет или чужой — 403) |
| `GET /api/chats` | — | `{items: Chat[]}` |
| `POST /api/chats` | `{title?, mode?}` | `Chat` |
| `GET /api/chats/{id}` | — | `{chat: Chat, messages: UiMessage[]}` |
| `PATCH /api/chats/{id}` | `{title?, pinned?, mode?, model?}` | `Chat` |
| `DELETE /api/chats/{id}` | — | `{ok}` |
| `POST /api/chats/{id}/messages` | `{text, images?: [dataURL], mode?}` | `{chat_id, message_id, reply_id}` — ответ идёт событиями (`chat.delta` с `message_id = reply_id`); пока в этом чате работает агент, текст — ему (ответ на вопрос, поправка), `reply_id` — его итог |
| `POST /api/chats/{id}/stop` | — | `{ok}` |
| `POST /api/agent/run` | `{task}` | `{run_id}` — задача агенту в новом чате; из открытого чата агент запускается сообщением с `mode: "agent"` |
| `POST /api/agent/stop` | — | `{ok}` |
| `GET /api/agent/journal/{run_id}` | — | журнал прогона агента: `{run_id, task, chat_id, started, finished, status, outcome, summary, error, plan, steps: [{index, ts, title, name, args, ok, result, thought, images}]}`; нет — 404 |
| `GET /api/agent/journal/{run_id}/{name}` | — | `{url}` — картинка шага (`NNN-K.jpg`) data URL'ом; нет — 404 |
| `POST /api/agent/scenario` | `{chat_id?, message_id?}` | `{scenario}` — удачный прогон агента (итог с `meta.recipe`; пусто — последний) → сценарий: фраза — слова человека, шаги — его действия; повторно — тот же сценарий; нечего запомнить — 400 |
| `GET /api/agent` | — | состояние текущего запуска (как `agent.state`) или `null` |
| `GET /api/browser` | — | `BrowserState` — браузер Jarvis для агента (`jarvis/ai/browser`) |
| `POST /api/browser/open` | `{url?}` | `BrowserState` — кнопка «Открыть браузер Jarvis»: включить (`ai.browser`), открыть окно со своим профилем или вывести его вперёд (`url` http/https — новой вкладкой); не вышло — 400 с тем, что сделать |
| `POST /api/browser/disconnect` | — | `BrowserState` — отключиться (окна браузера остаются; агент подключится снова, когда понадобится сайт) |
| `GET /api/ai/tools` | — | `{items: ToolInfo[], groups: [{id, title}], agent_confirm}` — каталог «Инструменты и разрешения» (`jarvis/ai/catalog.py`); разрешение меняется `PATCH /api/settings {ai: {tool_policy: {<policy_key>: allow\|ask\|deny\|auto}}}` (`auto` — снова по риску) |
| `GET/POST /api/memory`, `DELETE /api/memory/{id}` | `{text, kind?}` | `{items}` / `MemoryItem` (временные с истёкшим сроком не приходят) |
| `PATCH /api/memory/{id}` | `{text?, kind?}` | `MemoryItem` — поправленная запись становится записью человека (`source: user`); 404 — нет записи |
| `DELETE /api/memory` | — | `{ok, count}` — забыть всё |
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

`ToolInfo = {policy_key, name, title, description, source: builtin|plugin:<имя>|mcp:<сервер>|agent, group, modes:
[chat|all|agent], available, timeout, risk: read|act|risky, risk_title, default: allow|ask, override:
allow|ask|deny|null, decision: allow|ask|deny}` — `policy_key` — ключ в `settings.ai.tool_policy`: имя инструмента,
у действий агента — `agent.<действие>` (`agent.ui_click`); `risk` и `decision` — `jarvis/ai/policy.py`
(рискованное по умолчанию спрашивается); `available: false` — выключено настройкой (поиск, память).
`MemoryItem = {id, text, source: ai|user, kind: fact|assumption|temp, expires, created}` — `assumption`: предположение (ИИ уточняет, прежде чем на него опираться), `temp`: забывается через сутки (`expires` — unix-время). Запись человека ИИ не перезаписывает: `Notes.memory_add` вернёт её же с `unchanged: true`.
`Chat = {id, title, kind: chat|voice, mode: chat|all|agent, model, pinned, created, updated}`.
`UiMessage = {id, chat_id, role: user|assistant, content, created, meta: {sources?: [{title, url}],
actions?: [{title, ok, detail?}], memory?: [string], images?: [string], error?, usage?, stopped?,
agent?: {run_id, status, steps, plan}, recipe?: {request, actions: [{type, value}], scenario_id?}}}` — `stopped` —
ответ оборван «Стопом», `agent` — итог запуска агента, `recipe` — его удачный прогон, который можно запомнить
сценарием (`scenario_id` — уже запомнен).
`BrowserState = {enabled, status: off|closed|starting|connected|error, app, found, version, error, tabs, profile}` —
`app` — «Google Chrome» / «Microsoft Edge» (выбран или найден), `found: false` — ни того ни другого нет, `error` —
что не так и что сделать, по-русски, `profile` — папка профиля браузера Jarvis.
`ChatGptState = {installed, installing, logged_in, account, error, version, login_pending?, auth_url?,
verification_url?, user_code?}` — `account`: «email (ChatGPT Plus)»; `error` — почему компонент не скачался или Codex
не запустился (текст для человека); `login_pending` и ссылки — пока ждём входа в браузере.
`CliState = {id, name, installed, path, version, logged_in: true|false|null, account, installing, install_error,
install_log, can_install, npm, node, node_required, install_url, install_command, login_command, login_hint, note,
images, agent, tools, error}` — `logged_in: null` — неизвестно (проверьте подключение); `npm: ""` — кнопки «Установить»
нет (Claude Code); `node` — версия Node.js на ПК; `note` — важное о тарифах и входе; `error` — почему последний ход
не прошёл по входу (текст для человека).
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
| `chat.image` | `{chat_id, message_id, image, info}` — картинка, нарисованная в ответе (`generate_image`), до конца ответа |
| `memory.added` | `{item: MemoryItem}` — «Запомнил: Аня не ест мясо» (при замене факта — тот же id) |
| `memory.updated` | `{item: MemoryItem}` — человек поправил запись |
| `memory.deleted` | `{id}` |
| `memory.cleared` | `{count}` — память очищена целиком |
| `notes.changed` / `tasks.changed` / `events.changed` / `reminders.changed` | `{op: add\|update\|delete, item?, id?}` |
| `reminder.fired` | `{item: {…, kind: reminder\|event, missed}}` |
| `agent.state` | `{run_id, task, status: planning\|running\|waiting\|done\|stopped\|error, step, total, text, plan: string[], outcome: ""\|done\|partial\|failed, chat_id}` — `outcome` — как оценил итог сам агент (finish): «Готово» в интерфейсе только при `done`; `waiting` — ждёт разрешения или ответа на вопрос (`text`: «Жду ответа: …»); `chat_id` — чат, куда агент пишет итог (сообщение туда, пока он работает, — ему) |
| `agent.step` | `{run_id, index, title, status: running\|ok\|error}` |
| `browser.state` | `BrowserState` — браузер Jarvis подключился, закрылся, запускается, ошибка, выключен |
| `agent.pointer` | `{run_id, kind: click\|double\|right\|type\|keys\|scroll\|focus, x, y, label, rect?: [l,t,r,b], ts}` — куда агент жмёт и что вводит, в физических точках экрана (оверлей рисует «курсор агента»; основной монитор: точка оверлея = физическая / масштаб − начало рабочей области) |
| `mcp.status` | `{server_id, status, error?, tools_count}` |
| `ai.configured` | `{configured}` — изменилось «ИИ настроен» (ключ, провайдер, адрес, каталог, вход в ChatGPT или программу) |
| `ai.fallback` | `{active: true, provider, backup, reason, text, seconds}` — основной не ответил, на паузе, отвечает запасной (и тост «Отвечает запасной ИИ — …»); `{active: false}` — паузы сняты (новые настройки или ключ) |
| `ai.cli` | `{provider, state: installing\|log\|installed\|error\|login\|auth, line?, version?, error?, opened?, logged_in?}` — установка программы-провайдера, окно терминала для входа, смена состояния входа |
| `ai.chatgpt` | `{state: installing\|installed\|login\|device\|logged_in\|logged_out\|error, error?, version?, account?, auth_url?, verification_url?, user_code?, opened?}` — компонент и вход ChatGPT |

Картинки (`jarvis/ai/images.py`, `jarvis/api/images.py`): рисует OpenAI Images — модель `gpt-image-1`
(`JARVIS_IMAGE_MODEL` меняет), ответ JPEG; ключ — `openai_api_key`, а если провайдер ИИ — OpenAI, то и
`ai_api_key` (адрес — его `base_url`, иначе `https://api.openai.com/v1`; прокси — `settings.ai.proxy`). Нет ключа —
400 с текстом, где его задать (подписка ChatGPT картинки через API не рисует). Расход — строка в `usage` (токены из
ответа; цена — по токенам gpt-image-1), месячный лимит `ai.monthly_budget` проверяется до запроса.

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/images/status` | — | `{available, model, problem}` — есть ли ключ, чтобы рисовать |
| `POST /api/images/generate` | `{prompt, format?: square\|landscape\|portrait, quality?: low\|medium\|high, references?: [dataURL] (до 4)}` | `{image: dataURL, info: {prompt, size, model, quality}, cost}`; с `references` — `POST /images/edits` (своё фото как образец, `input_fidelity: high`); ошибка OpenAI — 400 с понятным текстом (ключ, регион, проверка безопасности, подтверждение организации, лимит) |

Инструмент чата `generate_image {prompt, format?, quality?, use_reference?}` (режимы «Чат» и «Всё вместе», риск
`act`, около 30 с): `use_reference` — образец из последнего сообщения человека с картинками. Готовая картинка сразу
уходит событием `chat.image {chat_id, message_id, image, info: {prompt, size, model, quality}}` (`message_id` — id
будущего ответа, из `chat.tool` этого вызова), а итог ответа получает `meta.images` (data URL) и `meta.generated`
(`[{prompt, size, model, quality}]`, по записи на картинку) — ядро дописывает их после сохранения ответа и шлёт
`chat.message` с этим id ещё раз (следом за `chat.done`). Удачная карточка «Нарисовать: …» интерфейсом не
показывается — видна сама картинка со «Скачать». Внимание: в режиме «Всё вместе» просьба «нарисуй …» сейчас
сразу уходит агенту (`apphints.app_task`: «нарисуй» — глагол действия на ПК), «сгенерируй картинку …» — чату.

Подтверждения агента — общий механизм `ctx.confirmations.request(..., kind="agent")` (события `confirm.*`).
Фраза «агент, …» отправляет задачу агенту в любом режиме чата; «стоп» останавливает и ответ ИИ, и агента.
Агент из чата (инструмент `run_agent`) пишет итог в тот же чат. В режиме «Всё вместе» чат сам делает только
лёгкое — одну простую команду (окно, громкость, медиа, открыть или закрыть программу, свет), вопросы и сведения,
память, заметки, задачи и напоминания, картинки («нарисуй кота»; «нарисуй в пейнте…» — агенту); остальное дело на компьютере («напиши Ане, что…», «поставь будильник…»,
«создай в ворде…», несколько действий подряд) и вопрос о том, что на экране, в окне, в почте или мессенджере,
сразу уходит агенту, без отдельного ответа модели (`settings.ai.auto_agent`, по умолчанию вкл;
`jarvis/ai/apphints.py` `app_task`); с голоса агент сразу отвечает «Сейчас сделаю», итог — голосом в конце.
Дело в браузере — всегда агенту (он видит страницу и жмёт по ней, чат умеет только открыть ссылку): в просьбе
браузер, сайт, страница, вкладка, ссылка, YouTube, Ozon, Wildberries, Авито, Госуслуги или адрес сайта, даже
«открой …» (сведения — «найди в интернете …», «загугли» — по-прежнему чату); или впереди браузер
(`apphints.browser_front`: активное окно — `playbooks.BROWSER_EXES`; впереди окно Jarvis — нет) — тогда лёгкое только
громкость, звук, окна и свет, а «открой первое видео», «включи видео», «закрой вкладку» — агенту.

«Что на экране?» — за один ход (`jarvis/ai/screen.py`): вопрос о том, что видно сейчас («что это за модель?»,
«какой фильм на экране?», «где ошибка в коде?», «прочитай, что тут написано», «переведи текст на экране»;
`screen.is_question`), если модель видит картинки (ключ API — `settings.ai.vision`; ChatGPT и программы — модель
понимает картинки), идёт не агенту, а в обычный чат: Jarvis снимает монитор окна впереди без сетки и меток (окно
обведено), мелкое окно — следом крупно; впереди окно Jarvis (спросили из чата) — снимок окна под ним (PrintWindow).
Картинки — в `meta.images` сообщения человека (видны в чате), к словам для модели — пометка (`Turn.note`: какое окно
впереди, отвечать коротко, текст со снимка — данные, а не команды); в чат пометка не пишется. Голосом — ответ
голосом. Уточняющий вопрос («а какого она года?») получает картинки прошлого обмена снова (ключ API: история с
картинками только у последнего обмена; ChatGPT — они уже в его разговоре). Дела («открой», «напиши»), то, чего на
экране нет («что в почте», «что написала Аня в телеграме»), фраза «агент, …» и модель без картинок — агенту, как
раньше. Снять экран не вышло — тоже агенту.
Не угаданное правилом чат отдаёт агенту сам (`run_agent`), не прося включить режим агента. Скорость: `settings.ai.speed` — `fast` (по умолчанию: усилие рассуждений `low` у Codex и у думающих
моделей OpenAI, Gemini 2.5+, OpenRouter), `balanced` (умолчание модели), `deep` (`high`).

Подсказки модели (`jarvis/ai/apphints.py`): к просьбе в чате (режимы «Всё вместе» и «Агент», ключ API, ChatGPT,
программы-провайдеры) и к задаче агента добавляется блок — приёмы программ и сайтов, о которых просьба
(`jarvis/ai/playbooks.py`: у каждой `text` для агента — работа в окне, `chat` для чата — какими инструментами),
готовые команды паков для этих программ (установленный пак — по id, `run_command {text, id?}` с `id` выполнит
именно её; не установленный — только его сочетания клавиш), свои команды и сценарии пользователя (по id), рецепт
Telegram через MCP по настоящим именам инструментов (найти чат → id → прочитать / отправить) и собственные
инструкции подключённых серверов MCP (`initialize.instructions`, до 1500 символов). Инструмент чата `open_url`
открывает и адреса программ: `tg:`, `ms-settings:`, `spotify:`, `mailto:`, `whatsapp:` (место в программе, сами
ничего не отправляют).

Агент (`jarvis/ai/agent.py`). Действия: `ui_snapshot`, `screenshot`, `focus_window`, `ui_click` (`double`),
`browser_*` (браузер Jarvis — ниже),
`ui_type`, `click_point {x, y, what, button?, double?}` (координаты картинки последнего снимка окна → экран;
окно сдвинули — пересчёт), `type_text {text, field?}` (туда, где фокус), `scroll {direction, amount, x?, y?}`,
`press_keys`, `launch_app`, `open_url` (`https:`, `tg:`, `ms-settings:`, `spotify:`, `mailto:`, `whatsapp:`),
`run_command {text, id?}`,
`wait`, `zoom {x, y, width, height}` (лупа: кусок последней картинки экрана в полном разрешении, мелкий — крупнее
до 3 раз; потом `click_point` — по картинке лупы), `ask_user {question}`, `finish {status: done|partial|failed,
summary, attention?, lesson?}` и инструменты подключённых
серверов MCP (имя `mcp__…`, ответы помечены как данные). Видит окно впереди (никогда не окно Jarvis): элементы
UI Automation (окно браузера — одним запросом `FindAllBuildCache` со свойствами в кэше, только видимые: сначала
кнопки самого браузера, до 30, потом «Страница «заголовок»» — документ и его ссылки, поля, кнопки — и «Текст
страницы на экране» до 2500 знаков, без подписей ссылок и кнопок; страница пустая — Chrome строит её дерево, когда
спросят: `AccessibleObjectFromWindow` окну страницы и ещё снимок через 0,4 с; запрос не удался — обход дерева, как
у других окон; `jarvis/ai/uia.py` `split_page`) и, если модель понимает картинки (`settings.ai.vision`: `auto` — по провайдеру и модели, `on`,
`off`), после каждого действия (и сорвавшегося — `WindowChanged`, ошибка платформы, тайм-аут; и после ответа
на `ask_user`) — картинку экрана (до 1568 точек, JPEG 85): монитор окна впереди (`platform.monitor_rect`) — окно
обведено, его элементы с метками `e…`, сетка через 100 точек (`jarvis/ai/vision.py` `render_pixels`); если на ней окно
впереди мельче родного хотя бы на четверть — следом оно крупно, в родном разрешении (до 1568 точек), с метками и сеткой
(`render_details`, `click_point`/`zoom` с `on: "window"` — по его координатам), а окно во весь большой монитор —
крупно самое большое изменение после действия (новое сообщение, окно, ошибка; только посмотреть); и другие мониторы
(`platform.monitors()`, до 3) следом, мельче (до 960 точек), без сетки и меток — для обзора (`render_other`; одним
сообщением с главной картинкой). Пикселями без PNG (`platform.screen_pixels` — в пуле ввода-вывода, мониторы
параллельно, пока поток платформы читает элементы окна; платформа без них — PNG всех мониторов, обрезанный до
нужного). Окно задачи меняют только действия, которые для того и нужны: `launch_app`, `open_url`, `run_command`,
`focus_window`, `ui_snapshot`, клавиши Win+…/Alt+Tab (после `launch_app` агент ждёт окно программы до 15 с и выводит
его вперёд, нет окна — модели «окна пока нет: подожди»; после `open_url` — до 5 с, пока впереди не сменится окно); после нажатия, ввода, ожидания, сорвавшегося действия и ответа
на вопрос всплывшее окно другой программы окном задачи не становится. В нём можно действовать по тому, что видно в
свежем снимке этого окна (`ui_click`, `ui_type`, `click_point` — оно и становится окном задачи), но не вслепую
(`type_text`, `press_keys` — `WindowChanged`); окно задачи закрылось — окно задачи то, что впереди. Delete (и Ctrl+D)
не в поле ввода — «Удалить выделенное…?» с разрешения. Набранное в мессенджере — сообщение и короткое («ок», «да»),
кроме поиска (Ctrl+K/F/E/L, F3 или щелчок по полю поиска — до Enter, Escape или Tab). Что изменилось с прошлой картинки того же монитора — обведено зелёным
(до 6 областей; сравнение серых копий клетками 64 в ширину, одиночная клетка — часы, курсор — не в счёт), в
подписи — «ничего не изменилось» или «изменился почти весь». После действия — пауза (`settle_s` 0,3 с, после
запуска программы или адреса 0,6 с) и ожидание, пока окно впереди не перестанет меняться (до 2,5 с, после запуска
— до 4 с; меняется одно и то же место — видео, крутилка — уже не ждём). `screenshot` — крупно одно окно (и когда
экран не снять). Модель не приняла картинку (400 со словом image) — повтор без неё, зрение остаётся; второй отказ
за прогон — дальше без картинок. Codex и программы-провайдеры получают картинку экрана уже с первым сообщением
хода (программы — если видят картинки). Codex в «режиме кода» (GPT-5.6+, exec) получает ответ действия одной
строкой: тексты, затем строками data URL картинок; правила треда велят после каждого действия выводить строки
«data:image/…» через `image()` (`image()` всего ответа — ошибка; проверено на codex 0.163.0). Пока агент работает (`agent.state` planning / running / waiting), оверлей Jarvis не попадает
на снимки экрана (`setContentProtection`, Windows 10 2004+): агент видит то, что под его панелью. Экран текстом — и
моделям без картинок: после действия — какие окна появились и закрылись с прошлого снимка («Окна на экране с
прошлого снимка») и новые всплывающие уведомления Windows («Новые уведомления Windows», до 3; источник — тот же
WinRT UserNotificationListener, что у «Озвучки уведомлений»; без доступа к уведомлениям — ничего, Windows
спрашивается не чаще раза в минуту); всё это — как данные. Первый `finish` со `status: done` после действий, которые что-то меняют,
не принимается сразу: модель получает свежий снимок и «Проверка перед итогом» — сверить с задачей и снова вызвать
`finish` (или доделать); так во всех трёх циклах агента (ключ API, Codex, программы-провайдеры —
`Agent.check_finish`). Реплика человека посреди работы (`Agent.interject`: фраза Jarvis, сообщение в чат агента)
приходит модели в ответе на ближайшее действие («Пользователь перебил: «…»», до снимка — не вырезается вместе
со старыми снимками; так же и план, если он готов позже первого шага), вместо итога, если пришла перед `finish`, или
новым заходом, если модель закончила без `finish` (ключ API — тот же разговор; Codex и программы — новый ход:
задача, что сделано, реплика, свежий снимок). Итог принят — агент уже не берёт реплики (`busy` — нет): фраза идёт
обычным запросом, а не теряется. `ask_user` — вопрос сообщением в чат (и вслух,
если задачу дали голосом: ответ слушается сразу, без «Джарвис», 20 с), ответ — голосом, сообщением в этот чат или
любой фразой Jarvis; нет ответа за 2 минуты — «Пользователь не ответил», модель решает сама или заканчивает
`partial`. Удачный прогон (итог `done`, все действия, что меняли экран, сценарий повторит: без инструментов MCP,
безымянных кнопок, паролей и номеров карт) несёт `meta.recipe` — его шаги действиями команд (`ui_click` →
`Ui.Click «название»`, `ui_type` → `Ui.Type`, `type_text` → `Keys.Type`/`Keys.Paste`, `press_keys` → `Keys.Press`,
`launch_app` → `Launch.App`, `open_url` → `Launch.Url`, `focus_window` → `App.Focus`, `click_point` →
`Mouse.MoveInWindow` (точка от ближнего края окна) + `Mouse.ClickLeft`/`ClickRight`/`DoubleClick`, `scroll` →
`Mouse.MoveInWindow` + `Mouse.Scroll`, `run_command` → `Jarvis.Command`, `wait` → `Wait`). «Запомнить
как сценарий» (кнопка у итога, `POST /api/agent/scenario`, голосом «запомни это» в течение 15 минут) — сценарий
с фразой человека (`Agent.run(request=…)`: его слова, а не переписанная задача; длиннее 100 знаков — обрезана по
слову): в следующий раз — сразу, без ИИ; шаг не сработал — дело снова берёт агент. Перед каждым вводом —
окно той же программы, что при последнем снимке (впереди окно Jarvis — фокус окну задачи; другая программа —
действие не выполняется); мышь не жмёт, если точку закрывает чужое окно; курсор человека возвращается на место.
Подсказки по программам и сайтам (≈30: мессенджеры, браузеры и YouTube, карты, магазины, почта, Office, Zoom,
Teams, Steam, OBS, плееры, Notion…) — `jarvis/ai/playbooks.py`, вместе с готовыми командами их паков; программа,
впервые оказавшаяся впереди, приносит свои подсказки и команды. План работы составляется параллельно с первыми
шагами (агент его не ждёт) и приходит модели в ответе на ближайшее действие. Разрешения: `settings.ai.tool_policy` с ключами
`agent.<действие>` и именами инструментов MCP; отправка, удаление и оплата через MCP спрашиваются всегда; после
отказа агент спрашивает перед каждым действием, которое что-то меняет. Связь и питание компьютера — всегда с
разрешения, в любом окне: кнопка или окно о Wi-Fi, Ethernet, Bluetooth, VPN, «в самолёте», хот-споте, мобильной
сети, о выключении, перезагрузке, сне, выходе из системы (`NETWORK_NAME`, `POWER_NAME`), любое нажатие в быстрых
настройках, центре уведомлений, «Сетевых подключениях» и «Диспетчере устройств» (пробел и Enter там — тоже).
«Стоп» — сразу и до конца: начатое действие обрывается (и когда его позвал Codex или программа-провайдер, не
цикл агента), а вызовы инструментов, что ещё придут, — «Ошибка: пользователь остановил работу» без действия.
Лимиты: `ai.max_agent_steps`, 30 минут на запуск, 3 минуты на действие.

Браузер Jarvis (`settings.ai.browser`, по умолчанию выкл; `ai.browser_app`: `auto` — Chrome, иначе Edge, `chrome`,
`edge`; `jarvis/ai/browser`) — агент работает с сайтами по Chrome DevTools Protocol, а не мышью и UI Automation.
С Chrome 136 порт отладки для профиля по умолчанию закрыт, поэтому у Jarvis свой профиль — `<data>/browser`
(`--user-data-dir`, `--remote-debugging-port=0`, `--no-first-run`, `--no-default-browser-check`; запуск —
`platform.launch`, порт — из `<профиль>/DevToolsActivePort`, только 127.0.0.1, WebSocket без прокси): человек один
раз входит в нужные сайты в этом окне; его обычный браузер не трогается. Окно открывается само при первом
`browser_open` или кнопкой в настройках; открытое раньше (Jarvis перезапустили) — подхватывается без нового окна;
порт есть, но браузер не ответил за 3 с — «не отвечает», второй не запускается; закрыли браузер — `closed`,
выключили настройку — подключение закрывается (скачивания — снова как обычно). Действия: `browser_open {url, new_tab?}`
(только http/https; «ozon.ru» — https; javascript:, file:, chrome:, data: — нет; ссылка с данными в адресе на сайт,
которого нет в задаче и среди поисковых, — с разрешения, имя сайта, похожее на подделку, — с разрешения всегда (§7);
ждёт загрузку до 10 с; переходит только во вкладке, которую
открыл агент в этом прогоне, или в пустой — во вкладке человека открывает новую), `browser_click {id, double?}`,
`browser_type {id, text, submit?}` (содержимое заменяется; в `select` — выбор варианта; `submit` — Enter),
`browser_scroll {direction: down|up|top|bottom, amount?, id?}`, `browser_press {keys}` (события клавиатуры в страницу;
`alt+left`/`alt+right` — история, `f5` — обновить, `ctrl+t` — вкладка, `ctrl+w` — закрыть, `ctrl+tab`, `ctrl+1…9`;
открыто окно сайта — `enter` OK, `escape` Отмена), `browser_wait {seconds?, text?}`, `browser_tab {number?, title?}`,
`browser_read {part?}` (вся страница частями по 6000 знаков). Каталог: группа `browser` («Агент: браузер Jarvis»),
ключи `agent.browser_*`; без настройки действия модели не даются. Правила модели (`BROWSER_RULES`) добавляются
к `AGENT_PROMPT`, когда браузер включён; правила Codex и программ-провайдеров упоминают `browser_*`.
Когда впереди окно браузера Jarvis (процесс браузера — `SystemInfo.getProcessInfo`), `Agent._look` берёт вместо
UI Automation страницу видимой вкладки (`jarvis/ai/browser/page.py`): дерево доступности
(`Accessibility.getFullAXTree`, рамки того же процесса — тоже) и раскладку (`DOMSnapshot.captureSnapshot`) —
«Страница в браузере Jarvis» (как данные, `untrusted`): вкладка, адрес, вкладки, «экран N из M», что на экране —
текстом и элементами `[b12 кнопка «Купить»]` (роль по-русски, значение, флаги: фокус, отмечен, пароль, скачивает
файл…), выше и ниже экрана — элементы списком, ближние первыми, пока хватает места (дальше — «ещё N»); элементы
не обрезаются по числу; id — по `backendNodeId`, не меняются, пока открыт тот же документ. Значения полей — из
дерева доступности (пароли там скрыты; из раскладки, где они открытым текстом, не берутся), номера карт и токены
замазаны. Нажимаемое без роли (обработчик щелчка, курсор-рука) — «элемент». Заметки: капча, форма входа, рамки
других сайтов (видны только на картинке). Картинка экрана — как всегда, метки — `b…`. Впервые за прогон — подсказка,
что приёмы «Браузер» из подсказок — для обычного браузера. Без подключения, с другим окном впереди или если
страницу снять не вышло — как раньше. Нажатие — `DOM.scrollIntoViewIfNeeded`, `DOM.getContentQuads`, проверка
`DOM.getNodeForLocation` (точку закрывает другой элемент — баннер, чужая рамка — «закрыт другим элементом», ничего не
нажато) и `Input.dispatchMouseEvent`; без размера — `click()` элемента. Ввод — `DOM.focus`, выделение прежнего,
`Input.insertText`. JavaScript от модели не выполняется (только свои функции), cookies, пароли и хранилище сайтов
не читаются. Разрешения (`Agent._approve`, в карточке — сайт и что будет сделано): рискованные кнопки (`RISKY_NAME`:
купить, оплатить, отправить, удалить, заказ…), отправка набранного в мессенджере (Telegram Web, WhatsApp, VK, почта —
по адресу сайта или заголовку окна), вход (кнопка или Enter в форме с паролем; без формы — «Войти» на странице
с полем пароля), Enter или пробел на кнопке в фокусе — как её нажатие, ввод пароля или кода (`type=password`,
`autocomplete` current-/new-password, one-time-code), данные карты (`cc-*`, номер по Луну), вставка из буфера обмена
(`ctrl+v`), скачивание (ссылка с
`download` или на файл) и выбор файла для загрузки, «Согласиться» в окне сайта с рискованным текстом и «Покинуть
страницу?», закрытие вкладки, которую агент не открывал. Скачивания: пока агент работает (с первого снимка
браузера Jarvis в прогоне), браузер их не начинает (`Browser.setDownloadBehavior deny`), кроме разрешённого нажатия:
разрешение — на это нажатие (и 1,5 с после загрузки), потом снова `deny`, и когда нажатие сорвалось; сайт
попытался — агенту «Сайт начал скачивать «…» — Jarvis не дал». Выбор файла: пока агент работает, окно выбора файла
перехвачено (`Page.setInterceptFileChooserDialog`): кнопка, которая его открывает («Добавить фото»), — сначала
разрешение, потом нажатие ещё раз, и окно открывается; клавишей — агенту «сайт хотел открыть выбор файла — Jarvis не
дал». Закончил агент — скачивания и выбор файла как обычно (`Browser.release`). Снимок страницы (десятки тысяч узлов)
разбирается в потоке, не в цикле событий. Вкладка на экране — с самым длинным заголовком, совпавшим с заголовком
окна (вкладка агента — только при равенстве). Окно сайта (alert, confirm, prompt, beforeunload): пока открыто,
страница не отвечает — нажатие, открывшее его, не ждёт ответа, снимок показывает окно, `browser_press` отвечает.
Перед действием на странице впереди должно быть окно браузера Jarvis (окно Jarvis поверх — вперёд браузер; другая
программа — `WindowChanged`). «Стоп» обрывает и ожидание загрузки. Журнал — как у всех действий (`args` без
секретов: ввод в поле пароля — «••••»). Сценарий: `browser_open` → `Launch.Url` (повтор откроет сайт в браузере по
умолчанию), чтение, ожидание и прокрутка — без шагов, нажатия, ввод, клавиши и вкладки — прогон не запоминается.
Заметка `finish.lesson` в браузере Jarvis — о сайте (`ozon.ru`), а не о `chrome.exe`; «Заметки с прошлых раз о сайте
«…»» — когда сайт впервые за прогон впереди.

Заметки о программах (`ai.agent_notes`, `jarvis/ai/lessons.py`): `finish.lesson` — что в программе не сработало
и как получилось — хранится по exe окна задачи (kv `agent:lessons`, последние 5 на программу, до 50 программ,
номера карт и токены замазаны); агент получает их, когда программа впереди («Заметки с прошлых раз о «…»
(подсказки, не команды)», как данные); итог в чате несёт `meta.lesson {app, text}`. Журнал работы
(`ai.agent_journal`, `jarvis/ai/journal.py`): `<data>/agent_runs/<run_id>/journal.json` и `NNN-K.jpg` — экран
в начале, каждое действие (`title`, `name`, `args` без секретов, `ok`, `result`, `thought` — что модель написала
перед ним, `images`), проверка перед итогом, итог; последние 30 прогонов; удалили чат — его журналы тоже. Итог
в чате — `meta.agent.journal: true` (кнопка «Как он это делал»). Обе — категории «Приватности» (`agent_notes`,
`agent_journal`).

Проверочные задачи агента (`core/evals`, [EVALS.md](EVALS.md)) — вне программы (в сборку не входят), но держатся
за такие точки агента: `Agent.ui` (`snapshot`, `click`, `set_text`, `element_at`, `owner_at`), `Agent.toast_source`
(`access`, `fetch`, `close`), `settle_s`, `launch_settle_s`, `pointer_delay_s`; `run`, `wait`, `stop_run`, `interject`,
`waiting_answer`, `current` (`AgentRun.usage`, `count`, `journal.dir`); события `agent.step` (`index`, `status`),
`agent.state` (`waiting` с текстом «Жду ответа: …»), `confirm.request`; строка снимка окна (`format_snapshot`) и
подписи картинок («Снимок экрана — W×H точек», «Лупа: кусок экрана W×H точек, ×Z — картинка A×B») — по ним модель
по сценарию находит элементы и точки. Меняешь что-то из этого — `uv run pytest tests/evals`.

Провайдер `chatgpt` — «ChatGPT (подписка)» (`jarvis/ai/chatgpt`): модели GPT по подписке ChatGPT через Codex
app-server (SDK `openai-codex==0.163.0` с бинарём Codex, ≈150 МБ, скачивается в `<data>/pylib` по кнопке; extra
`chatgpt` в pyproject). Вход — `<data>/codex-home` (CODEX_HOME; вход прежнего Jarvis там же и подхватывается сам),
рабочая папка Codex — пустая `<data>/chatgpt`. «Настроено» = компонент есть и вход выполнен. Один процесс Codex на
ядро (без окна консоли), тред Codex на чат Jarvis (`kv` `chatgpt:thread:<chat_id>`, переживает перезапуск; не
продолжился — новый тред с пересказом переписки; удаление чата удаляет тред). Инструменты Jarvis — `dynamicTools`
треда: Codex присылает `item/tool/call`, ядро выполняет инструмент так же, как у провайдеров по ключу (те же
`chat.tool`, подтверждения, источники, память, режим «Чат»). Shell, правки файлов, под-агенты Codex — запрещены
(отказ на запросы разрешений; ход, где модель запустила под-агента, прерывается вместе с под-агентом). Агент с
этим провайдером — ход Codex с действиями
агента как инструментами. `complete()` — временный тред, JSON — `outputSchema`. Ошибки — по-русски: регион/VPN,
лимит подписки, «Войдите в ChatGPT в настройках Jarvis AI», нет связи (повторы Codex обрываются через 30 с).

Провайдеры-программы (`jarvis/ai/cli`): `claude_code` — «Claude (подписка)» через Claude Code, `gemini_cli` — Gemini
CLI, `qwen_code` — Qwen Code. Jarvis не входит в аккаунты сам, не скачивает Claude Code и не читает чужие токены:
находит программу человека (PATH ядра и из реестра, `%APPDATA%\npm`, `%USERPROFILE%\.local\bin`,
`%LOCALAPPDATA%\qwen-code\bin`, WinGet, pnpm, Scoop, Volta; обёртки npm `.cmd` запускаются без cmd.exe — node.exe со
скриптом) и запускает её в документированном режиме без окна: `claude -p --output-format stream-json …`,
`gemini --output-format stream-json …`, `qwen --output-format stream-json …`. Вход проверяется самой программой
(`claude auth status`) или по наличию её файлов (Gemini CLI, Qwen Code); вход — в окне терминала с программой
(`POST /api/ai/cli/{provider}/login`). Gemini CLI и Qwen Code ставятся кнопкой (`npm install -g` официального пакета),
Claude Code — только официальным установщиком (`irm https://claude.ai/install.ps1 | iex`). Программа работает в
пустой папке `<data>/cli/<программа>`, её встроенные инструменты (оболочка, файлы, веб) выключены флагами и
настройками, а инструменты Jarvis приходят с `/api/ai/mcp` (`--mcp-config` у Claude Code и Qwen Code,
`.gemini/settings.json` рабочей папки у Gemini CLI — токены там только ссылками на переменные окружения). Вызовы
идут тем же путём, что у провайдеров по ключу (карточки, подтверждения, источники, память, режим «Чат»). Текст
человека — через stdin, системный промпт — файлом, контекст хода (время, профиль, память) — блоком `<jarvis>` в
сообщении. Сессия программы на чат — `kv` `cli:<provider>:session:<chat_id>` (продолжение `--resume`; не
продолжилась — новая с пересказом переписки). «Стоп» убивает процесс с потомками. Агент — один запуск программы
с действиями агента как инструментами. Ошибки — по-русски (не установлена, нужен вход, лимит подписки со временем
сброса, модель недоступна, регион, нет связи). Расход записывается с ценой 0 (подписка). `ANTHROPIC_API_KEY` в
окружение Claude Code не передаётся (иначе он платит ключом, а не подпиской).

Модель на этом ПК меньше 14B (`qwen3:8b`, `gemma-3-12b`; `llm.weak_local_model`) для долгих задач слабовата:
фоновая задача пишет предупреждение в свой ход и показывает тост (раз за запуск на модель), «Диагностика» — `warn`
у Jarvis AI. `LlmClient.timings` — скорость последних 500 запросов (`{provider, model, seconds, ttft,
completion_tokens}`), `speed_stats()` — медиана времени до первого токена и токены в секунду.

**«Приватно: …»** (`Ai.ask`, `PRIVATE_PHRASE`: «приватно», «локально», «по секрету», «без облака», «только на этом
ПК» в начале фразы) — ответ в режиме «Чат» только моделью на этом ПК (`LlmClient.private_config`: основная, если она
на этом ПК, иначе запасная, если она на этом ПК), без инструментов, которые ходят в интернет (`NETWORK_TOOLS`:
поиск, страницы, погода, новости, курсы, агент) и без серверов MCP; без запасного в облаке. Модели на этом ПК нет —
ошибка `PRIVATE_NONE`, в облако ничего не уходит. Префикс в чат не пишется.

**Запасной ИИ** (`settings.ai.fallback_provider` — провайдер по ключу или на этом ПК, не подписка; `fallback_model` —
пусто: модель по умолчанию; `fallback_base_url` — пусто: адрес пресета; ключ — секрет `ai_fallback_api_key`, у того
же провайдера, что основной, — его ключ). `LlmError.kind` — вид сбоя: `auth`, `quota`, `rate`, `region`, `server`,
`network`, `timeout` (запасной поможет) или `config`, `model`, `too_long`, `rejected`, `format` (не поможет — чинить
запрос или настройки). Основной провайдер по ключу не ответил так, что поможет запасной (после своих повторов), и
на экран ещё ничего не ушло — `LlmClient.chat` повторяет тот же запрос запасному, а основной ставит на паузу
(`OUTAGE_COOLDOWN`: квота — 30 мин, ключ и регион — 10, сервер — 2, связь — 1, частота — по `Retry-After`, 10 с —
10 мин): пока пауза, `LlmClient.config()` отдаёт запасной, и основной не зовётся вовсе. Подписка ChatGPT и
программы (Claude Code, Gemini CLI, Qwen Code) после сбоя хода (лимит подписки, нет входа, регион, сбой, нет
связи; `note_outage(provider, reason)`) тоже встают на паузу — `Ai.account_backend()` тогда `None`, и ответы,
агент и фоновые задачи идут через запасной по ключу (настройки — проверка связи и список моделей —
`account_backend(routed=False)` и `llm.primary_config()`: всегда основной). Без запасного пауза не ставится.
Новые настройки ИИ или ключ снимают все паузы.

Провайдеры по ключу — OpenAI-совместимый `/chat/completions` с особенностями пресета (`jarvis/ai/providers.py`):
регионы (Qwen/Model Studio, Kimi, GLM, MiniMax, Doubao, SiliconFlow), YandexGPT — заголовки
`Authorization: Api-Key <ключ>` (IAM-токен `t1.…` — `Bearer`), `OpenAI-Project: <folder_id>` и
`x-data-logging-enabled: false` (не сохранять запросы), модель
`gpt://<folder_id>/<модель>` (`settings.ai.folder_id`); Mistral — id вызовов из 9 символов; Doubao — без ключей длины
в схемах инструментов; Gemini — подписи рассуждений (`extra_content`) возвращаются вместе с вызовами; убранные
провайдером модели (`deepseek-chat`, `deepseek-reasoner`) заменяются моделью по умолчанию.

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
timer, stopwatch, focus, rates, crypto, stocks, news, system, apps, quick, recent, volume, smarthome, pet, ai, worldclock,
countdown, network, battery, sticky, plugins, desk_links`.

«Системные операции» (`jarvis/widgets/sysops.py`, `jarvis/api/sysops.py`; страница интерфейса `#/system`): нагрузку,
погоду и «Сейчас играет» панель берёт у виджетов `system`, `weather`, `media`, остальное — здесь.

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/system/sensors` | — | `{cpu_temp, gpu_temp, gpu_name, gpu_load, top: [{name, cpu, ram_mb}], updated}` — температура в °C (нет датчика — `null`): видеокарта NVIDIA — `nvidia-smi`, процессор — счётчики WMI «Thermal Zone» (без прав администратора); не нашлось — повтор через 10 минут; `top` — 5 самых загруженных программ (одноимённые процессы — одной строкой, `cpu` — % всех ядер) |
| `GET /api/system/mixer` | — | `{master: {volume, muted}, items: [{app, title, level, muted, active}]}` — программы со звуком (`platform.app_volumes()`): `app` — имя процесса или `system` (системные звуки), `active` — звучит сейчас; несколько сессий одной программы — одна строка |
| `POST /api/system/mixer` | `{app, level 0..100}` | то же, что `GET` — громкость одной программы (`platform.app_volume_set`); программа без звука — 400 |

Голосом: «открой системные операции», «покажи панель системы», «мониторинг системы» — динамическая команда
`sysops:open` (категория `system`) → `ui.show {page: "system"}`; «микшер» остаётся встроенной командой (микшер
Windows).

`Plugin = {id, name, description, icon, color: [от, до], enabled, config, schema, status: {running, error?, hint?,
active?, …}, actions: [{id, title}], examples: [фраза]}`. Плагины: `smart_home, dictation, subtitles, summary, reader,
autolock, second_pc, notify_voice, pet, focus, clipboard, file_search`. `status.hint` — подсказка человеку (что
настроить); `status.active` — у плагинов с сеансом (субтитры, диктовка): включённый плагин ещё не значит, что сеанс
идёт, — интерфейс пишет «Готов к запуску» и показывает только уместное действие `start`/`stop`.

Поле схемы: `{key, kind: text|number|bool|select|secret|hotkey|device|list, label, hint?, placeholder?, default?,
options?: [{value, label}], min?, max?, step?, device?: input|output (список — из /api/voice/devices),
fields?: [поля элемента списка] (kind list), secret?: имя, set?: bool (kind secret — задан ли)}`. В
`PUT /api/plugins/{id}` секрет: строка — сохранить, `""` — не трогать, `null` — удалить.

| Плагин | Действия (`POST …/action`) | События `plugin.<id>.*` |
|---|---|---|
| `smart_home` | `states`, `toggle`, `turn_on`, `turn_off`, `set`, `refresh` | `devices {items}` |
| `dictation` | `toggle`, `start`, `stop` | `state {state: listening\|processing\|idle, text}` |
| `subtitles` | `start`, `stop` | `text {id, text, translation, lang, pending}`, `state {active}` (значок «Субтитры» в оверлее и в трее) |
| `summary` | `start`, `stop`, `retry` | `state {state: recording\|transcribing\|summarizing\|done\|error, seconds}` |
| `reader` | `read {text?\|url?}`, `pause`, `resume`, `stop`, `speed {speed}`, `seek {index}` | `progress {state, index, total, sentence, sentences?}` |
| `autolock` | `pause`, `resume`, `check` | `state {present, last_seen, armed}` |
| `second_pc` | `ping`, `dispatch {pc, text}` | `state` |
| `notify_voice` | `test`, `request_access` | `state` |
| `pet` | `feed`, `pet`, `eat {path}` | `state {playing, recycle_count, windows, fullscreen}`, `action {action}` |
| `clipboard` | `list`, `paste {id\|text}`, `copy {id\|text}`, `pin {id, pinned?}`, `delete {id}`, `clear {all?}`, `ai {action, text?\|id?, card?}`, `palette` (`toggle`) | `items {items, total, pinned}`, `palette {items, actions, at, selection, selection_chars, target, ai, hotkey}`, `result {id, state: working\|done\|replaced\|error, action, title, source, text?, error?, at}` |
| `focus` | `start {minutes?}`, `pause`, `resume`, `toggle`, `stop`, `break`, `skip_break`, `allow {key}`, `stats`, `state` | `state {phase: idle\|focus\|break\|paused, paused_from, reason: user\|idle, left_s, total_s, ends_at, cycle, next, waiting, today_min, today_sessions, streak, …}`, `alert {kind: focus_done\|break_done\|back\|distraction, title, text, …}` |
| `file_search` | `search {text?, kinds?, date?, folder?, sort?, content?, limit?}`, `open {path\|index}`, `reveal {path\|index}`, `copy_path {path\|index}`, `toggle`, `box {open}`, `reindex` | `results {query, items, total, ttl}`, `box {open, query?}`, `opened {path, index, reveal?}` |

«Озвучка уведомлений» (`notify_voice`) не читает служебный шум никогда, даже без фильтров программ: «Ножницы» и
другие программы снимков экрана, «Снимок экрана скопирован / сохранён» (`notify_voice.noise`); агенту такие
уведомления тоже не показываются.

«Фокус» (`focus`): `plugin.focus.state` приходит при каждом изменении и раз в минуту, пока идёт таймер, — секунды
интерфейс отсчитывает сам от `left_s` (`ends_at` — unix-время конца, только пока таймер идёт); `waiting` — перерыв
кончился, ждём следующий фокус серии (`next: focus`). `alert`: `focus_done {minutes, break_min, long}` — начался
перерыв, `break_done {auto}`, `back` — человек вернулся после автопаузы по бездействию, `distraction {key, name,
icon, site, closed}` — в активном окне отвлекающая программа или сайт (`allow {key}` — «Нужно по работе» до конца
серии). `stats` → `{today: {date, min, sessions, distractions}, week: [{date, min, sessions}] (7 дней, старые
первыми), streak, best: {date, min} | null, total_min, total_sessions}`; то же лежит в `status` плагина.

«Умный буфер» (`clipboard`): история буфера (`ClipboardItem = {id, text (начало, до 400 знаков), chars, ts,
pinned}`, новые сверху; копии с пометками менеджеров паролей «не запоминать» пропускаются — счётчик `status.skipped`),
хранится в `kv` `clipboard:items`. `paste`/`copy` — запись по `id` целиком или текст карточки; вставка — в окно,
активное при открытии палитры или взятии выделенного. `ai {action}`: `translate` (русский ↔ `config.target_lang`),
`fix`, `shorten`, `polite`, `explain`, `reply` и `read` (вслух, без ИИ); с `text`/`id` — вернуть `{action, title,
text, replaced: false}` (`card: true` — ещё и карточкой), без них — с выделенным текстом: итог встаёт на его место
(`config.replace`; кроме `explain`/`reply`) или приходит карточкой `result`. ИИ не настроен — 400 «Подключите ИИ
в настройках, сэр». Хоткей плагина (`toggle`) — палитра: `palette.at` — курсор в физических пикселях экрана,
`selection` — выделенное (до 400 знаков), `target {title, exe}` — куда вставится; повтор хоткея быстрее 0,6 с
не открывает палитру заново.

«Поиск по ПК» (`file_search`): ищет через индекс Windows (OLE DB `Search.CollatorDSO`, `platform.search_index`), без
него — по своему индексу «Загрузок», «Документов», «Рабочего стола», «Изображений», «Музыки», «Видео» и
`config.include` (кэш — `cache/file_search_index.json`); `status {mode: index|own, index_available, files, scanning,
indexed_at, truncated, index_error?}`. `search`: `text` — фраза по-русски («pdf в загрузках за неделю», «презентация
про бюджет», «файлы со словом отчёт»), уточнения `kinds: [document|spreadsheet|presentation|image|video|audio|
archive|pdf|folder]`, `date: today|yesterday|before_yesterday|week|last_week|7d|month|last_month|30d|year|
last_year|365d`, `folder: downloads|documents|desktop|pictures|music|videos`, `sort: recent|relevance`, `content` —
слова в тексте файлов; ответ `{query, items: FoundFile[], total, mode, content_skipped, parsed}`, `FoundFile = {path,
name, kind (как в kinds или other), size, modified, folder, where}` (`where` — «Документы › Проекты»). `index` —
номер в последних результатах (с 1). Голос: одно явное совпадение открывается сразу («Открываю …»; программу или
сценарий — после подтверждения), несколько — карточка `results` и «Нашёл N — какой открыть?»: `ttl` секунд работают
«открой второй», «номер три», «покажи первый в папке» (`plugin.file_search:pick:<n>` / `reveal:<n>`), ничего —
«Не нашёл …». `box` — быстрый поиск в оверлее по хоткею (`toggle`), закрыть — `box {open: false}`. Jarvis AI:
инструменты `find_files` и `open_file` (режим «Всё вместе»; путь не из `find_files` и программы — с подтверждением).

Инструменты Jarvis AI от плагина: `ctx.ai.tools.register_provider(<id>, provider)` → отписка; `provider()` отдаёт
список `Tool` (`jarvis.ai.tools`) и перечитывается при каждом ответе; встроенные инструменты не перекрываются, в
режиме «Чат» берутся только `in_chat_mode`.

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

## 6. Сценарии (`jarvis/scenarios`, `jarvis/api/scenarios.py`)

«Когда <триггер> [и <условия>] → сделать <действия>, сказать <фразу>». Хранятся в `kv` (`scenarios:item:<id>`,
вместе со служебными `armed_at` и `marks`, которых API не отдаёт).

```json
{
  "id": "3f9c0a1b2c3d4e5f",
  "name": "Доброе утро",
  "icon": "sunrise",
  "enabled": true,
  "triggers": [
    {"type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4]},
    {"type": "phrase", "phrases": ["доброе утро"]}
  ],
  "conditions": {"weekdays": [], "time_from": "", "time_to": ""},
  "actions": [{"type": "Jarvis.Briefing", "value": ""}],
  "say": "",
  "template": "good_morning",
  "last_run": 1792390800.0, "last_ok": true, "last_trigger": "time", "last_error": "",
  "runs": 3, "created": 1792300000.0, "updated": 1792300000.0
}
```

| Триггер `type` | Поля | Когда срабатывает |
|---|---|---|
| `time` | `at: "HH:MM"`, `weekdays: [0..6]` (0 — Пн; `[]` — каждый день) | в `at` по местному времени. Пропущенное, пока компьютер спал или Jarvis был выключен, — один раз, если опоздание меньше 10 минут, иначе пропуск. Новый или изменённый триггер (и снова включённый сценарий) прошлое не догоняет |
| `interval` | `minutes: 1..1440`, `only_active: bool` | каждые N минут; после сна — один раз. `only_active` — считаются только минуты за компьютером (ввод не реже раза в 2 минуты), перерыв дольше 10 минут обнуляет счёт |
| `phrase` | `phrases: string[]` (слоты как у команд: `{number}`, `{text}`…) | фраза — динамическая команда `scenarios:<id>` «Сценарий «…»» |
| `jarvis_start` | — | при запуске ядра (по `core.ready`, один раз) |
| `app_open` / `app_close` | `app` — название из «Пуска» или exe | программа открылась / закрылась (`platform.is_running` раз в ~3 с; открытая до запуска Jarvis — не событие) |
| `idle_return` | `after_min: 1..1440` | человек вернулся после N минут без ввода (`platform.idle_seconds`) или после сна компьютера |
| `battery_low` | `percent: 1..99` | заряд ≤ N% без зарядки (`system_stats`, раз в минуту); один раз за разрядку — снова после зарядки или подъёма на 3% |
| `calendar_event` | `before_min: 0..1440` | за N минут до события календаря (кроме «весь день»); перенесённое событие — снова; опоздание больше 10 минут — пропуск |

Условия (`conditions`) — дни недели и окно времени (`22:00`–`06:00` переходит через полночь; пусто — без
ограничения); проверяются для всех триггеров, кроме ручного запуска. Сработавший триггер передаёт действиям слоты:
`app_open`/`app_close` — `{app}`, `calendar_event` — `{text}` (название события) и `{number}` (минуты),
`battery_low` — `{number}` (заряд), `idle_return` — `{number}` (минут отсутствия), фраза — свои слоты.

Выполнение — исполнителем команд как своя команда человека (все действия, включая `Launch.Shell`; подтверждения
вложенных `Jarvis.Command` — как обычно), затем `say` (`{address}` → обращение из профиля). Один сценарий не
выполняется дважды одновременно: триггер во время выполнения пропускается, ручной запуск — 400 «Сценарий «…» уже
выполняется». Итог — `scenario.run` и `scenarios.changed {op: "update"}` (`last_run`, `last_ok`, `runs`…). Сценарий,
запущенный триггером или кнопкой, пишет `HistoryItem` (`source: "scenario"`, `text` — триггер: «Будни 8:00»,
«Запуск вручную»; `command_id: "scenarios:<id>"`), тост `{title: "Сценарий «…»", message, status}` и говорит `say`
(не в тихом режиме; ошибки не проговаривает). Фразу (голос, строка) записывает и озвучивает диспетчер, как любую
команду.

Голосом: фразы сценариев; «запусти / выполни сценарий {text}» (`scenarios:run` — вручную: без условий, и
выключенный), «включи / выключи сценарий {text}» (`scenarios:enable` / `scenarios:disable`). Название ищется
нечётко. Категория команд — `scenarios` («Сценарии»); пока сценариев нет, команд тоже нет.

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/scenarios` | — | `{items: Scenario[]}` (по времени создания) |
| `POST /api/scenarios` | `ScenarioIn` | `Scenario` |
| `GET /api/scenarios/templates` | — | `{items: ScenarioTemplate[]}` |
| `POST /api/scenarios/from-template` | `{template_id}` | `Scenario` (включённый; имя занято — «Доброе утро 2»); неизвестный шаблон — 404 |
| `GET /api/scenarios/{id}` | — | `Scenario` |
| `PATCH /api/scenarios/{id}` | часть `ScenarioIn` (`{enabled}` — включить/выключить) | `Scenario` |
| `DELETE /api/scenarios/{id}` | — | `{ok}` |
| `POST /api/scenarios/{id}/run` | — | `ExecResult` (`data.trigger: "manual"`); условия не проверяются |
| `POST /api/scenarios/{id}/stop` | — | `{ok, stopped}` — остановить, если выполняется (итог запуска — «Остановлено», `data.cancelled`); 404 — нет сценария |

`ScenarioIn = {name, icon?, enabled? (true), triggers (1–10), conditions?, actions (до 60), say?, template?}` (`template` — id
шаблона, если сценарий настроен из витрины; незнакомый не запоминается) — нужны действие
или `say`; ошибка проверки — 400 с текстом («Триггер 2: время запуска пишется как «08:00»»), неизвестный id — 404,
название — уникальное (без учёта регистра). `ScenarioTemplate = {id, name, icon, description, triggers,
conditions, actions, say, added}` (`added` — сценарий из шаблона уже есть; `{address}` уже подставлен). Шаблоны:
`good_morning` «Доброе утро», `leaving` «Ухожу», `game_mode` «Игровой режим», `end_of_day` «Конец рабочего дня»,
`eye_break` «Перерыв для глаз», `welcome_back` «С возвращением».

Значки сценариев (рисует интерфейс): `sparkles, sunrise, sunset, moon, door, gamepad, eye, wave, coffee, bolt,
briefcase, battery, calendar, bell, music, home, rocket, heart, book, monitor` или ключ из §1 (`steam`…). В тостах
и истории — ближайший ключ §1 (`sunrise` → `weather`, `door` → `lock`…).

`Jarvis.Briefing` — «Сводка дня» без ИИ, `value`: `""` — приветствие по времени суток, дата, погода
(`profile.city`), встречи и задачи на сегодня (с просроченными), курсы доллара и евро, 3 новости; `short` — без
курсов и новостей; `tasks` — все незакрытые задачи; `missed` — «С возвращением» и напоминания (`reminder.fired`),
сработавшие за время отсутствия (без известного ухода — за последний час); `today` — дела на сегодня по времени:
просроченные задачи, задачи со сроком сегодня, встречи и напоминания (таймеры — нет), «Сэр, на сегодня у вас 5 дел:
оплатить интернет — просрочено, в 10 утра — планёрка, в 7 вечера — сходить в банк…» (с персоной «Джарвис» первая
фраза — готовая: «Сверяюсь с расписанием, сэр. У вас 5 дел: …»), ничего не назначено — задачи без срока; `welcome` —
приветствие в характере («С возвращением, сэр.»), напоминания, сработавшие без человека («Пока вас не было, сработало
напоминание: …»), напоминания и встречи на ближайшие 3 часа («Также напоминаю: в 7 вечера — сходить в банк.»),
сколько задач на сегодня; ничего — «Новых напоминаний нет, всё под контролем.». Уход — с последнего ввода (не трогали
клавиатуру и мышь 2+ минуты), иначе последний известный (приветствие по возвращении, сценарии), иначе час назад;
`hello` — то же с приветствием по времени суток («доброе утро» вечером — «Добрый вечер, сэр.») и погодой, если она
пришла за 0,4 с (из кэша виджетов; иначе без неё — ответ не ждёт сеть). Текст — в `message` (тост), голосом —
`say`; сбой сети — часть просто пропускается. Спросить ИИ — `Jarvis.Ask`, уведомление без голоса — `Notify`.

| Событие | Данные |
|---|---|
| `scenarios.changed` | `{op: add\|update\|delete, item?, id?}` |
| `scenario.run` | `{id, name, trigger: time\|interval\|phrase\|jarvis_start\|app_open\|app_close\|idle_return\|battery_low\|calendar_event\|manual, ok, error?, cancelled?}` |

## 7. Приватность и данные (`jarvis/retention.py`, `jarvis/redact.py`, `jarvis/api/data.py`)

Сроки хранения — `settings.privacy` (дни; 0 — хранить, пока человек не удалит сам): `history_days` (90) — история
команд, `chat_days` (0) — чаты Jarvis AI без новых сообщений (закреплённые не трогаются), `voice_chat_days` (30) —
сообщения голосового чата, Telegram и пульта, `tool_results_days` (30) — скрытые результаты инструментов и
картинки старых сообщений (текст переписки остаётся; у сообщения появляется `meta.images_removed`), `audio_days`
(1) — записи «Конспекта» (не трогаются, если в плагине включено «Хранить запись»), `logs_days` (14) — журналы ядра
(большие обрезаются до последнего мегабайта). Очистка идёт раз в сутки (первая — через минуту после запуска);
временные записи памяти с истёкшим сроком удаляются всегда. База — `PRAGMA secure_delete` (удалённое затирается).

Секреты не попадают в журналы: `jarvis.log.RedactFilter` прогоняет сообщение и текст исключения через
`jarvis.redact.redact()` — номера карт (по Луну), CVV, «пароль: …», пин-коды, ключи (`sk-…`, `ghp_…`, `xox…`,
`AIza…`, `y0_…`, `glpat-…`, `hf_…`, Stripe `sk_live_…`, Yandex Cloud `AQVN…` и IAM `t1.…`, Google `ya29.…`, токены
ботов Telegram, `Bearer`/`Basic`, JWT), закрытые ключи PEM целиком, пароль в адресе подключения
(`postgres://user:•••@host`) и `key=…`/`token: …`/`*_SESSION_STRING=…`.

**Наружу — без секретов** (`settings.ai.mask_secrets`, по умолчанию `true`): тот же `redact()` проходит всё, что
уходит облачной модели, — сообщения в API провайдера (`redact_messages`: текст и текстовые части, картинки как есть;
модели на этом ПК и в локальной сети — без маски), запросы, инструкции и результаты инструментов подписки ChatGPT
(Codex), задание и системный текст программ (Claude Code, Gemini CLI, Qwen Code), ответы MCP-сервера Jarvis.
Модель видит `•••` вместо ключа; печатает и вводит Jarvis то, что сказал человек.
YandexGPT получает заголовок `x-data-logging-enabled: false` — Yandex не сохраняет запросы и ответы у себя.

**Окна, скрытые от ИИ** (`jarvis/ai/private_windows.py`): менеджеры паролей (KeePass, KeePassXC, 1Password,
Bitwarden, LastPass, Dashlane, Enpass, RoboForm, Keeper, NordPass, Proton Pass, Password Safe, Kaspersky Password
Manager), окно «Безопасность Windows» с вводом пароля и запрос UAC, а также программы и слова заголовка из
`settings.ai.private_apps` («keepass» / «keepass.exe» / «Сбербанк Онлайн»). Агент такое окно не снимает и не читает
(ни элементов, ни снимка — модели только «окно скрыто от ИИ»), в нём не кликает и не печатает; снимок всего экрана,
на котором видно такое окно, не делается (лупа на его месте — тоже); «что на экране?» с таким окном впереди — без
снимка, с пояснением.

**Ссылки**: имя сайта не латиницей в вопросе «Открыть …?» показывается и в настоящем виде (punycode:
`пример.рф` — `xn--e1afmkfd.xn--p1ai`); имя, похожее на подделку (буквы разных алфавитов в одном имени —
`gооgle.com` с русскими «о»; имя целиком из русских двойников латинских букв вне русских зон — `аррӏе.com`),
спрашивается всегда, даже без данных в адресе, с предупреждением и настоящим именем в заголовке, и только кнопкой
(`jarvis.ai.policy.lookalike_host`, `link_note`) — в чате, у агента на экране и в браузере агента.

`core/tests/test_hygiene.py` проверяет всё, что лежит в git: ни ключей и паролей (тесты с поддельными — не в счёт),
ни личных путей (`C:\Users\<имя>` — только образцы вроде `me`), ни адресов почты не на `example.*`/`noreply`, ни
программ, моделей, баз, журналов и ключевых файлов; текст — до 1 МБ, двоичный файл — до 512 КБ.

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/data/inventory` | — | `{dir, logs_dir, db_bytes, purged_at, items: DataCategory[]}` |
| `POST /api/data/clear` | `{scopes: [id…]}` (`"all"` — всё, что можно удалить) | `{ok, cleared: {id: сколько}}`; неизвестная или неудаляемая категория — 400 |
| `POST /api/data/purge` | — | `{ok, removed: {…}}` — очистить по срокам сейчас |
| `GET /api/logs` | — | `{dir, items: [{name, size, modified}]}` — журналы ядра |
| `DELETE /api/logs` | — | `{ok, cleared}` — открытые журналы обрезаются, старые копии удаляются |

`DataCategory = {id, title, hint, count, bytes, clearable}`; `id`: `history, chats, voice_chats, images, memory, notes, tasks,
events, reminders, clipboard, summaries, screenshots, file_index, voice_cache, usage, logs, secrets` и `models` (не удаляется
отсюда — «Голос и распознавание»). Чаты удаляются через Jarvis AI — вместе с тредами Codex и сессиями программ.
`images` — нарисованные картинки в ответах ассистента (`meta.images` при `meta.generated`): очистка убирает их, текст
остаётся, у сообщения — `meta.images_removed`; по сроку `tool_results_days` картинки уходят и из ответов ассистента.

| Событие | Данные |
|---|---|
| `data.purged` | `{removed: {memory?, history?, chats?, voice_chats?, tool_results?, summaries?, logs?}, ts}` |
| `data.cleared` | `{cleared: {id: сколько}, ts}` |
| `notes.changed` / `tasks.changed` / `events.changed` / `reminders.changed` | ещё и `{op: "clear"}` — удалены все |

## 8. Фоновые задачи (`jarvis/background`, `jarvis/api/background.py`)

Агент работает над большой задачей сам, в фоне: ищет, читает страницы, пишет итог в хранилище заметок
(Obsidian). Экрана, мыши, клавиатуры, команд Jarvis и запуска программ у него нет. Устройство и план —
`docs/BACKGROUND_TASKS.md`.

- **Очередь**: одновременно идёт не больше `settings.background.parallel` задач; остальные — `queued`. Задача,
  которая работала, когда Jarvis закрыли, при запуске снова встаёт в очередь и продолжается с плана и хода работы
  (новый «заход»: `runs` + 1). Состояние — файлом `<данные>/background/<id>.json`.
- **Провайдер** — как у Jarvis AI: ChatGPT (подписка) — ход Codex с его веб-поиском; Claude Code, Gemini CLI,
  Qwen Code — запуск программы с её веб-поиском и чтением страниц (оболочка и файлы — выключены), инструменты
  Jarvis — с сервера MCP ядра; ключ API — цикл Jarvis (`temperature` 0.2) с поиском DuckDuckGo/Bing.
- **Инструменты задачи**: `web_search`, `web_read` (если у провайдера нет своих), `vault_search`, `vault_read`,
  `vault_list`, `vault_write`, `measure_add`, `update_plan`, `report`, `ask_user`, `finish`, инструменты серверов MCP
  (рискованные — с разрешения, см. ниже). Сорвалось действие — остальные действия того же хода модели не
  выполняются (ответ «Не выполнено: предыдущее действие этого хода не удалось…»); одно и то же действие с тем же
  ответом дважды подряд — подсказка сменить путь, пять раз — заход останавливается (`partial`); за 10, 5, 3, 2 и 1
  действие до `settings.background.max_steps` — предупреждение модели; после лимита действий или времени
  (`max_hours`, без ожидания ответа) — просьба вызвать `finish`, потом остановка (`partial`).
- **Вопросы**: `ask_user` (и разрешение на рискованное — `kind: "approval"`, варианты «Разрешить» / «Отклонить»)
  ставит задачу в `waiting` и ждёт ответа до 9 минут; дольше — заход заканчивается, задача ждёт, а ответ
  (`POST …/answer`) запускает новый заход. Разрешение, данное после паузы, запоминается (`approved`) — то же
  действие не спрашивается снова.
- **Повторы** (`jarvis/background/repeat.py`): `repeat = {kind: "daily", at: "ЧЧ:ММ", weekdays: [0…6]}` (пусто —
  каждый день) или `{kind: "hours", hours: 1…168}`; `{}` — не повторяется. При создании `repeat: null` — повтор
  ищется в начале текста («каждый вечер …», «по будням в 10 …», «каждые 3 часа …»: остаток — название задачи).
  Повторяющаяся задача между запусками — `scheduled` (`next_run` — когда следующий) или `paused`; раз в 15 с
  ядро запускает те, чьё время пришло (пропущенное, пока Jarvis был закрыт, — один раз), запуск — новый `cycle`:
  план, лимиты действий и времени и разрешения (`approved`: разрешённое в прошлом запуске снова спрашивается) —
  заново, итог — в `history` (`{cycle, ts, status, summary, steps}`, до 60), потом снова `scheduled`; «каждые N
  часов» — от начала прошлого запуска. Рассказ после повтора («каждый день я трачу…») — не повтор. Три неудачи подряд (`fails`) — `paused`. Отмена — повторы останавливаются
  (`cancelled`); «Ещё раз» (`retry`) — запуск сейчас, дальше по расписанию; `resume` — снова по расписанию.
- **Замеры** (`jarvis/background/measure.py`): `measure_add {table, values: [{column, value}]}` — строка в
  `Замеры/<table>.md` своей папки («/» в названии — «-»; Markdown-таблица, первая колонка «Дата»: `ГГГГ-ММ-ДД`, у
  задачи «каждые N часов» — `ГГГГ-ММ-ДД ЧЧ:00`, дата без часа — сегодня текущий час; та же дата — строка
  заменяется; новая колонка — сама; `\|` в ячейке человека — не граница) и график
  `Замеры/<table>.svg` рядом (числовые колонки, до трёх; в заметке — `![[…svg]]`). Таблицы задачи — `tables`.
- **«Входящие»** (`jarvis/background/inbox.py`, `settings.background.inbox`): строки в `<папка агента>/Входящие.md`
  (пункты «- …», «- [ ] …», «1. …» или просто строки; с отступом под пунктом — его подробности) становятся
  задачами (`source: "vault"`, до 5 за раз), когда файл не правили 30 с (человек может ещё печатать), и
  отмечаются «- [x] … — [[заметка задачи|задача]]» (не взялась — «- [x] … — ⚠️ не взято: причина»). Задача
  поставлена, а строку отметить не вышло (файл занят) — помнится (`kv background:inbox_pending`): строка будет
  отмечена позже, второй задачи не будет. ИИ не настроен — строки ждут; файл не в UTF-8 — не трогается. Агент
  сам в «Входящие» и «Инструкции» не пишет (`vault_write` — ошибка «ведёт человек»).
- **Хранилище** — `settings.background.vault`; пусто — открытое хранилище Obsidian из его списка (`obsidian.json`),
  нет — `Документы\Jarvis`. Агент пишет только в свою папку (`settings.background.folder`, «Jarvis»): `README.md`,
  `Инструкции.md` (указания человека — в промпт каждой задачи, если отличаются от шаблона), `Входящие.md`, `Задачи/` (заметка
  задачи — её ведёт Jarvis: свойства `type: jarvis-task, status, created, updated, finished, sources, steps`, бриф,
  итог, план, вопросы, ход работы, источники), `Исследования/`, `Замеры/`, `Журнал/<дата>.md` (строка на
  законченную задачу). Читать и искать — всё хранилище (`read_vault`), кроме служебных папок (`.obsidian` и
  других с точкой).
- **Уведомления** — `settings.background.notify_question` / `notify_done`: список из `toast` (тост с `task_id`),
  `voice` (вслух, кроме тихого режима), `telegram` (сообщение владельцу бота); пусто — тихо. Вопрос в Telegram —
  с кнопками вариантов (`callback_data: "bq:<задача>:<вопрос>:<номер>"`; разрешение — «Разрешить» / «Отклонить»):
  ответ — кнопкой или ответом на сообщение текстом (сообщение → задача и вопрос — `kv telegram:task_questions`,
  последние 100); разрешение, нажатое или написанное, пока Jarvis был выключен, нужно дать заново.
- **Запуск из чата и голосом**: «в фоне: …», «фоном …», «поставь задачу: …», «фоновая задача: …» (`ai.ask`, без
  картинок) — сразу задача, в чат — просьба и «Принял — работаю в фоне: «…»» (`meta.background` — id задачи,
  голосом — «Принял, сэр. Работаю в фоне.»); модель в режиме «Всё вместе» сама ставит большое дело в фон
  инструментом `start_background_task {task, result?, repeat?}` (риск `act`); повторяющаяся — «Принял — буду
  делать каждый день в 20:00: «…». Первый раз — сейчас…».
- **Браузер агента** (`jarvis/ai/browser/agent_browser.py`, `ctx.agent_browser`): Chrome или Edge без окна
  (`--headless=new`, подпись — как у обычного Chrome, флаг автоматизации снят), профиль `<данные>/agent-browser`,
  своя вкладка у каждой задачи (`task_tabs`), скачивания запрещены. Запускается при первом сайте задачи или
  открытии панели, закрывается вместе с Jarvis и через 10 минут без задач и зрителей. Действия задачи
  (`jarvis/ai/browser/task_tools.py`): `browser_open, browser_click, browser_type, browser_scroll, browser_press,
  browser_wait, browser_read` (и `browser_screenshot` — модели с картинками); ответ каждого — снимок страницы
  (элементы `b1, b2…`). Разрешение (вопрос задачи): деньги, отправка и публикация — всегда; удаление — кроме
  «Спрашивать реже»; остальное рискованное — при «Спрашивать чаще» (тогда и ввод текста). Пароли, коды и данные
  карт задача не вводит (ответ модели — попросить человека), файлы не скачивает и не загружает. Вкладкой
  управляет человек (`held`) — действие ждёт и не выполняется: «Пока человек управлял страницей, ничего не
  сделано…». «Открыть окном» (`open_window`): тот же профиль обычным окном (`windowed`) — войти на сайт, который не
  пускает браузер без окна; браузер без окна на это время закрыт (вкладки задач тоже), действия задач ждут до 30 с,
  потом ответ модели «человек входит на сайт…»; окно закрыли — браузер снова без окна, а задача открывает
  страницу заново.

Настройки `background`: `vault`, `folder`, `read_vault`, `inbox` (`true`), `parallel` (1–4, 2), `autonomy` (`careful | normal | bold`),
`max_steps` (10–1000, 150), `max_hours` (0.25–48, 4), `notify_question`, `notify_done` (`["toast"]`).

| Маршрут | Тело | Ответ |
|---|---|---|
| `GET /api/background` | — | `{items: TaskCard[]}` — идущие первыми, потом повторяющиеся (ближайший запуск первым), потом новые |
| `POST /api/background` | `{brief, deliverable?, title?, source?: ui\|text\|voice\|remote\|vault, repeat?: Repeat\|null, run_now?: true}` | `{task: TaskCard}`; ИИ не настроен или расписание неверное — 400 |
| `POST /api/background/parse` | `{text}` | `{repeat, repeat_text, title}` — повтор в начале текста («каждый вечер …»): форма новой задачи показывает его до создания |
| `GET /api/background/vault?refresh=` | — | `{path, folder, exists, obsidian, auto, vaults: [{path, name, open, ts}]}` |
| `POST /api/background/vault/setup` | — | то же — после создания своей папки агента |
| `GET /api/background/{id}` | — | `{task: Task}` — целиком: `brief, deliverable, plan[], log[], questions[], sources[], summary, note, result, error, steps, worked_s, runs, usage, provider, model, repeat, next_run, cycle, history[], fails, tables[], card, note_link, result_link` (`obsidian://…`) |
| `POST /api/background/{id}/answer` | `{text, question_id?}` | `{task: TaskCard}`; вопроса нет — 400 |
| `POST /api/background/{id}/cancel` | — | `{task: TaskCard}` |
| `POST /api/background/{id}/retry` | — | `{task: TaskCard}` — снова в очередь, с тем же планом и ходом работы; повторяющаяся — новый запуск сейчас |
| `POST /api/background/{id}/pause` | — | `{task: TaskCard}` — повторы на паузе; не повторяется или идёт — 400 |
| `POST /api/background/{id}/resume` | — | `{task: TaskCard}` — снова по расписанию |
| `PUT /api/background/{id}/repeat` | `{repeat: Repeat\|null}` | `{task: TaskCard}` — сменить расписание (обычная законченная задача становится повторяющейся; `null` или `{}` — повторы сняты); идёт — 400 |
| `GET /api/background/{id}/tables` | — | `{items: [{path, name, columns, rows (последние 400, старые первыми), total, numeric: [номер колонки], link}]}` |
| `DELETE /api/background/{id}` | — | `{ok}` — из списка Jarvis (заметки в хранилище остаются) |
| `GET /api/agent-browser` | — | `{status: closed\|starting\|connected\|error, app, found, version, error, tasks: {задача: вкладка}, held: [вкладка], profile, windowed}` |
| `POST /api/agent-browser/close` | — | то же — браузер агента закрыт |
| `POST /api/agent-browser/window` | `{url?, task?}` | то же — открыт окном (адрес сайта `http(s)://…`, страница задачи `task` или страница «войдите в сайты»); Chrome и Edge нет или окно не открылось — 400 |
| `POST /api/agent-browser/window/close` | — | то же — окно закрыто (бережно, через `Browser.close`) |

WebSocket `/ws/agent-browser?token=&task=` — панель «Браузер агента»: смотрит вкладку задачи `task` (у задачи её
ещё нет — `target: ""`, браузер не запускается) или, без `task`, свою новую вкладку (`task: "manual"`, стартовая
страница со ссылками на входы популярных сайтов — войти на сайты; закрывается вместе с панелью). Страница
сменилась сама (ссылка, действие агента) — снова `state`; после каждого перехода показ кадров включается заново,
первый кадр — сразу снимком страницы. Браузер открыт окном — `state` с `windowed: true, target: ""` (кадров нет;
окно закрыли — панель шлёт `watch` заново). От ядра: `{type: "state", data: {target, task, url, title, held, status, error, windowed}}`, `{type:
"frame", data: {data: JPEG base64, metadata: {deviceWidth, deviceHeight, pageScaleFactor, offsetTop, scrollOffsetX,
scrollOffsetY, timestamp}}}` (только последние кадры), `{type: "error", data: {text}}`. От панели: `{type: "watch",
task?}`, `{type: "take"}` / `{type: "release"}`, ввод в страницу (координаты — CSS-точки, как в `metadata`):
`{type: "mouse", event: mousePressed|mouseReleased|mouseMoved, x, y, button, clickCount, modifiers}`, `{type:
"wheel", x, y, dx, dy}`, `{type: "key", event: keyDown|keyUp|rawKeyDown, key, code, keyCode, text, modifiers}`,
`{type: "text", text}`, `{type: "navigate", url}`, `{type: "history", back: bool}`, `{type: "reload"}`. Панель
закрылась — управление само возвращается агенту.

`TaskCard = {id, title, brief, status: queued|running|waiting|done|partial|failed|cancelled|scheduled|paused, source,
created, updated, finished, plan_done, plan_total, steps, progress: [{ts, text, kind}] (последние 3), question:
Question|null, summary, error, note, result, sources, repeat: Repeat|{}, repeat_text («каждый день в 20:00»),
next_run, cycle, last_run: {cycle, ts, status, summary, steps}|null, tables}`. `Question = {id, text, kind:
question|approval, options, detail, key, asked, answer, answered}`. `Repeat = {kind: "daily", at, weekdays} |
{kind: "hours", hours}`.

| Событие | Данные |
|---|---|
| `background.updated` | `{task: TaskCard}` — по ходу работы не чаще раза в секунду |
| `background.question` | `{task_id, question: Question}` |
| `background.done` | `{task_id, status, summary, error, note, result, next_run, paused}` — у повторяющейся — итог запуска (`status`), когда следующий и не ушла ли на паузу |
| `background.deleted` | `{id}` |
| `agent_browser.state` | как `GET /api/agent-browser` |

**Утверждение плана** (`settings.background.approve_plan`: `never` | `big` (по умолчанию) | `always`): первый план
задачи (`update_plan`) из 4+ шагов, план повторяющейся задачи или любой при `always` уходит человеку вопросом
`kind: "plan"` — «Утвердить план?» с шагами, варианты «Начинай» / «Отмени задачу», ответ словами — правка: модель
составляет новый план, и он снова на утверждение; утвердили — `task.plan_ok`, дальше отметки по ходу без вопросов
(ответ во время паузы тоже ставит `plan_ok`). В Telegram — с 📋.

**Поиск и чтение — параллельно.** Пачка вызовов модели только из `web_search`, `web_read`, `vault_search`,
`vault_read` идёт одновременно (`runner.PARALLEL_TOOLS`); вперемешку с записью — по очереди.

**Скачивание с разрешения.** `browser_click` по ссылке на файл (или Enter на ней) в браузере агента — вопрос
«Скачать файл с <сайт>: «<ссылка>»?» (`approval`, ключ `download:<сайт>:<ссылка>`); разрешили — браузер на время
этого щелчка скачивает в `<Загрузки>\Jarvis` (нет такой папки — `<данные>/downloads`; `agent_browser.download_folder`)
и снова запрещает скачивания; модель получает путь, файл не открывается и не запускается; в ход задачи —
«Скачан файл …». Загрузка файлов на сайты — по-прежнему только человеком.

`POST /api/background/{id}/notebooklm` → `{path, link, urls}` — «Для NotebookLM»: итог, текст главной заметки и все
источники задачи одним Markdown-файлом `Jarvis/NotebookLM/<задача>.md` (у NotebookLM нет открытого API для обычных
аккаунтов — файл загружает человек: «Добавить источник» → «Загрузить»; ссылки — ещё и списком в конце файла и в
буфер, для «Добавить источник» → «Сайт»); у задачи без итога и источников — 400.
