# Проверка руками и «Мне на потом»

Всё, что сессия не могла проверить без настоящего Windows, рабочего стола, GPU, Everything, сети и входа в GPT.
Запускает только владелец на своём ПК. В репозиторий из вывода переносить только числа (см. «Публичный
репозиторий» в AGENTS.md).

Обозначения: `jarvis-cli` — установленный exe (`& "$env:LOCALAPPDATA\Programs\Jarvis\jarvis-cli.exe" …`);
`uv run jarvis …` — то же из исходников в C:\Jarvis. Каталог данных — C:\JarvisData (`JARVIS_DATA_DIR`).

## Порядок для владельца

<!-- ORDER -->

## Разделы по этапам

### S1 — скелет, settings, paths, policy, apps
1. `uv sync`; `uv run jarvis apps --refresh` → число приложений; `uv run jarvis apps телега`, `uv run jarvis apps ворд`
   → нужное приложение первым со score ≥ порога; `uv run jarvis apps фотошоп` (если его нет) → score ниже порога.
   Прислать: число приложений, время resolve, score (без списка своих приложений).
2. Windows-тесты путей (junction, 8.3, настоящие зоны): `uv run pytest -q tests\test_paths.py -k win_` → проходят
   (8.3 и symlink могут быть skipped). Прислать итоговую строку.
3. `.venv\Scripts\python -c "from pc import paths; print(paths.check(r'C:\PROGRA~1\x', 'user', 'open'))"` →
   ok=False (папка программ).
4. Зоны от ОС без переменных: в PowerShell `$env:APPDATA=''; $env:LOCALAPPDATA=''; $env:USERPROFILE=''` и
   `.venv\Scripts\python -c "import os; from pc import paths; print(paths.check(os.path.expanduser('~') + r'\.ssh\x', 'brain', 'read'))"`
   → ok=False.
5. Подключённый сетевой диск (если есть): `paths.check(r'Z:\x', 'user', 'open')` → отказ «UNC» (realpath даёт
   `\\server\share`). Подтвердить, что так приемлемо.
6. `uv run python -m timeit -s "from pc import paths" "paths.check(r'C:\Users\me\Documents\x.txt','brain','open')"`
   → <1 мс. Прислать число.
7. `<data>\apps.json`: нет «Удалить …», Readme, сайтов; кириллица цела. Прислать число записей и сколько из них —
   отображаемые имена Shell (name ≠ имени из Get-StartApps).

### S2a — окна, звук, медиа, процессы, файлы, система
1. Закрыть все окна Блокнота, затем `uv run pytest -q -s -m live tests\test_pc_live.py` → 3 passed (Блокнот:
   открыть → новое окно → фокус → свернуть → закрыть только его; `Тест_ёЁ.txt` находится es.exe по точному пути;
   громкость меняется и возвращается). Прислать блок «=== pc live ===» или `bench\results\pc_live-*.json`.
2. `uv run python -c "from pc import files; print([files.known_folder(n) for n in ('загрузки','документы','рабочий стол','картинки','видео','музыка')])"`
   → 6 путей (у OneDrive — переадресованные). Прислать только: есть ли None.
3. Включить музыку → `uv run python -c "from pc import media; print(media.media('play_pause','user'))"` → пауза. Да/нет.
4. hwnd Блокнота: `uv run python -c "from pc import windows; [print(w.hwnd, w.exe) for w in windows.list_windows()]"`;
   затем `uv run python -c "from pc import confirm_client, system; confirm_client.set_confirm_handler(lambda s,d,c: input(s+' [y/N] ')=='y'); print(system.type_text('Привет 😀\nвторая строка', <hwnd>, 'brain'))"`
   → текст с emoji и переводом строки в Блокноте. Повторить с длинным текстом и переключить окно посреди ввода →
   «Окно сменилось — ввод остановлен». Прислать оба Result.text.
5. `uv run python -c "from pc import system; print(system.clipboard_set('тест ёЁ\nстрока','user')); print(system.clipboard_get('user'))"`
   → та же строка. Да/нет.
6. `uv run python -c "from pc import system; print(system.lock('user'))"` → экран заблокирован.
7. (необязательно) сон: `power('sleep','user')` с тем же set_confirm_handler → сон, не гибернация.
8. При открытой «Командной строке (администратор)»: `uv run python -c "from pc import windows; print(windows.focus_target('командная строка','user'))"`
   → отказ «…с правами администратора…».
9. `uv run python -c "from pc import windows; print([(w.exe, len(w.title)) for w in windows.list_windows()])"`
   → совпадает с Alt+Tab, нет «Program Manager». Прислать число окон и лишние/пропущенные exe.
10. Фокус из фона: `Start-Sleep 3; uv run python -c "from pc import windows; print(windows.focus_target('блокнот','user'))"`
    (за 3 с переключиться в другое окно) → ok; если нет — на какой ступени лестницы остановилось (лог).

### S2b — канал подтверждений и MCP-сервер pc
1. `$env:JARVIS_DATA_DIR='C:\Jarvis\scratch\data'; uv run python scripts\mcp_smoke.py`
   → «Инструментов: 18», твои окна, пути из find_files("отчёт") с кириллицей без кракозябр.
   Прислать: число инструментов, «готов за X с», время вызовов (без заголовков окон и путей).
2. Канал между двумя процессами (оба окна PowerShell с `$env:JARVIS_DATA_DIR='C:\Jarvis\scratch\data'`):
   - окно 1: `uv run python -c "import time; from pc import confirm_client as c; s=c.ConfirmServer(lambda a,b,d: input(f'{a} / {b} / {d} y? ')=='y'); s.start(); print(s.address); time.sleep(300)"`
   - окно 2: `$env:JARVIS_CONFIRM_PIPE='<адрес из окна 1>'; uv run python -c "from pc import confirm_client as c; print(c.confirm('Тест?','детали','brain'))"`
   → в окне 1 вопрос с «brain»; «y» → True, другое → False; без ответа 60 с → False. Прислать: результаты и время.

### S3 — руки
1. `Start-Process C:\Jarvis\scripts\start_hands.cmd -ArgumentList 4b -WindowStyle Minimized` → /health = 200.
2. `uv run python -c "from jarvis.hands import Hands; from jarvis.config import load; h=Hands(load().hands); h.ensure_server(); h.warmup(); print(h.status)"`
   → prefix_tokens ≤1500, prefix_cache ok, vram ok, tps ~73–75. Прислать status. prefix_cache "unknown" — в b11498 нет
   `/v1/chat/completions/input_tokens`: сообщить.
3. `uv run jarvis ask --level hands --dry --window "Telegram" "закрой его"` → `(dry) close target=@cur`.
4. `uv run jarvis ask --level hands --dry "сделай потише"` → vol с отрицательным delta.
5. `uv run jarvis ask --level hands --dry "почему тормозит комп"` → ask_gpt.
6. `uv run jarvis ask --level hands "привет как дела"` → короткий reply быстрее ~0,8 с, без HTTP 400/500 (сервер
   принимает maxLength/minimum/maximum в схеме инструментов; если 400 — сообщить: уберу эти поля из TOOLS).
7. `uv run jarvis ask --level hands "открой загрузки"` → открылась «Загрузки».
8. `uv run pytest -q -s -m live tests\test_hands_live.py` → точность ≥90 %, p95 ≤1200 мс, prompt_n тёплых <100.
   Прислать числа из `bench\results\hands-live-<дата>.txt`.
9. При запущенной Ollama: трей → «Выгрузить руки» (или `Hands.stop()`) → завершены только cmd.exe и
   C:\llama\llama-server.exe, Ollama жива.

### S4 — грамматика, контекст, роутер, jarvis ask
1. `uv run jarvis ask "громкость 30"` → громкость 30 без модели, grammar <300 мс. Прислать строку таймингов.
2. `uv run jarvis ask "открой телегу"`, сразу за ним 3. `uv run jarvis ask "закрой его"` → Telegram открылся и закрылся
   («его» = последний объект; второе — grammar:close). Прислать вывод обоих.
4. `uv run jarvis route "почему тест падает"` → brain, heur:question.
5. `uv run jarvis route "открой то что я вчера качал"` → hands, reason «hands» (грамматика не жадничает).
6. `uv run jarvis bench --level grammar` → 60/60 и 0 ложных срабатываний. Прислать только числа.
7. `uv run jarvis ask --dry "громкость 30"` → `[грамматика…] ✓`, «до действия» <1 мс.
8. `uv run jarvis ask --level hands --dry --window "Telegram" "закрой его"` → `решение: close {"target": "@cur"}` и тайминги рук.
9. `pwsh -c "uv run jarvis route 'почему ё ⚙' > r.txt"` → r.txt читается как UTF-8.

### S5 — мозг
1. Если вход в CODEX_HOME мозга не сделан (doctor: «Not logged in»):
   `$d = if ($env:JARVIS_DATA_DIR) { $env:JARVIS_DATA_DIR } else { "$env:LOCALAPPDATA\Jarvis" }; New-Item -ItemType Directory -Force "$d\codex-home" | Out-Null; $env:CODEX_HOME = "$d\codex-home"; codex login; Remove-Item Env:CODEX_HOME`
   (у установленного exe вместо `codex` — `& "$env:LOCALAPPDATA\Programs\Jarvis\_internal\codex_cli_bin\bin\codex.exe" login`).
2. `uv run python scripts\brain_probe.py` → «вход: chatgpt», `[глобально] mcpServer/startupStatus/updated … ready`,
   во втором ходе вызов `pc/list_windows`. Прислать только блок «итог»: codex_start_s, t1_*, t2_cold_thread, t3_warm_thread.
3. `uv run jarvis ask "gpt: объясни в двух предложениях, что такое кэш префикса"` → пометка «холодный старт», текст
   потоком, «GPT до первых слов».
4. `uv run jarvis ask "думай: чем опасно держать модель на 8 ГБ VRAM во время игр"`.
5. `uv run jarvis ask "найди мой последний скачанный pdf и открой"` → find_files → open.
6. `uv run jarvis ask "gpt: заверши процесс блокнота"` → вопрос y/N в консоли.
7. `uv run pytest -q -s -m live tests\test_brain_live.py` → p95 до первых слов ≤3 с и для первого вопроса в новом треде,
   и для повторных; был вызов pc/open; x.txt не создан. Прислать `bench\results\brain-live-<дата>.txt`.
8. Под `jarvis run` (pythonw/Jarvis.exe) окно консоли codex не мелькает ни при старте, ни при первом вопросе.
9. Битый override (например, временно `idle_new_thread_min = "x"` не поможет — проверить вручную через brain_probe с
   испорченным ключом) → «Ошибка конфигурации Codex: …» (на Windows-сборке codex не проверено).

### S7 — журнал, stats, bench, doctor
1. `uv run jarvis doctor` (или `jarvis-cli doctor`) → ✓ сервер рук (/health, /props, владелец порта, кэш префикса,
   т/с), Everything («весь C: в индексе», кириллица), кэш приложений, вход мозга. Прислать вывод (без путей профиля).
2. `uv run jarvis doctor --brain` → «тестовый ход GPT: до первых слов X с». Прислать числа.
3. Сервер рук запущен → `uv run jarvis bench --live` → прислать итоги и список ошибок «фраза → ожидалось → получено»
   (это вход для доводки SYSTEM/примеров/порогов).
4. Через день использования — `uv run jarvis stats` (или `jarvis-cli stats`) → прислать.
5. `es.exe -cp 65001 -version` печатает версию (порядок ключей `-cp` и `-version` как в doctor).
