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
