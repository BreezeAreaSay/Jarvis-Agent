# Архитектура и контракты модулей

Документ — общий договор между модулями. Технические правила (политика риска, пути, скорость) — в AGENTS.md;
здесь — точные имена и сигнатуры. Меняя сигнатуру, поправь этот файл в том же коммите.

## Общие правила кода

- Python 3.12, типы везде, ruff (line-length 110). Идентификаторы по-английски; комментарии, докстринги и тексты
  для человека — по-русски. Короткие функции, без портов/адаптеров/фабрик.
- `pc` не импортирует `jarvis`. Уровни 0–2 и CLI работают без Qt: PySide6 импортируют только `jarvis/app.py`
  и `jarvis/ui/`; исключение — `jarvis.vision.encode_jpeg` (QtGui.QImage внутри функции, только на пути зрения).
  `openai_codex` импортирует только `jarvis/brain.py` (лениво, в `start()`).
- Каждый модуль с Win32/COM/реестром держит вызовы ОС за приватным тонким слоем `_api` (объект с методами).
  Его создаёт функция `api()` при первом вызове. Тесты подменяют `module._api` фейком: `monkeypatch.setattr(mod,
  "_api", Fake())`. Импорт `win32*`, `pycaw`, `comtypes`, `ctypes.windll`, `winreg` — только внутри реализации
  `_api` (лениво) или под `sys.platform == "win32"`, чтобы `import pc.<модуль>` работал на Linux.
- Тесты, которым нужен настоящий Windows (junction, 8.3, реестр, окна), — `@pytest.mark.skipif(sys.platform !=
  "win32", reason=...)`: они идут в CI на windows-latest. Тесты, которым нужен рабочий стол владельца, GPU,
  Everything, сеть или вход в GPT, — `@pytest.mark.live`.
- Внешние процессы — только `pc.subproc.run/spawn`.
- ctypes: `argtypes`/`restype` объявлять явно; HANDLE/HWND — 64-битные (`wintypes.HANDLE`, `c_void_p`); `LRESULT` —
  `c_ssize_t`; проверять возвращаемые значения.

## pc — действия Windows

### pc.result
`Caller = Literal["user", "brain"]`; `Result(ok: bool, text: str, data: Any = None)`; `ok(text, data=None)`,
`fail(text, data=None)`.

### pc.settings
`data_dir()`, `data_file(*parts)` (создаёт родителя), `config_path()`, `app_root()`, `install_dir()`, `is_frozen()`,
`load_toml()` (кэш по mtime, общий с jarvis.config), `config_error()`, `load_pc_settings() -> PcSettings(es_path,
private_paths, aliases)` (ключи aliases — casefold), `es_path()`.

### pc.subproc
`run(argv, timeout, cwd=None, env=None) -> Completed(returncode, stdout: bytes, stderr: bytes)`;
`spawn(argv, cwd=None, env=None, stdout=DEVNULL, stderr=DEVNULL) -> Popen`. Всегда CREATE_NO_WINDOW, stdin=DEVNULL.

### pc.policy
Имена действий = строки таблицы AGENTS.md: find_files, list_windows, list_apps, list_processes, volume_get,
open_app, open_folder, focus, window, volume, media, open_file, open_executable, open_url, close_window,
clipboard_get, clipboard_set, lock, screenshot, read_text, type_text, kill_process, trash, power, shell,
write_file, delete_permanent, jarvis_self. screenshot — user «сразу», brain «нет» (у мозга инструмента снимка нет).
- `check(action, caller) -> "allow"|"confirm"|"deny"` — чистая таблица; неизвестное — `UnknownAction`.
- `decide(action, caller, now=None)` — плюс бюджет мозга: 5 изменяющих действий за 60 с (чтение не считается).
- `require(action, caller, summary, details="") -> Result | None` — None = можно; иначе отказ. «Спросить» —
  через `confirm_client.confirm(summary, details, caller)`.
- `reset_budget()` — для тестов.

### pc.confirm_client
- `set_confirm_handler(handler: Callable[[str, str, Caller], bool] | None)`.
- `confirm(summary, details, caller) -> bool`: обработчик процесса; иначе pipe из `JARVIS_CONFIRM_PIPE`; иначе False.
  Через pipe: summary > 1000 или details > 16000 символов — отказ (без обрезки).
- `ConfirmServer(callback: Callable[[str, str, Caller], bool])`: `.start()` (нет/повреждён ключ — RuntimeError),
  `.address`, `.close()`. Запросы из pipe всегда от мозга (caller="brain", в окне — «просит GPT»). Второй сервер
  в том же процессе — адрес `…-<PID>-<n>`.
- `load_key(create=False)`, `key_path()`, `pipe_address(pid=None)`; `TIMEOUT_S`, `PIPE_ENV`.

### pc.paths
- `canonical(path: str) -> str` — абсолютный путь Windows: нормализация (`..`, слеши), `_resolve()` (ссылки,
  junction, 8.3 → длинные имена). Ошибка — `PathDenied(reason)`.
- `is_within(path, root) -> bool` — по частям, без учёта регистра, на `PureWindowsPath` (не зависит от ОС).
- `check(path, caller, op, is_dir=None) -> PathCheck(ok, path, reason, confirm)`; `op ∈ "open" | "read" | "trash" |
  "list"`; `is_dir` (None — спросить ОС): для папки confirm не ставится.
  Отказ всем: UNC, `\\?\`, `\\.\`, ADS, системные зоны, private_paths. Для brain ещё: %APPDATA%, %LOCALAPPDATA%,
  `data_dir()`, `~\.*`, секретные файлы. `confirm=True` — исполняемый/«активный» тип при op="open".
- `is_executable_type(path) -> bool`, `is_secret_file(path) -> bool`, `hidden_for_brain(path) -> bool`.
- ОС-зависимое — только `_resolve(path) -> str` (на Linux — без изменений; тесты подменяют).

### pc.privacy
`redact_title(title) -> str` (≤80 символов; заголовок с именем из private_paths — «(скрыто)»),
`redact_path(path) -> str | None` (None — путь скрыт от мозга), `redact_text(text) -> str` (пути закрытых зон
и private_paths в свободном тексте → «(скрыто)»), `redact(value) -> value` (рекурсивно по dict/list/str).

### pc.apps
- `App(name: str, app_id: str)`; `THRESHOLD` (≈85).
- `inventory() -> list[App]` — из памяти, при первом вызове из кэша `<data>\apps.json` (мгновенно).
- `refresh() -> list[App]` — Get-StartApps (PowerShell 5.1 через subproc) + отображаемые имена Shell.Application;
  атомарная запись кэша. Вызывают только `jarvis run` (фон) и `jarvis apps --refresh`.
- `enable_auto_refresh(True)` — включает обновление при промахе resolve (не чаще раза в 10 мин; только `jarvis run`).
- `resolve(name) -> tuple[App | None, float]` — алиас → rapidfuzz по нормализованным именам; ниже порога — (None, score).
- `top(name, n=5) -> list[tuple[App, float]]`.
- `list_apps_result(query, caller) -> Result` (data: без query — `[{"name"}]`, с query — топ-5 `[{"name","score"}]`).
- `run_sta(fn, *args) -> Any` — выполнить в выделенном потоке с CoInitialize (STA).
- `set_inventory(apps: list[App | dict] | None) -> None` — подменить инвентарь (тесты, `jarvis bench`); None — снова кэш.
- `lookup(name)` — как resolve, без автообновления (windows, procs, execute). Помощники: `normalize`, `latin`, `core`,
  `name_variants(text) -> set[str]`, `app_exe(app) -> str | None`, `cache_path()`; `RefreshError` — refresh не удался,
  кэш цел.

### pc.windows
- `WindowInfo(hwnd: int, title: str, pid: int, exe: str)`; exe — имя файла (`chrome.exe`).
- `list_windows() -> list[WindowInfo]` — видимые top-level с заголовком, без окон Jarvis, cloaked и tool-окон.
- `foreground() -> WindowInfo | None` (окна Jarvis не исключаются — для «снять активное окно» до показа).
- `find_window(target: str | int) -> WindowInfo | None` — hwnd, exe, заголовок или имя приложения.
- `focus(hwnd) -> Result` — лестница из S2a.
- `focus_target(target, caller) -> Result`, `close_target(target, caller) -> Result`,
  `window_action(action: "minimize"|"maximize"|"restore"|"minimize_all", target: str|int|None, caller) -> Result`,
  `list_windows_result(caller) -> Result` (data: список dict hwnd/title/exe; для brain — через privacy).

### pc.procs
- `ProcGroup(exe: str, count: int, pids: list[int])`; `find_processes(name: str | None) -> list[ProcGroup]`.
- `own_pids() -> set[int]` — сам Jarvis и его дерево: корень из `JARVIS_ROOT_PID` (передаёт brain.py в env MCP)
  или текущий процесс; все потомки корня; свои предки до корня.
- `processes(name, caller) -> Result`, `kill(name, caller) -> Result` (policy kill_process; свои — jarvis_self).

### pc.files
- `find(query, kind="any", caller="user", limit=20) -> Result` (data: список путей; для brain — через privacy;
  пути, которые `paths.check` запрещает, отбрасываются).
- `known_folder(name) -> str | None` — загрузки, документы, рабочий стол, изображения, видео, музыка.
- `open_target(target, kind: "app"|"folder"|"file"|"url"|None, caller) -> Result` — ЕДИНСТВЕННЫЙ `os.startfile`
  (внутри `_api.startfile`). Приложение — только `shell:AppsFolder\<AppID>` из инвентаря (через `apps.run_sta`).
- `trash(path, caller) -> Result`, `read_text(path, caller, max_bytes=65536) -> Result`.

### pc._input
`send_key(vk, up=False) -> bool`, `send_text(text) -> int`, `text_inputs(text) -> list[INPUT]` — SendInput (KEYEVENTF_UNICODE, суррогатные пары,
перевод строки — VK_RETURN). Общий для windows.focus (отпускание Alt), media и system.type_text.

### pc.audio / pc.media / pc.system
- `audio.volume_get(caller) -> Result` (data: {"level": int, "muted": bool}); `audio.warmup()` — фоновый прогрев
  потока звука и импорта pycaw при старте `jarvis run`;
  `audio.volume(set=None, delta=None, mute=None, caller="user") -> Result`. Вся работа с COM — в одном потоке.
- `media.media(action: "play_pause"|"next"|"prev", caller) -> Result`.
- `system.lock(caller)`, `system.power(action: "sleep"|"shutdown"|"restart", caller)`, `system.clipboard_get(caller)`,
  `system.clipboard_set(text, caller)`, `system.type_text(text, target: str|int, caller)` — все `-> Result`.

### pc.screen
Снимок окна или экрана для GPT — только по команде человека (вызывает jarvis.core с caller="user").
- `Shot(width, height, bgra: bytes)` — frozen, в памяти: строки сверху вниз, 4 байта на точку B, G, R, A (альфа не
  используется). Размер — не больше `MAX_SIDE` = 8192 по стороне (лишнее обрезается).
- `capture_window(hwnd, caller, deny_apps=()) -> Shot | Result` — PrintWindow(PW_RENDERFULLCONTENT): окно целиком,
  даже перекрытое окном Jarvis. `capture_screen(hwnd | None, caller, deny_apps=()) -> Shot | Result` — BitBlt
  (SRCCOPY | CAPTUREBLT) монитора, на котором окно hwnd (нет окна — основного). Result — отказ или ошибка с фразой.
- Первым — `policy.require("screenshot", caller, …)` (мозгу — отказ). Отказ всегда: окно Jarvis (policy jarvis_self),
  менеджеры паролей (`PASSWORD_MANAGERS` по exe, `PASSWORD_TITLES` по заголовку), exe из `deny_apps` (jarvis передаёт
  `[vision] deny_apps`), имя из private_paths в заголовке, нечитаемый конфиг; окно неизвестной программы, свёрнутое
  или зависшее; чёрный снимок (`blank(shot)`). Экран — отказ, если такое окно активно или видно на этом мониторе
  (`windows.list_windows()`, не свёрнуто, пересекает монитор).
- `app_key(exe)` («KeePass.exe» → «keepass»), `denied_apps(extra) -> frozenset[str]`, `clamp(rect)`.
- `_api` (`_Api(fns)`; fns — таблица функций user32/gdi32 с argtypes, тесты подставляют фейк): `is_window`,
  `is_iconic`, `is_hung`, `pid_of`, `exe_of`, `title_of` (через pc.windows), `window_rect`, `monitor_rect`,
  `grab_window`, `grab_rect`. DC, DIB-секция (biHeight < 0) и выбор объекта освобождаются в finally. DPI: на время
  снимка поток — в DPI-режиме самого окна (GetWindowDpiAwarenessContext), для экрана — per-monitor v2
  (SetThreadDpiAwarenessContext), потом — как было: координаты в физических точках и в `jarvis run` (Qt 6 ставит
  процессу per-monitor v2), и в `jarvis ask` (процесс без DPI-осведомлённости).

### pc.mcp
FastMCP stdio, `python -m pc.mcp` (в exe — `jarvis-cli.exe mcp`), caller="brain". 18 инструментов: list_windows,
list_apps, find_files, open, focus, close, window, volume, media, processes, kill_process, clipboard_get,
clipboard_set, read_text_file, move_to_trash, type_text, lock, power. `TOOL_ACTIONS: dict[str, list[str]]` —
инструмент → строки policy; `TOOLS: dict[str, Callable]` — имя → функция инструмента; `build_server() -> FastMCP`
(mcp импортируется только здесь); `main()` — запуск сервера по stdio. Инструмента снимка окна или экрана нет
(тест test_s8_policy_mapping).

## jarvis — приложение

### jarvis.events
`Level(level, reason, model="")`, `Status(text, kind="info", done=False, ok=True, key="")`, `TextChunk(text)`,
`Items(items)`, `Done(ok, text="", level, reason, autohide, cancelled, timings)`. Порядок: Level → (Status |
TextChunk | Items)* → ровно один Done.

### jarvis.config
`load() -> Config(mode, ui: UiConfig, hands: HandsConfig, brain: BrainConfig, journal: JournalConfig,
vision: VisionConfig)`. `VisionConfig(enabled=True, max_side=1600, jpeg_quality=80, hide_jarvis=True,
deny_apps=[])`: max_side вне 640..2560 и jpeg_quality вне 50..95 — умолчания.
`UiConfig(hotkey, autohide_s, width, backdrop, animations, orb="always", orb_size=64)`: orb — `"always" | "auto" |
"off"` (иное — "always"), orb_size ограничен `ORB_SIZE_MIN..ORB_SIZE_MAX` (40..160); backdrop вне solid|acrylic — solid.
`Config.voice: VoiceConfig(ptt_key="right ctrl", min_hold_ms=250, max_record_s=30.0, device="", model="gigaam-v3",
threads=2, vad=True, tts="voice", tts_voice="ruslan", tts_speed=1.0, tts_pitch=1.0, tts_effect=0.35)`: ptt_key "" —
голосовой ввод выключен, неразборчивая клавиша — умолчание; model вне `VOICE_MODELS` — "gigaam-v3"; tts —
`"voice" | "always" | "off"` (true/false из старых конфигов → "voice"/"off"); числа вне пределов (50..2000 мс,
2..120 с, 1..8, 0,5..2,0, pitch 0,8..1,2, effect 0..1) — умолчания; tts_voice — `[a-z0-9_-]+`.
`BrainConfig`: кроме моделей и уровней размышления — `persona="jarvis"` (`"jarvis" | "plain"`, иное — "jarvis")
и `address="сэр"` (обращение: одна строка ≤30 знаков без `[]`; пусто — "сэр").

### jarvis.context
`Context(active_window: WindowInfo | None, last_object: str | None, last_kind: str | None, last_hwnd: int | None,
found: list[str], last_ts: float)`; `TTL_S = 300`; `cur_target(now=None) -> str | int | None` (цель "@cur":
свежий последний объект — hwnd или имя, иначе окно на момент хоткея); `remember(obj, kind, hwnd=None)`;
`set_found(paths)`; `fresh(now=None)`; `load_cli()`/`save_cli()` — `<data>\cli_context.json`.

### Словарь инструментов (грамматика, руки, execute)
open{target, kind?}, close{target}, focus{target}, win{action, target?}, find{query, kind?}, vol{set?|delta?|mute?},
media{action}, kill{name}, open_found{index}, lock{}, clock{what: "time"|"date"}, reply{text}, clarify{question},
ask_gpt{}. open_found, lock и clock — только у грамматики.

### jarvis.hands
`HandsDecision(kind: "tool"|"error", tool: str, args: dict, reason: str, timings: dict)`; `HandsError`;
`Hands(cfg, job_hook=None, *, transport=None)`: `.status` (server, prefix_tokens, prefix_cache, tps, vram, error,
model), `ensure_server()` (HandsError), `warmup()`, `prefix_tokens()`, `decide(text, ctx) -> HandsDecision`, `stop()`,
`close()`. `SYSTEM`, `TOOLS` — константы; `prefix_bytes()`, `build_body(user, max_tokens)`, `user_message(text, ctx)`,
`parse_response(resp)`. execute: цель рук «из текста» — partial_ratio ≥ 80 или то же приложение инвентаря, что
и 1–3 слова команды (`apps.lookup`); `from_text(value, text, kind)`.

### jarvis.execute
`Outcome(kind: "done"|"reply"|"clarify"|"ask_gpt", ok: bool, text: str, items: list[str], autohide: bool,
action: str)`; `run(tool, args, text, ctx, source: "grammar"|"hands", dry=False) -> Outcome`.

### jarvis.grammar / jarvis.router
`GrammarHit(action, args, confidence)`; `match(text, ctx) -> GrammarHit | None`.
`Route(level: "grammar"|"hands"|"brain"|"local", reason, text, deep=False, hit=None, local_only=False,
vision="")`; `route(text, ctx, mode) -> Route` (снимает префиксы «локально:», «gpt:», «думай:»). Сразу после
префиксов — `vision.match`: зрение → level "brain", reason "vision:window" | "vision:screen", text — вопрос,
`vision` — что снимать (с «думай:» — deep); без облака — "local:needs_gpt" (снимка нет).
Цели грамматики: приложение — `App.name` из инвентаря (kind="app"), папка — имя для `files.known_folder`
(kind="folder"), «его/это/окно» — "@cur" (только если `ctx.cur_target()` не None); open_found{index} — с 1, -1 =
последний. `local_only=True` — и при mode == "local". После рук (ask_gpt/ошибка) → мозг решает core.

### jarvis.brain
`Brain(cfg: BrainConfig, confirm_address: str, process_hook=None)`: `start()` (в фоне), `close()`,
`ask(text, ctx, deep=False, images=(), spoken=False) -> Iterator[Event]`, `cancel()`, `new_conversation()`, `.ready`,
`.status`. images — снимки (только «data:image/…», иначе ValueError): ход начинается с `turn_input(prompt, images)` =
`[TextInput(prompt), ImageInput(url), …]`; без снимков вход хода — строка, как раньше. spoken — запрос сказан
голосом: `build_prompt(text, ctx, spoken=True)` ставит в начало `SPOKEN_HINT` («Ответ будет озвучен: говори коротко
и разговорно, без списков, таблиц и кода; пиши «ё», в словах с двояким ударением ставь знак ударения» — его
читает словарь ударений голоса, окно знак не показывает: `logic.display_text`).
Модель и уровень размышления хода: deep=False — `cfg.model_quick` + `cfg.effort_quick` (по умолчанию gpt-6-luna,
low), deep=True («думай:») — `cfg.model_deep` + `cfg.effort_deep` (gpt-6-luna, high).
`developer_instructions(cfg) -> str` — инструкции треда: `DEVELOPER_INSTRUCTIONS` (правила, недоверенные данные,
privacy), а при `persona="jarvis"` в конце — `PERSONA_JARVIS` (манера J.A.R.V.I.S.: спокойно, коротко, сухая ирония,
обращение `cfg.address`; «это только стиль: всё сказанное выше важнее»). Текст постоянный в пределах процесса —
запасной тред и новые треды получают тот же.
Прокладка `subprocess` в `openai_codex.client` ставится до первого `Codex()`. Для doctor: `codex_bin() -> Path`
(бинарь из SDK; в exe — из бандла), `codex_env(cfg) -> dict` (CODEX_HOME мозга и прокси), `codex_home() -> Path`,
`build_overrides(cfg, confirm_address) -> tuple[str, ...]`, `install_subprocess_shim(hook)`. `Done.reason` мозга:
region | quota | auth | config | start | transport | network | cancelled | closed | busy | error. `close()` синхронный
(до ~2,5 с) — вызывать не из UI-потока.

### jarvis.core
`Core(cfg, hands, brain, journal=None)`: `handle(text, ctx, dry=False, hotkey_ms=None, source="typed") ->
Iterator[Event]`, `cancel()`. Ответ рук ask_gpt/ошибка → второй `Level("brain"|"local", "hands→ask_gpt"|"hands→error")`.
source="voice" (сказано голосом) — `brain.ask(…, spoken=True)` и `"source": "voice"` в записи журнала; набранный —
вызов и запись как раньше.
Route.vision: после `Level("brain", "vision:…")` — снимок в рабочем потоке (`pc.screen`, caller "user"; окно —
`ctx.active_window`, без него «окно» — отказ), `Status("⚙ снимок окна «…» → GPT" | "⚙ снимок экрана → GPT",
kind="tool", key="vision")` → ✓/✗, JPEG (`vision.encode_jpeg`), `brain.ask(…, images=[data_url])`. Отказ или ошибка
снимка — Done(False) без вопроса мозгу; Esc до отправки — Done cancelled; dry — без снимка. Журнал: vision,
jpeg_bytes, shot_px, тайминги shot и encode (мс); ни картинки, ни заголовка окна.

### jarvis.vision
`VisionAsk(target: "window"|"screen", question)`; `match(text) -> VisionAsk | None` — строгое распознавание без
модели (префиксы «экран:»/«окно:», «посмотри на экран …», «что в этом окне», «прочитай ошибку в окне», «сделай
скриншот и …»); дешёвая предпроверка подстрок «экран», «окн», «скрин», «монитор». Вопрос без служебных слов;
пусто — `DEFAULT_QUESTION`. `status_label(target, window)` — строка «⚙ снимок …» (заголовок — одной строкой, без
управляющих символов, ≤50). `encode_jpeg(shot, max_side=1600, quality=80) -> bytes` — QImage Format_RGB32 (альфа
GDI не учитывается), SmoothTransformation, меньший снимок не увеличивается; QtGui импортируется внутри функции,
работает без QApplication и вне GUI-потока. `data_url(jpeg) -> str`. Тексты: `NO_WINDOW_TEXT`, `DISABLED_TEXT`.

### Голос (S9): jarvis.ptt, jarvis.mic, jarvis.asr, jarvis.voice, jarvis.tts (без Qt; всё локально)
- ptt — `parse_key(name) -> int` (VK: «right ctrl», «правый ctrl», «f9», «0xA3»; Ctrl/Shift/Alt/Win без стороны —
  ValueError), `key_label(vk) -> str`. `Hook(vk, events: SimpleQueue)`: `start()` (свой поток "jarvis-ptt" с GetMessage,
  WH_KEYBOARD_LL; не встал — `PttError`), `stop()`, `key_down()` (GetAsyncKeyState), `release()`, `.down`. Колбэк
  `_hook_proc` — только событие ("down" | "up" | "other" | "esc" | "resume", time.monotonic()) и CallNextHookEx;
  "esc" — Esc в любом окне (глушит речь, даже если окно Jarvis не в фокусе); клавиши не глушатся; объект WINFUNCTYPE — `ptt._proc` на уровне модуля; переустановка каждые `REHOOK_S` (новый хук раньше
  снятия старого) и после паузы таймера > `RESUME_GAP_S` (сон ПК → событие "resume"). Слой ОС — `ptt._api`.
- mic — `Recorder(device="", max_s=30, on_level=None)`: `start()` (MicError), `stop() -> Recording(pcm: bytes,
  rate=16000)`, `abort()`, `.full`; RawInputStream 16 кГц моно int16, блоки 50 мс, `on_level(0..1)` на каждый блок
  из потока PortAudio; устройство без 16 кГц — его частота (asr передискретизирует). `level(block)`,
  `find_device(name, kind)`, `device_name(index, kind)`, `warmup()` (импорт sounddevice в фоне), `api()` — слой
  sounddevice (`mic._api`; нет PortAudio — MicError). numpy не нужен.
- asr — `MODELS` (gigaam-v3 | gigaam-v3-punct | gigaam-v2 | vosk-small → папка `<data>\models\<имя>`),
  `model_files(name)` (AsrError с подсказкой get_voice_models.ps1), `vad_path()` (`models\vad\silero_vad.onnx`),
  `Recognizer(model, threads, vad)`: `load()` (секунды — в фоне; прогрев), `recognize(Recording) -> AsrResult(text,
  raw, asr_ms, audio_s, speech_s, vad_ms)`; VAD кормится окнами по 512 (весь буфер одним вызовом sherpa-onnx 1.13.8
  разбирает неверно), края речи + запас 0,25/0,35 с; речи нет — пустой текст без декодирования.
  `normalize(raw)` — как набранная команда: без обращения «Джарвис» (нечётко, `strip_wake`), без точки в конце,
  строчная первая буква; «?» остаётся.
- voice — `Voice(cfg: VoiceConfig, emit, *, on_press=None, on_escape=None, journal=None, recognizer=None,
  recorder=None, hook_factory=None)`: `start()` (потоки "jarvis-voice" и "jarvis-asr", модель грузится в фоне, хук — сразу; не встал —
  `status="error"`), `stop()`, `cancel()` (Esc), `.status` (off | loading | ready | error), `.state`, `.error`.
  События в `emit` (из рабочих потоков): `Listening()` (PTT ≥ min_hold без других клавиш — окно «Слушаю…»),
  `Loudness(value)`, `Recognizing()`, `Heard(text, asr_ms, audio_s, model)`, `Failed(text)`,
  `ListenCancelled(reason: keys | esc | resume | stop)`. Запись идёт с нажатия; короткое нажатие и сочетание
  (правый Ctrl+C) молча выбрасываются; новое нажатие во время распознавания — старый результат не нужен;
  потерянное отпускание — по 3 опросам GetAsyncKeyState; `on_press()` и `on_escape()` (Esc в любом окне) — глушат
  речь (Narrator.cancel); Esc ещё и бросает запись или распознавание.
  Журнал: `{"kind": "voice", asr_ms, vad_ms, release_to_text_ms, wait_ms, audio_s, speech_s, empty, model, threads,
  text}`. `enabled(cfg)` — ptt_key задан и модель скачана.
- tts — `Speaker(voice="ruslan", speed=1.0, threads=2, pitch=1.0, effect=0.0)`: `load()` (TtsError), `say(text) ->
  gen` (не блокирует; синтез следующей фразы, пока звучит текущая), `stop() -> bool` (глушит сразу, новое
  поколение), `.speaking`, `.first_sound`, `on_idle(gen)`, `on_level(0..1)` (громкость блока перед выводом —
  `block_level`, 0 — замолчал; поток вывода); вывод — `mic.api().open_output`, блоки ~46 мс. pitch (0,8..1,2):
  синтез со скоростью speed/pitch, вывод на частоте ×pitch — длительность та же, тон ниже/выше; effect (0..1) —
  `voicefx.electronic` после синтеза. `pick_voice(wanted)` — wanted, если скачан, иначе первый скачанный.
  Синтез — `generate(text, GenerationConfig)` (`generation_config(so, speed)`: sid 0, `SILENCE_SCALE` = 1.0 —
  паузы на запятых не сжимаются). Словарь ударений: `jarvis_dict()` — `app_root()/build/voice/ru_dict` (собирает
  scripts/build_ru_dict.py; в exe — из бандла) или None; `install_dict(espeak_dir, on) -> "jarvis" | "espeak"` —
  до загрузки модели ставит его в espeak-ng-data голоса (исходный — `ru_dict.orig`, запись через tmp + replace),
  off или не собран — возвращает исходный; `Speaker(…, use_dict=True)`, `.dict` после load().
  `Narrator(speaker, mode, journal, address="")`: `begin(rid, spoken)`, `close()`, `on_event(rid, ev)` (мозг — весь
  ответ по предложениям через `Splitter`, не больше `SPEAK_LIMIT`, код/таблица — `REST`; пока мозг думает — тишина,
  работу показывает сфера; грамматика и руки — `addressed(short_text(Done.text), ok, address)`; cancelled —
  тишина), `confirm(summary)`, `say(text)` (напоминания), `cancel()`, `.active`, `on_unsaid()` (один раз на запрос:
  у мозга код/таблица/лимит, итог грамматики/рук длиннее `SHORT_LIMIT` — часть ответа не прозвучит).
  `addressed(text, ok, address, lead=False)` — обращение только в озвучке: успех — «Открыл Telegram, сэр.»,
  ошибка, подтверждение и напоминание (lead) — «Сэр, …»; address "" — как есть. Обращение берётся из
  `[brain] address` при `persona="jarvis"`.
  Журнал: `{"kind": "tts", tts_ms, reply_ms, spoken, interrupted, level, chars, phrases, voice}`. `speakable(text)` —
  без markdown, ссылок, путей, расширений; латиница по-русски (`transliterate`).
- voicefx — `electronic(samples, rate, amount) -> list[float]` (без numpy): два прямых эха 4,5 и 11 мс (гребёнка,
  «металл»), кольцевая модуляция 50 Гц, ФВЧ первого порядка (до 150 Гц), нормализация пика `PEAK`; amount 0 —
  копия без изменений, >1 — как 1. Фраза 3 с при 22 кГц — десятки миллисекунд.

### jarvis.journal
`Journal()`: `write(record: dict)` (фоновый поток), `close()`; модульные `read(days) -> list[dict]`,
`stats(days, records=None)`, `format_stats(st, min_tokens_per_s)`, `percentile`, `fmt_ms`. CLI журнал не пишет.
Записи с полем `kind` ("voice", "tts") — не запросы: в `stats` — разделы `voice` (asr p50/p95, средняя длина
записи, доля «не расслышал») и `tts` (до первого звука p50/p95, доля перебитых), бюджеты «голос: распознавание»
≤400 мс и «озвучка: до первого звука» ≤300 мс.

### jarvis.cli
`main(argv=None) -> int`. Подкоманды: run (`jarvis.app.main`), ask, route, apps, bench (`jarvis.bench`),
stats (`jarvis.journal`), doctor (`jarvis.doctor`), autostart (`jarvis.winapp`), selftest (`jarvis.selftest.main`);
скрытая `mcp` (`pc.mcp.main` — сервер pc для мозга). Тяжёлое импортируется внутри подкоманды.
В exe: `Jarvis.exe` (windowed; без аргументов — `run`), `jarvis-cli.exe` (консольный, весь CLI).

### jarvis.winapp (без Qt)
Job Object (`create_job()`, `add_to_job(job, popen|pid)`, `job_hook(job)`, `close_job(job)`, `own_descendants(exe)`),
mutex одного экземпляра (`acquire_instance() -> int|None`, `release_instance(h)`, `signal_first_instance()` — окно-
приёмник `RECEIVER_TITLE` + сообщение `show_message_id()`), автозапуск — ярлык Jarvis.lnk в «Автозагрузке» (`set_autostart(on) -> str`,
`autostart_enabled() -> bool` — ярлык или старое значение HKCU\…\Run, `autostart_target()`, `autostart_command() -> str`), хоткей (`parse_hotkey(s) -> (mods|MOD_NOREPEAT, vk)`,
`format_hotkey(s)`, `register_with_fallback(hwnd, id, s) -> HotkeyResult(combo, message)`, `FALLBACK_HOTKEYS`),
`allow_set_foreground_any()`, `set_foreground(hwnd)`, `available()`, `exclude_from_capture(hwnd) -> bool`
(SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+: окно не попадает в снимки экрана; ошибка —
False и лог; app.py вызывает для окна-лаунчера и сферы при `[vision] hide_jarvis`). DWM — в jarvis/ui/dwm.py.
Полноэкранные окна (сфера на рабочем столе прячется): `watch_foreground(callback) -> ForegroundWatch | None` —
SetWinEventHook(EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS), `callback(hwnd)` приходит
в цикле сообщений UI-потока, без опроса; `ForegroundWatch(handle, proc)` держит ссылку на колбэк ctypes;
`unwatch_foreground(watch)` — UnhookWinEvent; `is_fullscreen(hwnd) -> bool` — GetWindowRect закрывает весь монитор
(MonitorFromWindow + GetMonitorInfoW), классы `SHELL_CLASSES` (Progman, WorkerW, Shell_TrayWnd) — нет;
`foreground_fullscreen()` — для старта. Окно, ставшее полноэкранным без смены переднего (F11 в браузере), хук не видит.

### jarvis.app и jarvis.ui
`app.main(argv=None) -> int` — `jarvis run`. Сфера: app создаёт одну `OrbModel(animations=ui.animations)`, отдаёт её
окну (`LauncherWindow(ui, orb=model)`) и, если `[ui] orb != "off"`, сфере на рабочем столе (`OrbWindow`, создаётся
при старте после трея, дальше только show/hide; меню — пункты трея без «Открыть»; `winapp.exclude_from_capture` при
`[vision] hide_jarvis`). Клик по сфере — как хоткей (окно активно и запроса нет — спрятать, иначе показать, без
«окна до Jarvis»). Хук `winapp.watch_foreground` → `OrbWindow.set_suppressed(is_fullscreen(hwnd))`; выход — снять
хук, `close_orb()`.
Голос (S9): в фоне после Core — `_start_voice()`: `tts.Narrator` (голос — `tts.pick_voice`, `[voice] tts != "off"`;
модель грузится в потоке "jarvis-tts-load"; `Speaker.on_level` → сигнал `speech_level(float)` → `OrbModel.speech`,
`Narrator.on_unsaid` → `_on_unsaid`) и `voice.Voice` (`voice.enabled(cfg.voice)`; `on_press` — `Narrator.cancel`,
`on_escape` — `_escape_anywhere`: речь смолкает, сигнал `voice_escape` → `LauncherWindow.dismiss_inactive()`).
Сигнал `voice_event(object)` (из потоков голоса, QueuedConnection) → `_on_voice_event`: `Listening` — активное окно
снимается ДО показа (как по хоткею). Окно спрятано и ответ прозвучит (`_speaks()`: Narrator готов, tts != "off",
сфера на столе есть) — голос «без окна»: `OrbModel.listen(True)`, окно не показывается; иначе окно показывается
БЕЗ активации (WA_ShowWithoutActivating: вдруг это правый Ctrl+C — C должна попасть в программу человека),
`voice_state("listen")`. `Loudness` → `voice_level`; `Recognizing` → `voice_state("recognize")` (без окна — ничего);
`Heard` → `listen(False)`, открытые подтверждения — «нет» (`deny_confirms`: голосом «да» не подтверждается) и
`start_request(text, source="voice", quiet=…)`; `Failed` → без окна: `OrbModel.fail()` и `Narrator.say(text)`,
иначе тост и `voice_dismiss()`; `ListenCancelled` → `listen(False)` или `voice_dismiss(0)`.
`start_request(text, source="typed", quiet=False)` → `Narrator.begin(rid, spoken=source == "voice")`; события
запроса → окно (спрятанное тоже копит ответ: он виден при следующем показе) и `Narrator.on_event`. Запрос без окна
показывает окно сам, без активации (`_reveal`), только ради списка (`Items`), того, что не прозвучит (`on_unsaid`:
код, таблица, длинный ответ) или подтверждения; после Done такое окно — `linger(REVEAL_LINGER_S)` (20 с).
Подтверждение → `Narrator.confirm(summary)`; Esc в окне → `Narrator.cancel()`; напоминания — `Narrator.say` (при
tts != "off"); выход — `Voice.stop()`, `Narrator.close()`.
- ui/logic.py — логика окна без Qt: `History`, `ConfirmRequest(summary, details, caller)` с `resolve(approved)` и
  `wait(timeout=60) -> bool`, `ConfirmQueue` (одна очередь для обработчика процесса и pipe; правило 700 мс;
  Enter/Esc = «нет», Ctrl+Enter = «да»), разбор «цифра+Enter», автоскрытие. `display_text(text)` — недоверенная
  строка для показа: без управляющих символов, подмены направления и знаков ударения после русских гласных.
  `OrbModel(animations=True)` — модель сферы без Qt: состояния idle | wake | listen | work | speak | confirm |
  success | error; входы `wake()` (показ окна), `key()`, `voice(rms)` (уровень голоса 0..1 — импульс), `begin()`,
  `set_level(level)` (оттенок grammar | hands | brain | local), `status()`, `chunk()`, `confirm(on)`,
  `done(ok, cancelled=False)`, `hide()` (идёт запрос — работа продолжается, вспышка догорает), `set_local(on)`;
  голос без окна: `listen(on)` (пробуждение и покой listen без показа окна), `speech(level)` (Jarvis говорит:
  импульс в такт, 0 — замолчал), `fail()` (малиновая вспышка «не расслышал»);
  `tick(dt) -> bool` (False — успокоилась, кадр застыл), `advance(now)` (общие часы для нескольких виджетов),
  `frame(t=None) -> OrbFrame` (детерминированно, без random: доли внутри сферы, аура и искры — в 1 + `ORB_EXTENT`),
  `subscribe(fn)`/`unsubscribe(fn)` (зовёт после входов и смены состояния в tick), `.animating`, `.resting` (окно
  спрятано; запроса, вспышки, слушания и речи нет), `.shown`, `.listening`, `.talking`. `AutoHide.arm_now()` —
  взвести без Done (окно, показанное само).
- ui/orb.py — `paint_orb(painter, rect, model, t=None)` (QPainter, кэш картинок тела, ободка и ореола; аура — один
  контур в маленьком буфере), `orb_radius(side)`, `orb_side(orb_size)`; `OrbWidget(model=None, parent=None, *, size)`
  — таймер ~60 к/с только пока `model.animating` и виджет виден, затем последний кадр и стоп; `OrbWindow(model,
  ui_cfg, *, state=None, writer=None)` (наследует OrbWidget): Frameless | Tool | StaysOnTop |
  WindowDoesNotAcceptFocus, WA_ShowWithoutActivating, прозрачный фон; сигнал `clicked`; `start()`,
  `set_menu_actions(actions)`, `menu()`, `set_suppressed(on)`, `hide_by_user()` (до перезапуска), `close_orb()`,
  `place(state)`, `default_center()` (правый нижний угол доступной области основного экрана, отступ
  `ORB_MARGIN_PX`), `center()`. Режимы: always — видна всегда; auto — видна, пока окно показано или идёт запрос,
  гаснет через `ORB_LINGER_MS` (2 с). Перетаскивание — `writer({"orb": {"x", "y"}})` (по умолчанию
  `save_state_async`: `<data>\ui_state.json`, tmp + os.replace в рабочем потоке); `load_state()`, `save_state(update)`,
  `saved_center(state)`, `state_path()`. Позиция вне всех экранов — угол по умолчанию.
- ui/window.py — `LauncherWindow(ui_cfg, parent=None, *, orb: OrbModel | None = None)`: сигналы `submitted(str)`,
  `cancel_requested()`, `open_item(int)` (индекс в Items: клик или цифра+Enter), `hidden()`; методы
  `show_launcher()`, `hide_launcher()`, `begin_request(text)`, `on_event(ev)` (события из jarvis.events, вызывается
  в UI-потоке), `ask_confirm(req)`, `set_local_mode(on)`, `show_toast(text, kind="error")`; `.orb_model` — модель
  сферы слева от строки ввода (`theme.ORB_PX` поверх места `theme.ORB_SLOT`), окно ведёт её своими событиями.
  Голос: `voice_state(phase)` ("listen" — строка пустая, подсказка «Слушаю…», сфера просыпается; "recognize" —
  «Распознаю…»; "" — обычная подсказка), `voice_level(value)` (→ `OrbModel.voice`), `voice_dismiss(delay_ms=2500)`
  (окно, которое человек не трогал — не активно, без запроса, подтверждения и текста, — прячется через delay_ms),
  `linger(seconds)` (окно, показанное само, прячется через seconds; мышь над ним — ждёт; клик или клавиша —
  остаётся), `dismiss_inactive()` (Esc в другой программе: окно без фокуса прячется; подтверждение — нет).
  Клик мимо прячет окно: потеря фокуса без запроса — всегда; посреди запроса — только с нажатой кнопкой мыши
  (`.mouse_down`, app ставит `winapp.mouse_button_down`): запрос продолжается, как после Esc ×2, а программа,
  открытая по команде и вышедшая вперёд, окно с ответом не прячет. Окно видно без фокуса — клик мимо ловится
  опросом кнопок мыши (`OUTSIDE_POLL_MS` = 50 мс, таймер — только пока окно такое). Открытое подтверждение и меню
  окна остаются.
- ui/icon.py — `render_icon(state: "ready"|"busy"|"local"|"warn", size) -> QImage` (рисунок QPainter; тот же
  рисунок — в трее и в .ico при сборке); ui/tray.py — `Tray`: сигналы `open_requested`, `local_toggled(bool)`,
  `new_conversation`, `restart_hands`, `unload_hands`, `open_logs`, `autostart_toggled(bool)`, `quit_requested`;
  `menu() -> QMenu` (его пункты показывает и сфера).

### jarvis.selftest
`main() -> int` — проверка собранного бандла без сети и GPU (раздел 5 NIGHT-PROMPT); пункт «голос: sherpa-onnx и
звук» — sherpa-onnx с нативными библиотеками, sounddevice с PortAudio (на Windows обязателен) и
`scripts/get_voice_models.ps1` в бандле.
