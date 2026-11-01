# Управление компьютером агентами подписки (п. 1.3 плана)

Сверено 1 ноября 2026. Источники: документация Claude Code, исходники и задачи `openai/codex` на GitHub. Сами
программы не запускались. Сайты OpenAI (developers.openai.com, community.openai.com) из среды исследования не
открывались: всё, что взято с них, — пересказ поисковой выдачи. Где написано «вывод», это моё заключение.

## Коротко

- **Claude Code CLI — нельзя.** Управление компьютером в CLI есть только на macOS и только в интерактивной
  сессии, с `-p` не работает. На Windows оно есть только в приложении Claude Desktop (Cowork, вкладка Code).
- **Codex app-server — частично, без документации.** Это плагин приложения ChatGPT/Codex для ПК. Ядро Codex
  умеет подгрузить его из конфигурации, но работу headless из стороннего клиента OpenAI не описывает. На Windows
  агент работает только на переднем плане.
- **Для Jarvis:** задачи «сделай в программе…» им сейчас не отдавать. Эти же модели Jarvis уже ведёт своим
  агентом со своими подтверждениями.

## Как Jarvis запускает их сейчас (по коду)

- **Claude Code** (`core/jarvis/ai/cli/specs.py`): `claude -p` с stream-json, `--tools ""`,
  `--permission-mode dontAsk`, `--setting-sources ""`, `--strict-mcp-config`, `--mcp-config` только с сервером
  `jarvis`, `--allowedTools mcp__jarvis__*`. В окружении нет `ANTHROPIC_API_KEY`,
  `ENABLE_CLAUDEAI_MCP_SERVERS=false`.
- **Codex** (`core/jarvis/ai/chatgpt/`): SDK 0.163.0, свой `CODEX_HOME`, треды `approvalPolicy: never` и
  `sandbox: read-only`. Выключены `features.plugins`, `code_mode`, `computer_use`, `browser_use` и
  `in_app_browser`. На запросы разрешений и `mcpServer/elicitation/request` Jarvis отвечает отказом.
- Итог: оба пути уже намеренно закрыты от встроенного управления компьютером.

## Claude Code

- **Что есть.** Встроенный сервер MCP `computer-use`, по умолчанию выключен. Включается только командой
  `/mcp` → Enable в интерактивной сессии, выбор сохраняется для проекта. Флага или переменной окружения нет —
  в cli-reference и env-vars его нет.
- **Платформа.**
  - CLI: только macOS («not available on Linux or Windows. On Windows, use … Desktop»).
  - Desktop: macOS и Windows, research preview, приложение должно быть запущено; фоновый режим — только macOS.
  - Запрос на поддержку Windows в CLI (#64381) закрыт как «not planned» 27.07.2026. В CHANGELOG до 2.1.316
    ничего нового о Windows или `-p` нет.
- **Headless.** «Not available in non-interactive mode with the `-p` flag». На macOS Jarvis отсёк бы этот
  сервер и сам: `--strict-mcp-config` и `--setting-sources ""`.
- **План.** Pro, Max или Ultra со входом через claude.ai. На Team и Enterprise, через Bedrock, Vertex и Foundry
  недоступно.
- **Разрешения** — в самом Claude Code, мимо Jarvis:
  - одобрение каждой программы на сессию;
  - уровни доступа: браузеры — только просмотр, терминалы и IDE — только клик, остальное — полный доступ;
  - Esc останавливает работу; одновременно работает одна сессия; остальные окна на время скрываются.
- **Обходы — не рекомендую.** Интерактивный `claude` в скрытом терминале на Windows не поможет. У Desktop,
  Dispatch и Remote Control нет API для сторонней программы.
- **Условия.** Запускать неизменённый `claude`, в который человек вошёл сам, разрешено. Предлагать вход
  claude.ai в своей программе и вести через подписку запросы за других — запрещено. Лимиты подписки рассчитаны
  на «ordinary, individual usage».

## Codex

- **Что есть.** Computer Use — плагин приложения Codex (теперь «ChatGPT desktop app»).
  - macOS: с 16.04.2026, работает и в фоне.
  - Windows: с 29.05.2026 (приложение 26.527). Только передний план, нужное окно должно быть видно, «locked use»
    недоступен.
  - Включение: плагин в настройках приложения, вызов через `@` или по решению модели.
- **Где это в Codex** (исходники main, 1.11.2026):
  - плагины `computer-use@openai-bundled` и `unified-computer-use@openai-bundled`;
  - их каталог `CODEX_HOME/.tmp/bundled-marketplaces/openai-bundled` наполняет само приложение;
  - серверы MCP — JS-REPL `node_repl` и `cua_repl`;
  - `features.computer_use` в коде помечен «requirements-only gate», по умолчанию включён;
  - настройка `[computer_use]`: `default_app_access`, `windows.aumids`, `windows.exes` (allow или deny по
    издателю, продукту и exe), `macos.bundle_ids`. По выдаче поиска, «Always allow» хранится в
    `computer_use.windows.always_allowed_app_ids`;
  - Guardian — автопроверка действий: вход в аккаунты, действия с последствиями и утечку данных он запрещает.
- **Headless — наблюдения, не документация.**
  - #20851 (открыта с 03.05.2026) просит поддержки в CLI. На macOS из CLI работало, только когда были
    установлены Codex.app и плагин: «the CLI alone does not include the MCP».
  - #52645 (09.10.2026, Windows, приложение 26.1002, codex-cli 0.162.0-alpha.2). Приложение пишет в общий
    `%USERPROFILE%\.codex\config.toml`:
    - `notify = [...\codex-computer-use.exe, "turn-ended"]`;
    - `[plugins."unified-computer-use@openai-bundled"]`;
    - `[mcp_servers.node_repl]` → `%LOCALAPPDATA%\OpenAI\Codex\runtimes\cua_node\<hash>\bin\node_repl.exe`.

    Headless `codex exec` с этой конфигурацией поднимает эти процессы. Делает ли он без приложения настоящие
    снимки и клики — **не подтверждено**.
- **Как включилось бы из Jarvis (вывод, не проверено).**
  - В своём `CODEX_HOME` снять запреты `plugins`, `code_mode` и `computer_use`.
  - Прописать `node_repl` и плагин по путям установленного приложения; хеш в пути меняется с обновлениями.
  - Отвечать на одобрения и elicitation вместо отказа. Как плагин спрашивает разрешения через app-server, не
    подтверждено.
  - Плюс: на `%USERPROFILE%\.codex` Jarvis не смотрит, поэтому runtime приложения сам не подхватывает.
- **План.** В таблице цен — «Limited*» для Plus, Pro, Business и Enterprise/Edu; сноска не прочитана. На
  Enterprise под Windows по умолчанию выключено. Free и Go — не подтверждено. EEA, Великобритания и Швейцария:
  на старте не было, сведения об открытии противоречат друг другу.

## Чем рискуем

1. **Слой подтверждений Jarvis не работает.** Кликает помощник Anthropic или OpenAI, Jarvis видит только итог
   вызова. Карточки «Разрешить» перед отправкой, удалением и оплатой, `ai.agent_confirm=always` и правило
   «после отказа другим путём нельзя» не срабатывают. Остаются только одобрения программ и Guardian поставщика.
2. **Приватные окна.** Снимки делает чужой помощник, поэтому скрытие приватных окон в Jarvis не действует. На
   Windows в облако уходит весь видимый рабочий стол. Запрет в Codex действует на программу целиком, не на окно.
3. **Два курсора и ввод человека.** На Windows оба агента работают на переднем плане настоящей мышью и
   клавиатурой. Детектор ввода человека Jarvis их не остановит, оверлей «курсор агента» не знает, куда они жмут.
4. **Нестабильные интерфейсы.**
   - Не документировано ничего: `bundled-marketplaces`, `runtimes\cua_node\<hash>`, `node_repl`/`cua_repl`;
     `unified-computer-use` уже пришёл на смену `computer-use`.
   - Jarvis закрепляет SDK 0.163.0, а приложение обновляется само.
   - Открытые баги Windows (#52645): помощник перезапускается без дела, после таймаутов остаются процессы.
5. **Чужое приложение и регион.** Нужен установленный, вероятно и запущенный, ChatGPT desktop или Claude
   Desktop. Действуют региональные ограничения поставщиков.
6. **Лимиты.** Снимки экрана — дорогие токены. Пользователи замерили (#97074, Anthropic не ответил), что
   `claude -p` тратит 5-часовое окно в 1,8–2,5 раза быстрее интерактивного режима.
7. **Не про управление компьютером, но важно.** В документации headless: «`--bare` … will become the default
   for `-p` in a future release». В bare OAuth не читается, так что провайдер «Claude Code по подписке» может
   перестать работать после обновления. `--safe-mode` вход сохраняет, но не грузит MCP (#101426).

## Что предлагаю

- **Решение:** сейчас не отдавать «дела в программах» встроенному управлению компьютером подписок.
  - Claude: на Windows невозможно.
  - Codex: не документировано и обходит подтверждения Jarvis.
  - Текущие запреты оставить.
- **Что даст эффект уже сейчас** (вывод, проверить на evals):
  - Агент Jarvis уже ведёт эти модели по подписке. Проверить, станет ли он точнее, если инструменты будут похожи
    на «computer» (снимок + клик по координатам).
  - Для провайдера Anthropic по ключу — `computer_toolset_20260801`. Он вышел из беты, а Claude 5.5 и новее
    поддерживают управление компьютером только через него. Действия исполняет Jarvis, подтверждения сохраняются.
- **Вернуться к вопросу, если** Claude Code получит Windows или `-p` для `computer-use`, либо OpenAI закроет
  #20851 или опишет computer use в app-server с API подтверждений.
- **Минимальный эксперимент на ПК владельца** (Codex по правилам плана: только `C:\jarvis-check`, код не
  трогать, ни во что не входить, перед мышью и окнами спрашивать, итог — отчёт `.md`):
  1. **Инвентаризация, без экрана.**
     - Версии `claude --version` и `codex --version`; установлен ли ChatGPT desktop (`Get-AppxPackage *OpenAI*`).
     - В `%USERPROFILE%\.codex\config.toml` найти `mcp_servers.node_repl`, `plugins.*computer-use*` и `notify` —
       выписать только имена и пути. `auth.json` не открывать.
     - Перечислить имена в `%LOCALAPPDATA%\OpenAI\Codex\runtimes\` и в
       `%USERPROFILE%\.codex\.tmp\bundled-marketplaces\openai-bundled\plugins\`.
  2. **Claude Code.** В `C:\jarvis-check\cc` запустить `claude -p "ответь ok" --output-format stream-json
     --verbose` и выписать `mcp_servers` и `tools` из `system/init`. Затем в интерактивном `/mcp` посмотреть,
     есть ли `computer-use`. Ожидаем, что нет. Ничего не включать.
  3. **Codex** — только если приложение установлено и владелец рядом.
     - Запустить `codex app-server` с `CODEX_HOME` по умолчанию (там уже вход приложения).
     - Вызвать `initialize`, `config/read`, `plugin/list`, `mcpServerStatus/list`: видны ли плагин и `node_repl`?
     - С согласия владельца — один ход с `approvalPolicy: on-request`: «сделай снимок окна Блокнота и опиши,
       ничего не нажимай». Блокнот открыть заранее, приватное закрыть.
     - Записать: точные имена методов разрешений и elicitation; был ли снимок; двигалась ли мышь; какие процессы
       появились (`node_repl.exe`, `codex-computer-use.exe`) и остались ли после выхода.

## Источники (открыты 1.11.2026)

- https://code.claude.com/docs/en/computer-use — CLI: только macOS, без `-p`, планы
- https://code.claude.com/docs/en/desktop#let-claude-use-your-computer — Desktop: macOS и Windows, уровни
- https://code.claude.com/docs/en/headless — `--bare` станет умолчанием для `-p`
- https://code.claude.com/docs/en/legal-and-compliance — вход по подписке, сторонние программы
- https://code.claude.com/docs/en/cli-reference , https://code.claude.com/docs/en/env-vars — флага нет
- https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md — до 2.1.316
- https://github.com/anthropics/claude-code/issues/64381 , /97074 , /101426
- https://support.claude.com/en/articles/14128542 — computer use в Cowork: безопасность, планы
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool — `computer_toolset_20260801`
- https://developers.openai.com/codex/app/computer-use , https://developers.openai.com/codex/pricing — по выдаче поиска
- https://openai.com/index/codex-for-almost-everything/ — 16.04.2026 (по выдаче поиска)
- https://www.neowin.net/news/openai-rolls-out-major-codex-for-windows-update-with-computer-use-and-mobile-access/ — 29.05.2026, вторичный
- https://github.com/openai/codex — `codex-rs/features/src/lib.rs`, `config/src/computer_use.rs`,
  `plugin/src/bundled_hooks.rs`, `core-plugins/src/marketplace_policy.rs`,
  `prompts/templates/guardian/node_repl_policy.md`, `app-server/README.md`
- https://github.com/openai/codex/issues/20851 , https://github.com/openai/codex/issues/52645
