Прочитай AGENTS.md целиком — это спецификация. Это первая сессия: в репозитории только AGENTS.md, CLAUDE.md,
.gitignore, prompts/ и scripts/ (pyproject ещё нет — это нормально).

Старый проект: C:\Jarvis-old (и тег legacy-v2.2 в этом репозитории) — только читать, ничего там не менять и
не запускать. Возьми оттуда только проверенные правила защиты путей с тестами; архитектуру и абстракции НЕ переноси.

Задача: скелет проекта и первая часть src/pc. Если контекст на исходе — закончи пункты 1–4, закоммить и перечисли,
что осталось (пункты 5–6 тогда пойдут отдельной сессией).

1. Скелет:
   - pyproject.toml: `requires-python = ">=3.12,<3.13"`, `uv python pin 3.12` (.python-version — в репозиторий),
     backend hatchling с `[tool.hatch.build.targets.wheel] packages = ["src/jarvis", "src/pc"]`,
     `[tool.uv] environments = ["sys_platform == 'win32'"]`, скрипт `jarvis = "jarvis.cli:main"`, все зависимости
     из «Стек» (кроме голоса; openai-codex — ровно ==0.160.1), dev: pytest, ruff; маркер pytest `live`
     (по умолчанию `-m "not live"` не задавать — это делает команда); настройки ruff; uv.lock в репозитории.
     В .gitignore добавь `*.egg-info/`, `build/`, `dist/`, `.venv/` (если нет).
   - jarvis.example.toml — ровно как в AGENTS.md.
   - .github/workflows/ci.yml: на push и pull_request, runs-on windows-latest, `permissions: contents: read`;
     actions/checkout@v4, astral-sh/setup-uv@v6; `uv sync --locked`; затем три проверки из AGENTS.md. Секретов нет
     и не добавлять (всё, что требует сети, GPT, GPU или рабочего стола, — только live).
   - tests/conftest.py: autouse-фикстура JARVIS_DATA_DIR=tmp_path, JARVIS_CONFIG=<tmp_path>\jarvis.toml.
   - tests/test_hygiene.py (репозиторий публичный), по `git ls-files --cached --others --exclude-standard`
     (ровно то, что попадёт в коммит после `git add -A`): нет jarvis.toml, auth.json, .env, логов и журнала (*.log,
     journal/), бинарников и моделей (*.exe, *.gguf, *.onnx, *.bin); в текстах нет секретов — шаблоны с минимальной
     длиной тела (sk-[A-Za-z0-9_-]{20,}, ghp_[A-Za-z0-9]{36}, github_pat_[A-Za-z0-9_]{22,}, xox[abprs]-[A-Za-z0-9-]{10,},
     eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}; в prompts/ эти префиксы встречаются как примеры — их не ловить);
     нет имени текущего пользователя Windows (USERNAME, кроме me/runner/runneradmin) и путей `C:\Users\<не me>\`;
     пути окружения из AGENTS.md (C:\llama, C:\models, D:\Jarvis-model-cache, C:\Jarvis, C:\JarvisData) разрешены.
   - src/pc/settings.py: data_dir() (JARVIS_DATA_DIR или %LOCALAPPDATA%\Jarvis), config_path() (JARVIS_CONFIG или
     C:\Jarvis\jarvis.toml), load_pc_settings() — секции [pc] и [aliases] (tomllib → dataclass с дефолтами; файла нет —
     дефолты); перечитывать при смене mtime файла.
   - src/jarvis/config.py: остальные секции (mode, [ui], [hands], [brain], [journal]) → dataclass'ы с дефолтами;
     пути — из pc.settings.
   - src/jarvis/log.py: технический лог через logging (логгер "jarvis") в <data>\logs\jarvis.log
     (RotatingFileHandler, UTF-8) + stderr; файл не открылся — только stderr, без падения.
   - src/pc/subproc.py: единый помощник запуска внешних процессов — всегда creationflags=CREATE_NO_WINDOW (никогда
     DETACHED_PROCESS: с ним PowerShell 5.1 падает на смене кодировки), stdin=DEVNULL, таймаут, вывод — байты,
     декодирование явно.
   - src/jarvis/cli.py: argparse с подкомандами run, ask, route, apps, bench, stats, doctor, autostart; сейчас рабочая
     только apps, остальные печатают «будет в сессии N». main() первым делом: для sys.stdout/sys.stderr, если они
     не None и не консоль (not isatty()), — reconfigure(encoding="utf-8", errors="replace"). Тест: вывод в pipe
     с «ё», «⚙», «✓» не падает и читается как UTF-8.
2. src/pc/result.py: Result(ok, text, data), Caller.
3. src/pc/paths.py — по разделу «Пути, цели и недоверенные данные»: canonical(), is_within() по частям,
   check(path, caller, op) с причиной отказа, включая закрытые для brain зоны и типы файлов (секреты; исполняемые
   типы для open — «спросить»). Тесты: регистр, `..`, смешанные слеши, ADS, UNC, \\?\, Program Files vs
   Program Files (x86), ловушка C:\Users\me vs C:\Users\meow, private_paths, зоны brain; 8.3-имена (если
   GetShortPathNameW вернул тот же путь — pytest.skip); junction — обычный тест (создавай в tmp_path через
   `cmd /c mklink /J`, права администратора не нужны; symlink без прав — skip). Сравнивай пути, канонизированные
   с обеих сторон: на раннере CI %TEMP% = C:\Users\RUNNER~1\….
4. src/pc/policy.py — таблица риска из AGENTS.md как данные: action → решение для user и brain
   ∈ {allow, confirm, deny}; check(action, caller); неизвестное действие — исключение. Счётчик для brain:
   не больше 5 действий без подтверждения за 60 с, дальше — confirm. Тест на каждую строку таблицы и на счётчик.
5. src/pc/apps.py — инвентарь приложений:
   - источник: Get-StartApps через Windows PowerShell 5.1 и pc.subproc, ровно такой argv (проверено: без смены
     кодировки PowerShell пишет в pipe в OEM 866, а Python по умолчанию декодирует как 1251 — кириллица ломается):
     `[r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
       "-Command", "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); ConvertTo-Json -Compress
       -InputObject @(Get-StartApps | Select-Object Name,AppID)"]`, timeout=30;
     `json.loads(stdout.decode("utf-8-sig").strip() or "[]")` (`@(...)` — чтобы одно приложение тоже дало массив);
     отбросить деинсталляторы и справку (uninstall, удалить, readme, help и т.п.);
   - имена, которые человек говорит вслух, могут отличаться от Name: добавь локализованные отображаемые имена
     из `Shell.Application` → `NameSpace("shell:AppsFolder")` (pywin32), если отличаются;
   - AppID бывает разной формы (`Пакет_xxx!App`, `{GUID}\prog.exe`, прочее) — хранить как есть;
   - кэш <data>\apps.json: при старте читается мгновенно; обновляет его только процесс `jarvis run` (в фоне при
     старте, раз в сутки и при промахе resolve — не чаще раза в 10 мин, затем повтор resolve) и
     `jarvis apps --refresh`; pc.mcp и прочие команды CLI только читают; запись атомарно (tmp + os.replace);
   - resolve(name) → (app, score): алиас из [aliases] → rapidfuzz по нормализованным именам с транслитерацией
     и разговорными формами (телеграм/телега→Telegram, хром→Chrome, ворд→Word, вскод/вс код→Visual Studio Code,
     параметры/настройки→Settings, проводник→Explorer); порог уверенности — константа, подбери на тестах (ориентир 85);
   - launch(app) → os.startfile("shell:AppsFolder\\" + AppID) из потока с инициализированным COM (STA):
     PID не возвращается, OSError = неудача (сообщение пользователю), успехом считать отсутствие исключения;
   - тесты resolve: фикстура tests/fixtures/apps.json (~40 реалистичных приложений: Telegram Desktop, Google Chrome,
     Visual Studio Code, Steam, Discord, Проводник, Блокнот, Параметры, Word, Excel, OBS Studio…) и ~60 фраз,
     включая опечатки и отрицательные случаи (несуществующее приложение — score ниже порога). Эта фикстура —
     общий инвентарь для тестов S2–S7.
6. `uv run jarvis apps [--refresh] [запрос]`: без запроса — инвентарь; с запросом — топ-5 со score и временем resolve.

Ограничения: никакого UI и кода уровней 0–2; в сеть ходит только uv.
Готово, когда: проверки из AGENTS.md зелёные; resolve на фикстуре ≥95 % верных; resolve <5 мс (после прогрева —
медиана 200 вызовов; при переменной окружения CI порог ×3).
Проверка руками (для меня): `uv sync`; `uv run jarvis apps --refresh`; `uv run jarvis apps телега`;
`uv run jarvis apps ворд`; `uv run jarvis apps фотошоп` (если его нет — score ниже порога); после пуша — зелёный CI.
