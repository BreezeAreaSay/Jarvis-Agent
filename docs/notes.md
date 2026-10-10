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
