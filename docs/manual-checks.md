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
