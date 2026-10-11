# Заметки (длинное из «Заметок» AGENTS.md)

## Принятые риски
- Канал подтверждений: DACL named pipe по умолчанию (multiprocessing) даёт Everyone чтение, удалённые клиенты
  не отклоняются (PIPE_REJECT_REMOTE_CLIENTS не ставится) — принятый риск для домашнего ПК: без записи и ключа
  `<data>\pipe.key` нельзя ни отправить запрос, ни подделать ответ «да» (рукопожатие HMAC взаимное).
- Несколько процессов pc.mcp (по одному на тред мозга) пишут в один `<data>\logs\pc-mcp.log`; на Windows ротация
  на 1 МБ может не пройти (файл занят) — запись продолжится в тот же файл, ошибка — в stderr.
- FastMCP читает `.env` из cwd и переменные FASTMCP_*: cwd MCP-процесса — папка установки/репозитория; `.env` туда
  не класть.

## Облачная сессия (NIGHT-PROMPT)
- GitHub Actions аккаунта BreezeAreaSay заблокированы: «The job was not started because your account is locked
  due to a billing issue» (ни один прогон с 1 октября не стартовал). Windows-проверка ночью — под Wine
  (docs/wine-build.md); build.yml заработает после починки биллинга или после переноса в stonebridgeway/Jarvis.
- Windows-линия под Wine (scripts/wine_build.sh, Windows-Python 3.12.10): pytest — 2000 прошли, 5 упали, все —
  ограничения Wine 9.0 (junction/symlink не создаются, psutil NtQuerySystemInformation, IsProcessInJob, медленный
  старт FastMCP ≈6 с); до исправления aaed6e7 ещё 6 тестов рук падали из-за C:\Jarvis\scripts на машине тестов —
  на windows-latest упали бы так же. Сборка: onedir 503 МБ / 164 файла, установщик ≈112 МиБ; selftest 8/8 ✓;
  тихая установка, ярлык, ключ Run, переустановка без автозапуска, удаление (каталог данных цел) — ✓.
- PyInstaller: «Hidden import "tzdata" not found» — безвредно: zoneinfo нужен только pydantic для полей ZoneInfo,
  в схемах MCP и в коде Jarvis их нет.

## Мозг (S5)
- fallback_provider: в generated-типах SDK 0.160.1 поля wire_api нет; бинарь codex 0.160.1 принимает только
  `wire_api = "responses"` (`chat` — «no longer supported»). Поля ModelProviderInfo: env_key, query_params,
  requires_openai_auth, supports_websockets, stream_max_retries, stream_idle_timeout_ms.
  TODO(live): отвечает ли `https://api.deepseek.com/v1/responses`. Кода fallback нет до подтверждения.
- Ошибка overrides в 0.160.1: неверный тип/значение роняет codex при старте (`Error: … in \`key\`` в stderr) —
  Brain.start_error() показывает «Ошибка конфигурации Codex: …»; неизвестный ключ — только configWarning;
  путь из S5 (InvalidRequestError на первом thread_start) тоже ловится.
- Без сети codex повторяет запрос бесконечно («Reconnecting…») — ход прерывается через 30 с: «Нет связи с GPT».
- SDK потокобезопасен для thread_start в фоне во время чужого хода (своя очередь ответа на запрос, запись под
  CodexClient._lock) — проверено на Linux-сборке codex.
- Глобальные уведомления читаются через `codex._client.next_notification()` — внутренности SDK (версия закреплена).

## Ревью S8 (независимые ревьюеры по направлениям; каждая находка — с тестом, который до исправления падал)
| Серьёзность | Что нашёл | Что сделал |
|---|---|---|
| critical | Job Object без breakaway: программы, открытые мозгом (Word, браузер), — члены job и умирают при выходе из Jarvis | SILENT_BREAKAWAY_OK; llama-server — в job явно по pid (d89f076) |
| high | own_pids = все потомки Jarvis: Chrome/Word, открытые через Jarvis (ShellExecute), считались «самим Jarvis» — не закрыть, не свернуть, нет в списке окон | обход дерева только через свои exe (OWN_EXES) (d89f076) |
| high | Общий флаг отмены в Core: заменённая команда рук (ждавшая загрузки модели) исполнялась после новой | свой Event на каждый запрос (5f7a186) |
| high | Гигиена не видела файлы с кириллицей в имени (git quotePath) | `-c core.quotepath=off ls-files -z` (ee022a0) |
| high | Конфиг exe «переезжал» в C:\Jarvis\jarvis.toml после первого запуска рук (start_hands.cmd создаёт C:\Jarvis\logs) — терялись private_paths и mode | критерий — сам файл, не папка (ee022a0) |
| high | Грамматика при промахе apps.resolve под `jarvis run` синхронно запускала Get-StartApps (секунды на уровне 0) | grammar — apps.lookup (ee022a0) |
| medium | Esc до старта запроса и заменённый запрос не отменялись | `_cancel_rid`/`_stale` в app (5f7a186) |
| medium | Хоткей во время команды менял её «@cur» (закрывалось не то окно) | окно хоткея откладывается до следующей команды (5f7a186) |
| medium | Зависшее окно: ShowWindow/BringWindowToTop висели вечно (в pc.mcp — все инструменты мозга) | IsHungAppWindow → отказ (d89f076) |
| medium | Непарный суррогат в заголовке окна ронял руки (UnicodeEncodeError) | pc.windows.clean_text; руки — errors="replace" (a2a8e49) |
| medium | audio.warmup() не вызывался при старте | вызов в фоне при старте (a2a8e49) |
| medium | bench --live считал верный ask_gpt рук ошибкой уровня | handed_over (ee022a0) |
| medium | В гигиене не было e-mail | test_no_emails (ee022a0) |
| low | Локальный режим не снимал открытые вопросы «просит GPT» | deny_confirms("brain") + отказ новым (5f7a186) |
| low | Выход во время загрузки модели не закрывал мозг/журнал (руки держали замок) | руки останавливаются последними (5f7a186) |
| low | Запуск приложения ждал перечисления AppsFolder в общем STA | своё STA-перечисление (a2a8e49) |
| low | AllowSetForegroundWindow из pc.mcp бесполезен — программы мозга не выходили наверх | из процесса Jarvis перед запросом и после «да» (d89f076) |
| low | Зависший COM навсегда занимал поток звука | новый поток после таймаута (d89f076) |
| low | pywintypes.error буфера обмена не OSError — «внутренняя ошибка» | обработанный отказ (d89f076) |
| low | save_mode: ключ `"mode"` в кавычках давал дубль | регулярка (ee022a0) |
| low | Гигиена: кэши, пути `C:/Users/<имя>/…` | добавлены (ee022a0) |
| low | Нет теста «действие грамматики/инструмент рук → строка policy» | tests/test_s8_policy_mapping.py (ee022a0) |
| — | Программы, открытые Jarvis, наследовали QT_PLUGIN_PATH/PATH бандла (чужой Qt грузил наши плагины) | scrub_child_env; jarvis-cli ставит Qt-путь только для selftest/run (3c6f039) |

## Идеи из аналогов (только идеи, без кода)
Источники: RustamovAkrom/JARVIS (лицензия проприетарная — код не брать), jarvis.institute («Jarvis AI Assistant»
для Windows, по описаниям на сайте и в Microsoft Store), jarvis.cx (браузерный/десктопный копилот).
- Берём (сделано в этой сессии): отмена последнего обратимого действия («отмени», «верни громкость»);
  напоминания («напомни через 20 минут …», локально, уведомление в трее); память о пользователе
  («запомни …», «забудь …»; пишет только человек, мозгу — в контексте); «что ты умеешь» без модели; журнал
  инструментов мозга и их сводка в stats; анимированная сфера-индикатор состояния (вместо аватара-лица).
- В план: голос (S9) — wake-word «Джарвис» + push-to-talk, подавление собственного эха, короткая голосовая реплика
  перед долгой операцией; «переведи / исправь текст из буфера» (буфер читает Jarvis по явной команде человека —
  без лишнего подтверждения мозгу); окно-сводка статистики (stats в окне вместо CLI); «аудит облака» — что ушло
  мозгу по каждому запросу (сейчас — журнал, если store_text).
- Не берём: веб-дашборд на 0.0.0.0 и удалённое управление (Telegram/MQTT) — противоречит «нет TCP/HTTP-слушателей»;
  shell и запуск кода для ИИ — запрещено политикой; плагины в каталоге — поверхность атаки; зрение по экрану и
  веб-камере — приватность (если когда-то — только по явной команде и с подтверждением); расписания, которые сами
  исполняют действия, — без человека в контуре это дыра.
