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
- Windows-линия под Wine (scripts/wine_build.sh, Windows-Python 3.12.10), финальный прогон на c727f17: pytest —
  2819 прошли, 5 упали, все — ограничения Wine 9.0 (junction/symlink не создаются, psutil NtQuerySystemInformation,
  IsProcessInJob, медленный старт FastMCP ≈6 с) — те же 5, что в первом прогоне (тогда 2000 прошли); до
  исправления aaed6e7 ещё 6 тестов рук падали из-за C:\Jarvis\scripts на машине тестов. Сборка: onedir 533 МБ /
  178 файлов, zip 196 МиБ, установщик 117,8 МиБ; selftest 9/9 ✓ (голос: sherpa-onnx, onnxruntime.dll и PortAudio
  в бандле грузятся); тихая установка, ярлык, автозапуск, переустановка без автозапуска, удаление (каталог данных
  цел) — ✓.
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
| high | Ревью безопасности (F01): «Локально» во время ожидания замка — ask() поднимал Codex заново, вопрос уходил в облако | start(resume=False) из ask (a06abdc) |
| high | SDK openai-codex по умолчанию ОДОБРЯЕТ запросы codex на команды и правки файлов (защищали только read_only и deny_all) | deny_server_request: всё — отказ (a06abdc) |
| medium | F09: модели каталога 0.160.1 объявляют multi_agent v2 — collaboration.spawn_agent есть при features.multi_agent=false (9 ключей перепроверено зондом) | ход с под-агентом прерывается, строка «⚠ …»; запрет в инструкции мозгу (a06abdc) |
| medium | F03: [последнее: …] для окон — redact_text вместо redact_title; при битом конфиге уходили память и заголовок | redact_title; при битом конфиге не отдаём (a06abdc) |
| medium | F04/F05: «⚙ буфер» прятал хвост и переводы строк; Tab в подтверждении ввода выглядел пробелами | ⏎, ⇥ и «… (ещё N симв.)» (a06abdc) |
| medium | F08: .py/.sh/.theme/.rdp/.wsb/.xll/.iso и др. открывались мозгом без вопроса | в активные типы (a06abdc) |
| low | F02 data ответов MCP без privacy; F06 IDN-хост в подтверждении; F07 оболочки под другими именами; F10 журнал при store_text=false; F11 терминалы для type_text; F12 трамплин dev-запуска | исправлено, tests/test_s8_security.py (a06abdc) |

## Идеи из аналогов (только идеи, без кода)
Источники: RustamovAkrom/JARVIS (лицензия проприетарная — код не брать), jarvis.institute («Jarvis AI Assistant»
для Windows, по описаниям на сайте и в Microsoft Store), jarvis.cx (браузерный/десктопный копилот).
- Сделано в этой сессии: отмена последнего обратимого действия («отмени»); напоминания («напомни через
  20 минут …», уведомление в трее и вслух); память о пользователе («запомни …», «забудь …»; пишет только человек,
  мозгу — в контексте через privacy); «что ты умеешь» без модели; журнал инструментов мозга и сводка в stats;
  анимированная сфера (Монарх теней) в окне и на рабочем столе; голос — push-to-talk, распознавание и ответ голосом
  локально (S9); зрение — снимок окна или экрана уходит GPT только по явной команде человека, у мозга такого
  инструмента нет.
- В план: слово-активатор «Джарвис» (русской KWS-модели в sherpa-onnx нет; английская находит ≤3/18); «переведи /
  исправь текст из буфера»; окно-сводка статистики; «аудит облака» — что ушло мозгу по каждому запросу.
- Не берём: веб-дашборд на 0.0.0.0 и удалённое управление (Telegram/MQTT) — противоречит «нет TCP/HTTP-слушателей»;
  shell и запуск кода для ИИ — запрещено политикой; плагины в каталоге — поверхность атаки; веб-камера; снимки
  экрана по инициативе ИИ; расписания, которые сами исполняют действия, — без человека в контуре это дыра.

## Голос (S9): модели и замеры в облаке (Linux, Xeon 2,1 ГГц, 4 vCPU, sherpa-onnx 1.13.8)
- Модели — релизы k2-fsa/sherpa-onnx: asr-models `sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16.tar.bz2`
  (160 МБ; encoder.int8.onnx, decoder.onnx, joiner.onnx, tokens.txt — 33 буквы + пробел, без цифр и пунктуации;
  `model_type="nemo_transducer"`, `feature_dim=64`), `…punct-giga-am-v3…` (v3 e2e: пунктуация, заглавные, цифры),
  `…giga-am-v2-russian-2025-04-19`, `sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16` (23 МБ, онлайн,
  80 признаков), `silero_vad.onnx` (0,6 МБ); tts-models `vits-piper-ru_RU-{denis,dmitri,ruslan,irina}-medium` (64 МБ).
  Лицензии: GigaAM — MIT, Vosk small — Apache-2.0, Silero VAD — MIT, piper denis/dmitri — датасет CC0, ruslan —
  CC BY-NC-SA 4.0. Hugging Face из облака недоступен; v3 на GitHub оказался тем же экспортом (export-onnx-rnnt-v3.py).
- Распознавание фразы ≈3,75 с (3 с речи + тишина, VAD): v3 250–340 мс (2–4 потока), VAD 20–30 мс; v2 ≈500 мс;
  vosk-small ≈200 мс. Загрузка модели 1,0–1,6 с, прогрев — первым декодированием.
- `scripts/voice_bench.py`: 20 команд синтезом piper (+шум 0,003, тишина 0,35/0,5 с), роутер и грамматика на
  распознанном тексте — «то же, что набранное»: gigaam-v3 18/20 (denis, 4 прогона), 19/20 (ruslan), p50 175–232 мс,
  p95 209–317 мс (запись 2,3–2,6 с, 2–4 потока); v3-punct 16–17/20 («?» уводит вопрос к мозгу, обращение — «деви»/«шарвес»);
  v2 19/20, но p50 290–330, p95 413–526 мс; vosk-small 10/20, p50 95 мс. 4 потока в облаке не быстрее (CPU занят
  соседями). По умолчанию — gigaam-v3, 2 потока. VITS случаен (noise_scale) — повтор даёт другие ошибки.
- Озвучка: piper medium fp32 — синтез фразы p50 120–140 мс, максимум 250–400 мс (RTF ≈0,08, 2 потока); int8-версии
  медленнее (RTF ≈0,2). Синтез → распознавание обратно 20 фраз: denis 16/20, ruslan 17/20, dmitri 13/20 (глотает
  начало). Латиница читается плохо («Открыл Telegram» → «отрыв») — tts.speakable транслитерирует.
- Слово-активатор не сделано: английская KWS `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` с ключом JARVIS
  (5 написаний BPE, порог 0,02–0,25, boost до 4) на синтезе «Джарвис, …» трёх голосов нашла ≤3/18 (ложных 0/45);
  русской KWS в релизах нет. Обращение «Джарвис» в начале фразы при PTT срезается (asr.strip_wake).
- sherpa-onnx 1.13.8: VoiceActivityDetector.accept_waveform всем буфером разом находит мусорный сегмент в конце —
  кормить окнами по 512 отсчётов. OfflineTts.generate с колбэком требует numpy — колбэк не используется.

## Журнал сессии — подробно (перенесён из AGENTS.md: лимит 32 КБ для Codex)
- S0 (облако, NIGHT-PROMPT): Linux-контейнер, ветка jarvis-v1 от 66dc61d; scratch\, сервера рук, Everything и входа
  в GPT нет — факты задания 00 взяты из NIGHT-PROMPT §2. uv 0.11, Python 3.12, openai-codex 0.160.1, mcp 1.30.
- S1 начат: каркас и контракты (docs/architecture.md); `[tool.uv] environments` — win32 и linux; mcp>=1.29,<2;
  PySide6-Essentials; эталонные scripts/brain_smoke.py и es_check.py исключены из ruff (держим как проверены).
- Модули писали параллельные субагенты по контрактам docs/architecture.md; главный поток — интеграция, коммиты.
- S1: paths (зоны, 8.3, junction, хвостовые точки, устройства), privacy, apps (resolve 0 промахов на 69 фразах,
  0,1 мс). GitHub Actions: «account is locked due to a billing issue» — CI не стартует; Windows-проверка — Wine.
- S2a: windows (лестница focus, Alt+Tab-фильтр), procs (own_pids из JARVIS_ROOT_PID), files (единственный startfile,
  es.exe argv из спеки), system (type_text с проверкой окна перед каждой порцией), audio (COM-поток), media, _input.
- S2b: ConfirmServer (рукопожатие в потоке соединения), клиент с проверкой адреса; FastMCP 18 инструментов, старт 0,7 с.
- S3: руки (префикс байт-в-байт, ~1300 токенов по оценке; maxLength 80; правило kill; пример приветствия/папки),
  execute (цель из текста или то же приложение инвентаря; тема для media/vol/win; open↔focus). Доводка — по live.
- S4: грамматика 30 шаблонов (match 0,02 мс), роутер, корпус 150 фраз (уровень 150/150, 0 ложных грамматики);
  core (Level → … → один Done; ответ рук ask_gpt → второй Level), CLI (UTF-8 в pipe, контекст в cli_context.json).
- S5: Brain (overrides сверены с настоящим codex 0.160.1; запасной тред; прокладка subprocess; обрыв без сети 30 с);
  wire_api и ошибки конфига — в notes. Live-тесты в conftest пропускаются без явного `-m live`.
- S7: журнал (фоновая запись), stats с бюджетами, bench (dry, корпус + фикстура инвентаря), doctor (17 проверок;
  в local codex не запускается).
- S6a: app (хоткей через nativeEventFilter на окне-приёмнике, Job Object + потомки llama-server, mutex и показ первого
  экземпляра, трей 4 состояния, Brain.close только в рабочем потоке), иконка кодом (QPainter), автозапуск HKCU Run.
- S6b: окно-лаунчер (анимации 140/90/120 мс, покой без таймеров, PlainText везде, подтверждение 700 мс/Ctrl+Enter,
  автоповтор Enter/Esc игнорируется), снимки — scripts/ui_screenshots.py (docs/screenshots/ в .gitignore).
