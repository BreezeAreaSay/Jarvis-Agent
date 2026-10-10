# Задание 00 — переезд на новый проект Jarvis

Ты работаешь на моём ПК: Windows 11, PowerShell 7 (`pwsh`), обычный пользователь без прав администратора.
Выполни шаги по порядку. Перед каждым действием из этого списка коротко скажи, что сделаешь, и дождись моего «да»:
переименование или перенос папок и файлов вне `C:\Jarvis`, `git push`, изменение настроек вне `C:\Jarvis`
(Codex, Everything, автозапуск, переменные окружения), замена `es.exe`. Ничего не удаляй насовсем.

Контекст. Старый проект Jarvis (Ollama, `jarvis model check`, `jarvis run`) больше не развиваем. Новый проект —
маленький, его спецификация — `AGENTS.md` из этого комплекта. Старый код остаётся справочником в `C:\Jarvis-old`
и в истории репозитория (тег `legacy-v2.2`). Репозиторий — публичный https://github.com/stonebridgeway/Jarvis.

Комплект (KIT) — папка, в которой лежит папка `prompts` с этим файлом. В ней: `AGENTS.md`, `CLAUDE.md`,
`.gitignore`, `prompts\`, `scripts\`.

Правила выполнения:
- Каждый вызов оболочки — отдельный процесс: `cd` и переменные из прошлого вызова не сохраняются. Каждый блок кода
  ниже выполняй одним вызовом, пути пиши полностью, git — только как `git -C C:\Jarvis …` (или `C:\Jarvis-old`).
- Для Python-команд в том же вызове сначала: `$env:PYTHONIOENCODING = 'utf-8'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)`
  (иначе кириллица в выводе превращается в кракозябры).
- Токены и пароли (например, в URL удалённого репозитория) в отчёт не выводи — заменяй на `***`.

## Шаг 0. Где ты запущен

Покажи `(Get-Location).Path` и полный путь KIT. Если любой из них внутри `C:\Jarvis` — остановись и попроси меня:
перенести KIT в папку вне `C:\Jarvis` (например, `%USERPROFILE%\Downloads\jarvis-next`), закрыть тебя и запустить
заново из домашней папки: `cd $env:USERPROFILE; codex -s danger-full-access -a on-request`. Windows не даёт
переименовать папку, которая открыта как текущая у любого процесса.

Затем сними пометку «скачано из интернета» с файлов KIT (иначе PowerShell не запустит `.ps1`):
`Get-ChildItem -Path '<KIT>' -Recurse -File | Unblock-File`.

## Шаг 1. Остановить всё, что держит файлы и видеопамять

1. Процессы старого Jarvis и сервера рук:
   `Get-Process | Where-Object { $_.Path -like 'C:\Jarvis\*' -or $_.Path -eq 'C:\llama\llama-server.exe' } | Select-Object Id, Name, Path`
   — покажи список, после «да» останови.
2. Ollama: команды `ollama ps/list/stop` на Windows сами ЗАПУСКАЮТ Ollama — не используй их.
   Проверяй так: `Get-Process 'ollama*' -ErrorAction SilentlyContinue | Select-Object Id, Name, Path`.
   Если запущена — `Invoke-RestMethod http://127.0.0.1:11434/api/ps` покажет загруженные модели; после «да» закрой:
   сначала `ollama app`, затем `ollama` (или «Quit Ollama» в трее).
3. `git -C C:\Jarvis status --porcelain` — в отслеживаемых файлах изменений быть не должно.
   Неотслеживаемые `scripts\start_hands.cmd`, `scratch\`, `bin\es.exe` — нормально.
   Есть изменения в отслеживаемых файлах — остановись и покажи мне.
4. В отчёт: `git -C C:\Jarvis log --oneline -1`, `git -C C:\Jarvis branch --show-current`,
   `git -C C:\Jarvis remote -v` (токены замаскируй). Если `git -C C:\Jarvis status -sb` показывает ahead —
   спроси меня, пушить ли эти коммиты перед переездом.
5. Автозапуск старого Jarvis (после переезда он начнёт запускать новый код или падать):
   ```powershell
   Get-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run | Format-List
   Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup" | Select-Object Name
   Get-ScheduledTask | Where-Object { "$($_.Actions.Execute) $($_.Actions.Arguments)" -like '*Jarvis*' } | Select-Object TaskName, State
   ```
   Покажи записи с `C:\Jarvis`; после «да» отключи (задачу — `Disable-ScheduledTask`, ярлык — перенеси из Startup,
   значение Run — переименуй, добавив `-disabled`).

## Шаг 2. Старый проект → `C:\Jarvis-old`

Если `C:\Jarvis-old` уже существует — остановись и спроси (возможно, шаг уже выполнялся).
Иначе, после «да»: `Rename-Item C:\Jarvis C:\Jarvis-old`.
Папка занята — найди процесс (шаг 1.1, плюс открытые терминалы и редакторы в этой папке), покажи и спроси.
Старый `.venv` после переименования сломан — это нормально; если старый Jarvis понадобится:
`uv sync --reinstall` в `C:\Jarvis-old`.

## Шаг 3. Новый `C:\Jarvis` из того же репозитория

Если `C:\Jarvis` уже существует (шаг выполнялся частично) — покажи `git -C C:\Jarvis status -sb` и спроси.

```powershell
git clone https://github.com/stonebridgeway/Jarvis C:\Jarvis
if (-not (git -C C:\Jarvis tag --list legacy-v2.2)) { git -C C:\Jarvis tag legacy-v2.2 origin/main }
git -C C:\Jarvis rm -r -q .
New-Item -ItemType Directory -Force C:\Jarvis\bin, C:\Jarvis\scratch | Out-Null
```

`git rm` очищает только рабочее дерево новой копии: история и тег остаются. Скопируй из KIT в `C:\Jarvis`:
`AGENTS.md`, `CLAUDE.md`, `.gitignore`, всю папку `prompts\`, всю папку `scripts\`; затем
`Get-ChildItem C:\Jarvis\scripts, C:\Jarvis\prompts -Recurse -File | Unblock-File`.
Перенеси из `C:\Jarvis-old` (после «да»): `bin\es.exe` → `C:\Jarvis\bin\`; `scratch\hands_test.json` и
`scratch\brain_smoke.py` → `C:\Jarvis\scratch\` (старый smoke — для истории; замер мозга — новым `scripts\brain_smoke.py`).

В новом `C:\Jarvis\scripts\start_hands.cmd`: если в старом `C:\Jarvis-old\scripts\start_hands.cmd` flash attention
был выключен (`-fa off`, `-fa 0` или `-fa false` — выбран по итогам llama-bench) — поставь `-fa off` и убери
`-ctk q8_0 -ctv q8_0` (квантованный кэш без flash attention не работает). Остальные флаги не меняй.

Автор коммитов: `git -C C:\Jarvis config user.name` и `git -C C:\Jarvis config user.email`. Пусто — спроси меня,
что поставить (репозиторий публичный: e-mail в коммитах виден всем; можно `<id>+<логин>@users.noreply.github.com`).

```powershell
git -C C:\Jarvis add -A
git -C C:\Jarvis status --short
```

В индексе должны быть только файлы KIT; `bin\`, `scratch\`, `logs\` — нет (их скрывает `.gitignore`). Затем:
`git -C C:\Jarvis commit -m "chore: start the new Jarvis (old code is tagged legacy-v2.2)"`,
покажи `git -C C:\Jarvis show --stat HEAD | Select-Object -First 40` и спроси меня перед пушем. После «да»:
`git -C C:\Jarvis push origin legacy-v2.2` и `git -C C:\Jarvis push origin main`.

## Шаг 4. Codex: версия, конфиги, модели, вход для мозга Jarvis

1. `codex --version` → в отчёт. Ниже 0.160 — скажи мне и предложи обновить Codex прежде, чем идти дальше
   (профили-файлы `-p deep` работают с 0.160).
2. Конфиг: `$dir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$env:USERPROFILE\.codex" }; $cfg = "$dir\config.toml"`.
   Сделай копию `config.toml.bak-<дата>`. Затем проверь и покажи мне, что найдено:
   - строка `profile = "..."` (в 0.160 ломает запуск) или таблицы `[profiles.*]` (не работают): перенеси содержимое
     `[profiles.X]` в файл `X.config.toml` в той же папке и убери из `config.toml`;
   - `approval_policy = "never"`, `sandbox_mode = "read-only"`, таблица `[mcp_servers.pc]` — остатки моего старого
     черновика: убери (мозг Jarvis настраивается сам, в своей папке);
   - остальные `[mcp_servers.*]` — только перечисли в отчёте;
   - `AGENTS.md` или `AGENTS.override.md` в `$dir` — скажи мне (они подмешиваются во все запросы Codex);
   - добавь, если нет (существующие значения не меняй). Ключи верхнего уровня — В НАЧАЛО файла, до первой строки
     `[...]` (ключи после заголовка таблицы принадлежат таблице и молча не работают):
     `check_for_update_on_startup = false`; если `model` не задан — `model = "<основная модель из п.4>"` и
     `model_reasoning_effort = "medium"`. Таблицы — в конец; если таблица уже есть — добавь ключ в неё, второй
     заголовок не создавай: `[analytics]` `enabled = false`, `[feedback]` `enabled = false`, `[windows]` `sandbox = "unelevated"`;
   - проверь, что файл разбирается: `uv run --no-project --python 3.12 python -c "import tomllib,sys; print(sorted(tomllib.load(open(sys.argv[1],'rb'))))" $cfg`.
3. Создай (или, после копии, перезапиши) `$dir\deep.config.toml` — профиль для моих сессий кодинга по Jarvis.
   `<UV>` — фактическое значение `Join-Path $env:LOCALAPPDATA 'uv'` (кэш uv: без этого `uv sync`/`uv run` в песочнице
   сессии падают с Access is denied):
   ```toml
   model = "<основная модель из п.4>"
   model_reasoning_effort = "high"
   sandbox_mode = "workspace-write"
   approval_policy = "on-request"

   [sandbox_workspace_write]
   network_access = true
   writable_roots = ['<UV>']
   ```
4. Модели, доступные моему аккаунту (без скрытых; файл нужен сессии S5):
   ```powershell
   $env:PYTHONIOENCODING = 'utf-8'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
   uv run --no-project --python 3.12 --with openai-codex==0.160.1 python -c "from openai_codex import Codex; c=Codex(); lines=[m.model_dump_json(exclude_none=True) for m in c.models().data]; c.close(); open(r'C:\Jarvis\scratch\codex-models.txt','w',encoding='utf-8').write('\n'.join(lines)); print('\n'.join(m[:200] for m in lines))"
   ```
   Выпиши id быстрой модели (luna / mini / самая дешёвая) и основной (sol / workhorse). Если в п.2 модель
   в `config.toml` не была задана — впиши основную туда и в `deep.config.toml`.
5. Проверка обоих конфигов (каждый вызов — холодный старт, 5–15 с — это нормально):
   ```powershell
   codex exec --skip-git-repo-check --ephemeral "Ответь одним словом: ок"
   codex exec -p deep --skip-git-repo-check --ephemeral "Ответь одним словом: ок"
   ```
   Ошибка конфига или 403 — покажи текст ошибки целиком.
6. Отдельный вход для мозга Jarvis (у мозга своя папка Codex, чтобы не тянуть мои настройки и MCP-серверы).
   Спроси меня — откроется браузер для входа:
   ```powershell
   New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\Jarvis\codex-home" | Out-Null
   $env:CODEX_HOME = "$env:LOCALAPPDATA\Jarvis\codex-home"
   codex login
   codex login status
   ```
   `codex login status` пишет результат в stderr; ожидаю «Logged in using ChatGPT».

## Шаг 5. Everything: индексируется ли весь диск и читается ли кириллица

Без службы Everything обычный пользователь не получает NTFS-индекс диска C: (он всегда «системный») —
только индексы отдельных папок. Поиск «по всему компу» тогда молча ничего не находит.

1. ```powershell
   $env:PYTHONIOENCODING = 'utf-8'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
   uv run --no-project --python 3.12 python C:\Jarvis\scripts\es_check.py
   sc.exe query Everything
   ```
   Нужен ES не старее 1.1.0.37 (там есть `-argv` и `-cp 65001`). Старее — спроси меня и после «да» скачай ES 1.1.0.38
   x64 с https://github.com/voidtools/ES/releases и замени `C:\Jarvis\bin\es.exe` (через winget не ставь: пакет тянет
   за собой установку Everything с правами администратора). Код 8 и Everything запущен — возможно, это 1.5 alpha:
   скажи мне (тогда нужен `-instance 1.5a`).
2. «объектов в индексе C:\Windows: 0» — диск C: целиком не индексируется. Покажи мне цифры и спроси, какой вариант:
   - а) служба Everything (нужен один раз пароль администратора): Everything → Tools → Options → General →
     «Everything Service» (галочка, а не квадрат). Полный индекс C: с обновлением в реальном времени.
     `run_as_admin` оставить 0;
   - б) без администратора: Tools → Options → Indexes → Folders → Add — `%USERPROFILE%`, `C:\Jarvis` и другие папки
     с моими файлами; у каждой включить «Attempt to monitor changes» и ежедневную перепроверку.
     Диск D: (если он не системный) можно индексировать как NTFS и без службы: Tools → Options → Indexes → NTFS →
     D: → «Include in database».
   После настройки — снова `es_check.py`.
3. Кириллица: строка «кириллица -cp 65001» должна показать «точный путь найден: True». Без `-cp` ожидаю False.
4. Если в существующем ярлыке автозапуска Everything нет аргумента `-startup` — после «да» добавь (Everything стартует
   сразу в трей). Второй автозапуск не создавай.
5. В отчёт: версии ES и Everything, числа из `es_check.py`, служба есть/нет, выбранный вариант, итог п. 3.

## Шаг 6. Ollama не должна занимать видеопамять

Ollama в новом проекте не нужна. Модели не удаляем, саму Ollama не удаляем.

1. После «да» выключи автозапуск: Диспетчер задач → «Автозагрузка приложений» → Ollama → «Отключить»
   (или перенеси `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk` в
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Ollama-autostart.lnk.off` — вне папки Startup).
2. После «да» — подстраховка на случай, если её кто-то запустит: переменная пользователя `OLLAMA_KEEP_ALIVE=0`
   (`[Environment]::SetEnvironmentVariable('OLLAMA_KEEP_ALIVE','0','User')`) — модель выгружается сразу после ответа.
   Переменные `GGML_VK_*` не ставь: их читает и сервер рук.
3. Процесс модели у Ollama тоже называется `llama-server.exe`. Наш сервер — `C:\llama\llama-server.exe` на порту 8081;
   останавливая процессы, смотри на путь, а не на имя.
4. В отчёт: была ли Ollama запущена, выключен ли автозапуск, задан ли `OLLAMA_KEEP_ALIVE`.

## Шаг 7. Замер рук: 4B и 8B

Перед замером закрой игры и тяжёлые программы на GPU. Выполни блок одним вызовом с `$model = '4b'`, затем ещё раз
с `$model = '8b'`:

```powershell
$model = '4b'
$p = Start-Process -FilePath C:\Jarvis\scripts\start_hands.cmd -ArgumentList $model -WindowStyle Minimized -PassThru
$ready = $false
foreach ($i in 1..360) {
  try { if ((Invoke-WebRequest http://127.0.0.1:8081/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $ready = $true; break } } catch {}
  Start-Sleep -Milliseconds 500
}
if ($ready) {
  pwsh -NoProfile -ExecutionPolicy Bypass -File C:\Jarvis\scripts\hands_probe.ps1 -Label $model | Tee-Object "C:\Jarvis\scratch\hands-$model.txt"
} else {
  "сервер рук ($model) не поднялся за 3 минуты:"
  Get-Content C:\Jarvis\logs\hands.log -Tail 30
}
Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object Path -eq 'C:\llama\llama-server.exe' | Stop-Process
Stop-Process -Id $p.Id -ErrorAction SilentlyContinue
```

Как читать итоговую строку замера:
- `tg` (т/с) сравни с llama-bench: 4B ≈ 86, 8B ≈ 52. Заметно ниже (меньше ~75 %) — VRAM чем-то занята, и данные
  молча уходят в общую память (Диспетчер задач → Производительность → GPU → растёт «Общая память GPU»). Найди, кто
  занял (Ollama, игра, браузер), скажи мне — такой замер не годится. Строка `offloaded …` в логе ничего не скажет:
  при `-ngl 99` она всегда полная.
- `prompt_n` должен быть десятки, а `cache_n` — близко к длине префикса (около тысячи): иначе кэш префикса не работает.
- В логе есть `Device memory allocation of size` — не хватило видеопамяти, причина та же.

## Шаг 8. Замер мозга

```powershell
$fast = 'gpt-6-luna'   # подставь id быстрой модели из шага 4
$env:PYTHONIOENCODING = 'utf-8'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
uv run --no-project --python 3.12 --with openai-codex==0.160.1 python C:\Jarvis\scripts\brain_smoke.py $fast | Tee-Object C:\Jarvis\scratch\brain-smoke.txt
```

Скрипт работает из пустой временной папки и с моим обычным входом в Codex. Первая строка вопросов — первый ход,
остальные — тёплые.

## Шаг 9. Отчёт

Выведи отчёт ровно в таком виде (я перешлю его целиком):

```
== ОТЧЁТ 00 ==
Старый проект: C:\Jarvis-old, ветка <имя>, последний коммит <hash> <тема>; remote <url без токенов>; автозапуск старого: <что найдено/отключено>
Новый репозиторий: C:\Jarvis, коммит <hash>; запушено: да/нет; тег legacy-v2.2: да/нет; автор коммитов: <имя> <e-mail или «noreply»>
start_hands: -fa <on/off>
Codex: <версия>; быстрая модель: <id>; основная: <id>; config.toml: <что изменено>; мои MCP-серверы: <список>; exec ок: да/нет; -p deep ок: да/нет; вход мозга (codex-home): да/нет
Everything: ES <версия>, Everything <версия>; в индексе всего/C:\Windows/профиль/D: — <числа>; служба: да/нет; вариант: <а/б>; кириллица с -cp 65001: True/False
Ollama: <была ли запущена; автозапуск; OLLAMA_KEEP_ALIVE>
Руки 4B: <последняя строка hands-4b.txt>
Руки 8B: <последняя строка hands-8b.txt>
Мозг:
<все строки brain-smoke.txt>
Проблемы и вопросы: <что не получилось>
```

После отчёта — полные таблицы из `hands-4b.txt` и `hands-8b.txt`.
