# Ночная сессия: Jarvis целиком — код, окно, exe

Ты — агент-разработчик в облачном Linux-контейнере. За эту ночь построй проект Jarvis целиком по спецификации
и собери устанавливаемый exe для Windows. Утром владелец поставит его на свой ПК (Windows 11) и проверит.
Меня (владельца) не жди: вопросов посреди работы не задавай. Однозначное решай сам и записывай в журнал,
неоднозначное помечай TODO и иди дальше. Пиши мне по-русски.

## 1. Где что лежит

- **Спецификация и задание 00 — `stonebridgeway/Jarvis`** (публичный, у тебя только чтение). `main` = `66dc61d`
  «docs: apply task 00 findings (data dir, hands)». Ниже `44a68c5` — комплект, тег `legacy-v2.2` = `5b9fe79`,
  это старый код (только справочник). Задание 00 (переезд, конфиги, замеры) выполнено там. В дереве лежат:
  `AGENTS.md` (спецификация), `prompts/FULL-PROMPT.md` (этапы S1–S9), `prompts/00…09`, `scripts/`
  (`start_hands.cmd`, `hands_probe.ps1` + `.json`, `brain_smoke.py`, `es_check.py`).
- **Рабочий репозиторий — `BreezeAreaSay/Jarvis-Agent`** (push есть). Его `main` — старый проект `5b9fe79`,
  история общая с stonebridgeway. Работай в новой ветке `jarvis-v1` от `66dc61d`:
  ```
  git clone --depth 50 https://github.com/BreezeAreaSay/Jarvis-Agent jarvis && cd jarvis
  git fetch --depth 50 https://github.com/stonebridgeway/Jarvis main
  git checkout -b jarvis-v1 FETCH_HEAD          # git log -1 → 66dc61d
  git push -u origin jarvis-v1
  ```
  `main` в Jarvis-Agent не трогай. Историю не переписывай, без force-push: утром ветка уйдёт в
  stonebridgeway/Jarvis быстрой перемоткой от `66dc61d`.
- **Источник правды.** В технике (контракты, политика риска, пути, стек, конфиг, публичный репозиторий) главнее
  `AGENTS.md`; порядок этапов задаёт `prompts/FULL-PROMPT.md`. Поправки этого файла главнее обоих: облако вместо
  ПК владельца, плюс exe и проработанное окно. **До первой строки кода прочитай `AGENTS.md` и `FULL-PROMPT.md`
  целиком.**

## 2. Факты с ПК владельца (задание 00, 10.10.2026)

- Windows 11, AMD Radeon RX 7600 8 GB, обычный пользователь без прав администратора. В имени профиля кириллица.
- **Каталог данных Jarvis — `C:\JarvisData`** (переменная пользователя `JARVIS_DATA_DIR`). С CODEX_HOME в профиле
  с кириллицей SDK 0.160.1 не заработал. Вход мозга лежит в `C:\JarvisData\codex-home`.
- **llama.cpp b11498** (Vulkan) в `C:\llama`. Модели: `C:\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf`,
  `D:\Jarvis-model-cache\Qwen3-8B-Q4_K_M.gguf`. `scripts\start_hands.cmd 4b|8b` — флаги не менять.
- **Руки — 4B.** Замер 16 фраз × 2: 4B — 11/16, p50 352 мс, tg 73–75 т/с; 8B — 12/16, p50 565 мс, сама отвечает
  на вопросы. `prompt_n` 10–24, `cache_n` 1183: кэш префикса работает. p95 портит одна фраза «привет как дела» —
  обрезка по max_tokens (128 токенов, 2 с). Разбор ошибок и что с ними делать — в S3 FULL-PROMPT (блок
  «Что показал замер 00»).
- **Codex.** Модели аккаунта — `gpt-6-luna` (быстрая) и `gpt-6.1-sol` (основная), совпадают с умолчаниями.
  CLI владельца — 0.162.0-alpha, а мозг Jarvis использует свой codex.exe из SDK 0.160.1. Тёплые ответы
  1,2–1,9 с до первых слов, первый — 6,3 с; тот замер шёл с `~/.codex` владельца и его MCP-серверами.
- **Everything 1.4.1.1032 + ES 1.1.0.38** (`C:\Jarvis\bin\es.exe`), службы нет, но весь C: в индексе
  (`C:\Windows` — 359 тыс. объектов). Кириллица с `-cp 65001` работает.
- Ollama выключена (автозапуск снят, `OLLAMA_KEEP_ALIVE=0`).

## 3. Облако вместо ПК: что меняется в FULL-PROMPT

FULL-PROMPT написан для агента на ПК владельца. У тебя Linux без Windows, GPU, llama-server, Everything и входа
в ChatGPT. Поэтому:

1. **Код пиши под Windows, проверяй на Linux.** Каждый модуль с Win32/COM/реестром держит вызовы ОС за
   приватным тонким слоем (`_api` — объект или набор функций). Тесты подменяют этот слой фейком и идут на любой ОС.
   Импорт `win32*`, `pycaw`, `comtypes`, `ctypes.windll`, `winreg` — только лениво или под
   `sys.platform == "win32"`, чтобы `import` модулей работал на Linux. Тесты, которым нужен настоящий Windows
   (junction, 8.3, реестр), помечай `skipif(sys.platform != "win32")`: они пройдут в CI.
2. **Настоящая проверка на Windows — GitHub Actions** (`windows-latest`) в Jarvis-Agent. После каждого этапа:
   push → смотри прогон через GitHub MCP (`actions_list`, `get_job_logs`) → чини до зелёного. Красный CI на
   ветке — это работа сейчас, а не «потом».
3. **uv.** `[tool.uv] environments = ["sys_platform == 'win32'", "sys_platform == 'linux'"]` (в S1 спецификации
   только win32 — расширь, чтобы `uv sync` работал и в облаке). Windows-зависимости — с маркером
   `; sys_platform == 'win32'` (pywin32, pycaw, comtypes). `uv.lock` в репозитории, CI — `uv sync --locked`.
4. **Пробных запросов рукам нет** (S3 «Сервер рук запущен — прогони 25 фраз…» пропусти). Выводы замера 00 из
   S3 всё равно внедри: короткий reply/clarify с `maxLength` 80, open↔focus, правило kill, слово темы для
   media/vol/win, примеры. Доводка по живому замеру — утром.
5. **Live-тесты, «Проверка руками» и «Мне на потом»** — в `docs/manual-checks.md`, как требует FULL-PROMPT.
   Добавь туда проверку установщика (раздел 5).
6. **Песочницы Codex у тебя нет** — пункты FULL-PROMPT про повышенные права, `writable_roots` и подтверждение
   каждого коммита к тебе не относятся. Коммить и пушь сам.
7. **Что я проверил в этом облаке:**
   - PyPI доступен, а `github.com/.../releases`, `api.github.com` (через curl) и huggingface — 403 или недоступны.
     Не выдумывай URL моделей и релизов, которые не можешь проверить.
   - `openai-codex==0.160.1` ставится. Исходники SDK читай в `.venv` (`openai_codex/api.py`, `client.py`, `types.py`).
     Бинарь — пакет `codex_cli_bin` (`bundled_codex_path()`); у `CodexConfig` есть поле `codex_bin`.
   - **mcp 2.x переименовал FastMCP в `mcp.server.mcpserver.MCPServer`**, а `mcp.server.fastmcp` при импорте
     падает. Закрепи `mcp>=1.29,<2` (1.30.0 работает, FastMCP как в спецификации).
   - Ставь `PySide6-Essentials`, не полный PySide6: exe будет меньше. Офскрин-рендер Qt работает после
     `apt-get install -y libegl1` и `QT_QPA_PLATFORM=offscreen`.
   - Python 3.12: `uv venv --python 3.12`.
   - Не запускай python из каталога распакованного пакета: модуль `types.py` в openai_codex перекрывает стандартный.
8. **Этап 0 FULL-PROMPT в облаке:**
   - вместо ветки `main` — `jarvis-v1`;
   - файлов `scratch\` (`codex-models.txt`, `hands-*.txt`, `brain-smoke.txt`) здесь нет — их содержание в разделе 2;
   - `127.0.0.1:8081` не опрашивай;
   - pwsh в облаке, скорее всего, нет: `.ps1` и `.cmd` проверяет CI на Windows (добавь туда шаг, который хотя бы
     разбирает `.ps1` через `[System.Management.Automation.Language.Parser]::ParseFile`).

## 4. Архитектура и контракты (обязательные решения)

Структура — как в AGENTS.md «Структура». Дополнительно:
- `src/jarvis/events.py` — общие события запроса. Порядок: `Level` → (`Status` | `TextChunk` | `Items`)* →
  ровно один `Done` последним. `Level(level ∈ grammar|hands|brain|local, reason, model)`,
  `Status(text, kind ∈ info|tool|warn, done, ok)`, `TextChunk(text)`, `Items(items)` для результатов find,
  `Done(ok, text, level, reason, autohide, cancelled, timings)`. Их отдают `core.handle` и `Brain.ask`, а показывает
  окно. CLI печатает их же.
- `pc.result`: `Caller = Literal["user","brain"]`, `Result(ok, text, data)`, помощники `ok()` и `fail()`.
- `pc.settings`:
  - `data_dir()` — `JARVIS_DATA_DIR`, иначе `%LOCALAPPDATA%\Jarvis`;
  - `config_path()` — `JARVIS_CONFIG`, иначе `C:\Jarvis\jarvis.toml`; у exe без папки `C:\Jarvis` — `<data>\jarvis.toml`;
  - `app_root()` — репозиторий, а в exe — `sys._MEIPASS`: там `scripts/`, `bench/`, `jarvis.example.toml`;
  - `install_dir()`, `is_frozen()`;
  - `load_toml()` — кэш по mtime, общий с `jarvis.config`;
  - `es_path()` — из конфига, иначе `bin\es.exe` рядом с приложением.
- `pc.subproc.run/spawn` — единственная точка запуска процессов (CREATE_NO_WINDOW, stdin=DEVNULL, байты).
- `pc.policy`:
  - имена действий = строки таблицы AGENTS.md: find_files, list_windows, list_apps, list_processes, volume_get,
    open_app, open_folder, focus, window, volume, media, open_file, open_executable, open_url, close_window,
    clipboard_get, clipboard_set, lock, read_text, type_text, kill_process, trash, power, shell, write_file,
    delete_permanent, jarvis_self;
  - `check()` — чистая таблица, неизвестное действие — исключение;
  - `decide()` — таблица плюс бюджет мозга: 5 изменяющих действий за 60 с, чтение не считается;
  - `require(action, caller, summary, details) -> Result | None` — None значит «можно»; иначе отказ или
    подтверждение через `confirm_client`.
- `pc.paths`: логика сравнения — на `PureWindowsPath` и не зависит от ОС (тестируется на Linux). ОС-зависимое
  (ссылки, junction, 8.3) — только в `_resolve()`. Мозгу закрыт `data_dir()`, где бы он ни лежал.
- **Единственный `os.startfile`** — `files.open_target(target, kind, caller)`. Приложение запускается только через
  `shell:AppsFolder\<AppID>` из инвентаря (`apps.launch` в STA-потоке).
- **Высокоуровневые функции pc принимают строку цели и caller** и сами делают policy, подтверждение и privacy:
  `windows.focus_target/close_target/window_action/list_windows_result`, `files.find/open_target/trash/read_text`,
  `audio.volume`, `media.media`, `procs.processes/kill`, `system.lock/power/clipboard_get/clipboard_set/type_text`.
  `pc.mcp` (18 инструментов) — тонкая обёртка над ними с caller="brain". `jarvis.execute` — над ними
  с caller="user".
- `jarvis.context.Context`: `active_window`, `last_object`, `last_kind`, `last_hwnd`, `found`, `last_ts`.
  `cur_target()` — цель `"@cur"`: свежий последний объект (hwnd или имя), иначе окно на момент хоткея.
  TTL 5 минут. CLI хранит контекст в `<data>\cli_context.json`.
- **Словарь инструментов общий** для грамматики, рук и execute: open, close, focus, win, find, vol, media, kill,
  open_found, lock, clock, reply, clarify, ask_gpt.
- **Подтверждения** — одна очередь в окне для двух источников: обработчик в процессе (`set_confirm_handler`)
  и запросы из pipe (`ConfirmServer`). Рабочий поток ждёт `threading.Event` не дольше 60 с.
- **Мозг**: `Brain(cfg, confirm_address, process_hook)`, методы `start()` (в фоне), `close()`,
  `ask(text, ctx, deep)` (генератор событий), `cancel()`, `new_conversation()`. Overrides — ровно из S5.
  Прокладка `subprocess` в `openai_codex.client` — до первого `Codex()`.
- Уровни 0–2 и CLI работают без Qt. PySide6 импортируют только `app.py` и `ui/`.

## 5. Exe и установщик (новое требование владельца)

- **PyInstaller, onedir** (onefile распаковывается при каждом старте — медленно). Один spec, общий COLLECT, два exe:
  - `Jarvis.exe` — windowed; без аргументов это `jarvis run`;
  - `jarvis-cli.exe` — консольный: весь CLI и скрытая подкоманда `mcp`, сервер pc для мозга.
  Имена `Jarvis.exe` и `jarvis.exe` на Windows совпадают — поэтому второй называется `jarvis-cli.exe`.
- **Frozen-режим:**
  - MCP-сервер мозга: `<install>\jarvis-cli.exe mcp`, cwd — папка установки (в разработке — `python.exe -m pc.mcp`,
    НЕ pythonw);
  - `CodexConfig(codex_bin=…)` — бинарь, собранный из `codex_cli_bin` (`collect_all`, проверь
    `bundled_codex_path()` в собранном виде);
  - в бандле: `scripts/start_hands.cmd`, `jarvis.example.toml`, `bench/phrases.ru.jsonl`; `bin/es.exe` — только если
    он лежит в рабочем дереве при сборке (у владельца он в `C:\Jarvis\bin`; в git его нет);
  - автозапуск exe: `HKCU\…\Run` → `"<install>\Jarvis.exe"`; в разработке — как в S6a.
- **Установщик — Inno Setup.** Per-user, без прав администратора (`PrivilegesRequired=lowest`), в
  `{localappdata}\Programs\Jarvis`. Ярлык в «Пуск», флажок «Запускать при входе в Windows», деинсталлятор.
  Процессы Jarvis закрываются перед установкой и удалением (`CloseApplications` или `taskkill` своих exe).
  Каталог данных при удалении не трогать. На выходе — `Jarvis-Setup-<версия>.exe`.
- **Иконка** — рисуется кодом (QPainter) и сохраняется в `.ico` при сборке; тот же рисунок в трее (состояния
  ниже). Бинарники в git не класть.
- **`jarvis-cli.exe selftest`** — проверка собранного бандла без сети и GPU: импорт всех модулей; конфиг; создание
  окна в скрытом режиме и рендер в PNG; запуск `mcp` как подпроцесса со списком 18 инструментов через клиент mcp;
  `codex --version` бинаря из SDK; наличие файлов бандла. Код выхода 0 или 1 и печать по пунктам.
- **CI:**
  - `.github/workflows/ci.yml` (push/PR, windows-latest): `uv sync --locked`, ruff check, ruff format --check,
    `pytest -q -m "not live"`;
  - `.github/workflows/build.yml` (push в `jarvis-v1`, теги `v*`, workflow_dispatch): тесты → PyInstaller →
    `jarvis-cli.exe selftest` на раннере → Inno Setup (`choco install innosetup -y`, если нет `iscc`) →
    artifact «Jarvis-Setup» (установщик + zip папки onedir).
  - `scripts/build.ps1` — та же сборка локально на ПК владельца.
- **Бюджеты exe:** старт до значка в трее ≤1,5 с на тёплом диске; хоткей → окно ≤100 мс; размер папки
  — без лишних модулей Qt (исключи QtWebEngine, Qml, 3D, Multimedia и т. п.). Запиши фактический размер.

## 6. Окно и анимации (фронт; владелец просит проработать)

Лаунчер в стиле Spotlight или Raycast: красиво, плавно, мгновенно.
- **Форма.** Без рамки, поверх всех, по центру сверху, ширина `ui.width` (720). Тёмная тема, скругления. Тень и
  скругления Windows 11 через `DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE)`. Опционально
  `ui.backdrop = "acrylic"` (`DWMWA_SYSTEMBACKDROP_TYPE`) с надёжным откатом на сплошной фон; по умолчанию сплошной.
  Шрифт — Segoe UI Variable, затем Segoe UI.
- **Анимации** (`QPropertyAnimation` и `QVariantAnimation`, OutCubic):
  - появление — прозрачность 0→1 и сдвиг на 8 px вниз за 140 мс, при этом фокус ввода сразу;
  - скрытие — 90 мс;
  - высота окна при росте ответа меняется плавно, 120 мс;
  - индикатор работы — бегущий градиент под строкой ввода, только пока идёт запрос;
  - бейдж уровня (грамматика, руки, GPT, локально) — свой цвет и плавная смена;
  - строки «⚙ …» — спиннер, затем ✓ или ✗;
  - потоковый текст мозга;
  - результаты find — нумерованный список с подсветкой при наведении; клик или «цифра+Enter» открывает;
  - карточка подтверждения — акцентная рамка, кнопки «Нет» (по умолчанию) и «Да (Ctrl+Enter)». Кнопки
    проявляются после 700 мс защиты, полоска отсчёта 60 с; пометка «просит GPT»;
  - ошибки — тост внутри окна.
- **Покой.** Без запроса ни одной работающей анимации и ни одного таймера: 0 % CPU. `ui.animations = false`
  отключает всё.
- **Клавиши.** Enter — отправить; Esc — отменить запрос, второй Esc — скрыть; ↑ и ↓ — история; цифра+Enter.
  Подтверждение: Enter и Esc = «нет», Ctrl+Enter = «да».
- **Безопасность текста.** Все недоверенные строки — только `PlainText`: заголовки окон, имена файлов, ответ мозга,
  summary и details из pipe.
- **Трей.**
  - Иконка, нарисованная кодом, четыре состояния: готов, занят, локальный режим, предупреждение. Подсказка —
    готовность компонентов.
  - Меню из S6a: локальный режим, новый разговор, перезапустить руки, выгрузить руки, журнал, автозапуск
    (флажок), выход.
- **Логика без Qt** — в `ui/logic.py`, с unit-тестами: история, цифра+Enter, очередь подтверждений, правило
  700 мс, Enter не подтверждает, автоскрытие.
- **Проверка глазами.** Скрипт `scripts/ui_screenshots.py` рендерит офскрин все состояния:
  - пусто;
  - ввод;
  - работа рук;
  - поток мозга;
  - список find;
  - подтверждение;
  - ошибка;
  - локальный режим.
  Посмотри PNG сам и исправь вёрстку. Лучшие снимки приложи к итоговому сообщению. В git не коммить —
  положи в `docs/screenshots/` под .gitignore или пришли файлами.

## 7. Скорость (главное требование владельца)

Бюджеты AGENTS.md:
- грамматика: p95 ≤0,3 с;
- руки: p95 ≤1,2 с;
- мозг: p95 ≤3 с до первых слов, и в новом треде тоже;
- хоткей → окно: ≤100 мс.

Отдельно следи:
- байты префикса рук неизменны: тест на две разные команды;
- один `httpx.Client` на процесс;
- инвентарь приложений — из кэша; PowerShell не вызывается на каждую команду;
- импорты тяжёлого (`PySide6`, `openai_codex`) — не в обработчиках;
- UI-поток не блокируется: модели и действия — в рабочих потоках, связь — сигналы Qt;
- окно создаётся при старте, дальше только show/hide;
- прогрев рук и запасной тред мозга запускаются при старте в фоне.

## 8. Как работать ночью

- **Порядок** — этапы FULL-PROMPT S1 → S2a → S2b → S3 → S4 → S5 → S6a → S6b → S7 → S8. Затем раздел 5 (exe)
  и раздел 6 (доводка окна). S9 (голос) — только если всё остальное готово и CI зелёный. Голос — опциональной
  группой зависимостей `voice`. Имена и URL моделей sherpa-onnx бери только проверяемые (PyPI, исходники пакета);
  не проверить — TODO(live) в manual-checks.
- **Журнал и коммиты** — по правилам FULL-PROMPT: «Состояние» и «Журнал сессии» в AGENTS.md,
  `docs/manual-checks.md`, коммит `feat(<область>): … (Sx)`. После каждого этапа — `git push`: если сессия
  оборвётся, работа не пропадёт. Перед пушем — три проверки из AGENTS.md.
- **Параллельность.** Если у тебя есть субагенты или workflow, реализуй независимые модули параллельно с
  раздельным владением файлами: pc-ядро (paths, policy, privacy), apps и windows, files и procs, audio, media
  и system, confirm и mcp, руки и execute, грамматика, роутер и корпус, мозг, core, журнал и CLI, окно, упаковка.
  Сначала каркас и контракты раздела 4. Коммиты и `git` — только из главного потока.
- **Ревью перед финалом** — S8 FULL-PROMPT и независимые проверки по направлениям:
  - безопасность и политика;
  - корректность Win32 и COM — сигнатуры, argtypes/restype, 64-битные HANDLE;
  - потоки Qt;
  - скорость и кэш префикса;
  - frozen-режим и установщик;
  - соответствие AGENTS.md.
  Каждую находку проверяй тестом, который до исправления падал.
- **Если оборвалось** — новая сессия начнёт словами «Прочитай AGENTS.md (Состояние, Журнал сессии) в ветке
  jarvis-v1 репозитория BreezeAreaSay/Jarvis-Agent и продолжи по NIGHT-PROMPT». Положи этот файл в репозиторий
  как `prompts/NIGHT-PROMPT.md` первым коммитом.

## 9. К утру

1. Ветка `jarvis-v1` в Jarvis-Agent: все этапы закрыты или явно помечены в «Состоянии», CI зелёный на Windows.
2. Артефакт сборки в Actions: `Jarvis-Setup-<версия>.exe` и zip папки onedir; selftest на раннере прошёл.
3. `docs/manual-checks.md` полный — порядок из «Финала сессии» FULL-PROMPT. В начале добавь:
   - установка из `Jarvis-Setup.exe`;
   - первый запуск (значок в трее, хоткей);
   - `jarvis-cli doctor`;
   - вход мозга, если `doctor` скажет «Not logged in»;
   - сервер рук.
4. `README.md`: что это, установка, первый запуск, хоткей, конфиг, сборка из исходников.
5. Итоговое сообщение мне:
   - таблица этапов (этап, статус, коммиты);
   - ссылка на прогон CI и на артефакт;
   - скриншоты окна;
   - что не проверено (все TODO(live));
   - что мне сделать утром и что прислать (выводы live-тестов, `bench --live`, `doctor`);
   - известные риски.
6. Как перенести в stonebridgeway — дай мне эти команды в итоговом сообщении:
   ```
   git -C C:\Jarvis fetch https://github.com/BreezeAreaSay/Jarvis-Agent jarvis-v1
   git -C C:\Jarvis merge --ff-only FETCH_HEAD
   git -C C:\Jarvis push origin main
   ```

## 10. Нельзя

- Пушить в `main` Jarvis-Agent, переписывать историю ветки, force-push.
- Класть в git бинарники (exe, модели, es.exe), `jarvis.toml`, журналы, логи, токены, а также личные данные
  владельца: имя профиля, e-mail, реальные пути и заголовки окон. Оба репозитория публичные.
  `tests/test_hygiene.py` следит за этим. На Linux переменной `USERNAME` может не быть — тест не должен падать.
- Выдумывать API библиотек: читай исходники в `.venv` (openai_codex, mcp, pycaw — его исходник можно
  скачать с PyPI, PySide6).
- Ослаблять политику риска, переводить мозгу shell, менять `sandbox=read_only` и `approval_mode=deny_all`.
- Пропускать, отключать или ослаблять тесты, чтобы CI позеленел.
