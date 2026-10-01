# Handoff — Session 2: M2 (SQLite, аренды, восстановление, CLI)

Ветка `claude/elegant-bohr-d8y2sb`; `main` не трогали. Состояние на конец сессии: все локальные
проверки зелёные. GitHub Actions по-прежнему завершается `startup_failure` до старта jobs — это
внешняя проблема настроек репозитория или аккаунта; на Windows код пока не запускался.

## Implemented

- **`adapters.sqlite`** — один файл `JARVIS_HOME/data/jarvis.db` в режиме WAL, стандартный `sqlite3`,
  явный SQL, без ORM. Пул соединений: у каждой единицы работы своё соединение.
- **Оптимистическая единица работы** (оба адаптера): чтения — один снимок, записи копятся и применяются
  на `commit()` короткой транзакцией `BEGIN IMMEDIATE`; ожидания (версия задачи, строка аренды,
  отсутствие дубликата) — условия в самих запросах; расхождение — `ConcurrentModification`, ничего не
  записано. InMemory повторяет семантику (снимок при первом чтении, проверки при commit).
- **Миграции** — `src/jarvis/adapters/sqlite/migrations/NNN_имя.sql`, без Alembic.
- **Аренды задач** между процессами, heartbeat, восстановление прерванных задач, отмена между
  процессами с `TaskBusy`.
- **Контрольная точка** — задача, событие перехода, объясняющие события (`error`, `budget.exceeded`,
  `task.finished`) и сравнение-и-запись аренды — одной транзакцией.
- **Метрики из событий** (`core.metrics`): длительность, активное время, переходы, ошибки,
  перепланирования; счётчики модели, инструментов и токенов — нули до появления их событий.
- **Таймлайн** (`core.timeline`) — шаблоны над событиями, без LLM.
- **CLI:** `jarvis tasks [-s running|waiting|finished|<статус>] [-n N]`, `jarvis trace <id> [--json]`,
  `jarvis cancel <id> [--reason]`. Каждая команда сначала восстанавливает задачи умерших процессов;
  ошибки базы — объяснение без traceback.
- **Фундамент replay:** `run_scenario(..., storage=)` сохраняет scripted-прогон; `normalize_events`
  убирает ID, время и пропуски нумерации для сравнения прогонов.
- **Понятные ошибки базы:** не SQLite, чужая база, повреждённая схема (таблицы, индексы, ограничения),
  повреждённые страницы, упавшая миграция, база новее кода, нет прав, база занята, нет места на диске.
  Ничего не «чинится».

## Database schema

Версия схемы — **1** (`001_initial.sql`). Таблицы:

| Таблица | Назначение |
| --- | --- |
| `schema_migrations` | применённые миграции (`version`, `name`, `applied_at`) |
| `tasks` | `id`, `seq`, `version`, `status`, `route`, `request_json`, `budget_json`, `usage_json`, `outcome_json`, `created_at`, `updated_at`; индекс `(status, seq)` |
| `trace_events` | `id`, `task_id` → tasks, `seq`, `ts`, `kind`, `v`, `payload_json`; `UNIQUE(task_id, seq)` |
| `id_counters` | счётчики ID (`scope`, `kind`, `last`); пишутся сразу, вне контрольной точки |
| `task_leases` | `task_id` → tasks, `owner`, `expires_at` |

Все таблицы `STRICT`. Миграции: недостающие — одной транзакцией под блокировкой записи (версия
перечитывается там же), внешние ключи на время миграции выключены и проверяются `foreign_key_check`
перед COMMIT, копия `jarvis.db.vN.bak` до обновления (не перезаписывается), затем SQL всех таблиц и
индексов сверяется с эталоном.

## Concurrency model

- Писатель SQLite — короткая транзакция только на `commit()`; ни одна единица работы не живёт через
  `await`. Читатели в WAL не блокируют писателя и видят согласованный снимок.
- Ожидание блокировки — `busy_timeout` (5 с); первое открытие новой базы повторяет переход в WAL,
  потому что SQLite не применяет к нему `busy_timeout`.
- ID выдаются отдельной мгновенной записью (`INSERT … ON CONFLICT DO UPDATE … RETURNING`).
- В одном процессе задачу ведёт один прогон (`TaskBusy` на второй).

## Lease semantics ([ADR 0021](../adr/0021-task-leases-and-optimistic-unit-of-work.md))

- Аренда = владелец (`хост:pid:токен`) + срок; срок — `runtime.lease_ttl_s` (30 с).
- `submit` пишет задачу, событие создания и аренду одной транзакцией.
- Прогон продолжает свою аренду только у задачи в CREATED и сначала продлевает её
  сравнением-и-записью. Каждая контрольная точка продлевает аренду тем же способом (ограждение
  записи); heartbeat продлевает её каждую треть срока; выход из активных состояний удаляет аренду.
- Чужая живая аренда — `TaskBusy` для прогона и отмены.
- Активная задача без живой аренды (процесс умер) или со своей арендой не в CREATED (прошлый прогон
  этого процесса оборвался) → FAILED, категория `interrupted`, поле перехода
  `interruption = owner_lost`. Прогон, остановленный посреди такта, → FAILED (`run_stopped`) или
  CANCELLED, если клиент просил отмену; запись этого итога не маскирует `CancelledError`.
- Процесс, потерявший аренду, ничего не запишет: heartbeat → `LeaseLost`, контрольная точка → не
  проходит сравнение.
- Различимы: сбой процесса (`interrupted`), сбой задачи (своя категория), отмена человеком
  (`task_cancelled`, CANCELLED), бюджет (`budget_exceeded`, BUDGET_EXCEEDED).

## Tests

458 тестов (457 проходят, 1 пропущен: тест файла только для чтения не имеет смысла под root).
Повторные прогоны (3×) без нестабильности.

- `tests/contract/test_storage.py` — один набор для InMemory и SQLite: CRUD, сериализация, дубликаты,
  атомарность, список задач, трасса, аренды (сравнение-и-запись, гонка двух владельцев, устаревшее
  удаление против продления), согласованный снимок, ID.
- `tests/contract/test_task_leases.py` — аренды в работе ядра на обоих адаптерах: живая аренда не
  пускает другие процессы, восстановление умершего, старый владелец не пишет после перехвата,
  heartbeat обнаруживает перехват и держит долгий такт, сбой heartbeat не подменяет результат.
- `tests/integration/test_restart.py` — каждый сценарий в отдельном процессе на SQLite, новый
  процесс восстанавливает ту же задачу и нормализованную трассу; убитый процесс: `TaskBusy` до
  истечения аренды, затем FAILED (`interrupted`).
- `tests/integration/test_sqlite_concurrency.py` — ID из чередующихся соединений и из 4 процессов,
  одновременное первое открытие, восстановление каждой задачи ровно одним из 4 процессов,
  согласованное чтение во время записи, ожидание короткой блокировки.
- `tests/integration/test_sqlite_failures.py` — повреждённые и чужие базы, миграции (обновление с
  копией, откат упавшей, перестройка таблицы со связями, нарушенные связи, запоздалое обновление не
  трогает копию), утрата ограничений, диск полон, блокировка, права.
- `tests/integration/test_performance.py` — 3000 событий в задаче и 300 задач на обоих адаптерах;
  запись не замедляется с ростом трассы (SQLite: 3000 событий ≈ 0,4 с).

## Evals

`jarvis eval` — 5 из 5 (`runtime.*`). Отчёты `.json` и `.md` пишутся в `evals/reports/`. Новых
сценариев в M2 нет: механика хранения и аренд проверяется тестами выше, а все пять сценариев
дополнительно прогоняются через SQLite и перезапуск процесса (`test_restart.py`).

## Known limitations

- **CI:** `startup_failure`; на Windows код не проверен. Тест прав доступа пропускается под root.
- **Часы:** аренды по системным часам; скачок часов вперёд на срок аренды (NTP, пробуждение ВМ)
  заставит CLI восстановить живую задачу как прерванную. Данные при этом согласованы.
- **Срок аренды и блокировки:** heartbeat работает в том же цикле событий, что и стадии; стадия,
  блокирующая цикл дольше срока аренды, или ожидание блокировки базы (до 5 с) может уронить живую
  аренду. Срок по умолчанию (30 с) с запасом больше `busy_timeout`, но это не проверяется.
- **`submit` без прогона:** аренда, взятая при `submit`, не продлевается до начала прогона; задачу,
  которую не запустили в течение срока аренды, любая команда CLI переведёт в FAILED (`interrupted`).
- После падения процесса его задача до истечения аренды (до 30 с) видна как занятая (`TaskBusy`).
- WAITING_CONFIRMATION пока покидается только отменой: `resolve_approval` — следующая сессия.
- InMemory не проверяет `UNIQUE(task_id, seq)` отдельно (ядро не может его нарушить: ID и `seq`
  связаны моделью события).
- `jarvis eval` не пишет прогоны в пользовательскую базу (только в память) — намеренно.

## Architecture deviations

- Порт `TaskLeases` из эскиза 03 заменён репозиторием аренд внутри единицы работы и правилами в
  `core.leases` — ADR 0021.
- События, объясняющие переход (`error`, `budget.exceeded`, `task.finished`), — часть контрольной
  точки, а не журнальные записи; журнальный `Tracer.emit` убран до появления инструментов.
- Активную задачу больше не продолжают с контрольной точки после обрыва прогона (в M1 тест это
  допускал): она становится FAILED (`interrupted`). Возобновление — только из WAITING_CONFIRMATION.
- `TaskService.cancel` возвращает снимок задачи после попытки (в эскизе 03 — `None`).
- Схема `tasks` — только поля с потребителем; эскизные столбцы следующих milestone не созданы.

## ADR changes

- **Новый [ADR 0021](../adr/0021-task-leases-and-optimistic-unit-of-work.md)** — аренды задач и
  оптимистическая единица работы, с уточнениями по итогам ревью.
- Обновлены 02-domain, 03-contracts, 05-storage-and-trace, 06-errors-and-config, 08-milestones,
  `docs/development.md`.

## Review

Независимое ревью M2 нашло и подтвердило пробами: перезапись копии базы при параллельном обновлении;
повторное выполнение такта тем же процессом после оборванного прогона; классификацию активного времени
по тексту причины; потерю итога при выходе во время остановки такта по лимиту времени; маскировку
ошибки «диск полон»; непроверяемые ограничения схемы; неверный `TaskBusy` при отмене; сбой heartbeat,
подменяющий результат; утечку соединений при закрытии. Всё исправлено с регрессионными тестами,
которые падают на старом коде.

Попутно: проба ревьюера случайно создала `tmpm5_tl0x4/` в корне репозитория (удалить её ревьюеру не
дали права); папка перенесена в scratchpad сессии, в репозиторий не попала.

## Commits

- `77cf753` feat(domain): add task lease, active statuses and lease errors
- `a673144` feat(storage): add lease repository and task listing to storage ports
- `1d06c1e` feat(storage): add sqlite storage with schema migrations
- `870bf2f` feat(runtime): compute task metrics from trace events
- `08ad260` feat(runtime): add task leases, recovery and cross-process cancel
- `58decfc` fix(runtime): do not count downtime before an interruption as active time
- `8b1a6a6` feat(cli): add tasks, trace and cancel commands
- `186dbe7` fix(storage): retry the WAL switch while another connection converts a new database
- `48dbce9` feat(evals): persist scripted runs and normalize traces for replay
- `7061f44` test(storage): cover corrupted databases and migrations
- `c048f07` test(storage): add performance sanity checks
- `afc7659` docs: add ADR 0021 and sync Stage 0 docs with M2
- `9e3fa92` docs: name the implemented trace --json option
- `f6ec8ab` fix(runtime): never let a shutdown write failure mask the cancellation
- `a3188ee` fix(runtime): close lease and shutdown gaps found in the M2 review
- `80df5e1` fix(storage): harden migrations, schema check and error reporting
- `b6d2e02` refactor: drop App.owner and widen real-time test margins
- `babf764` docs: record the M2 review rules in ADR 0021 and the storage spec

## Next recommended milestone

**Session 3 — Tool Runtime + Security Foundation** (M5 в нумерации Stage 0, без shell и LLM): домен
инструментов, реестр, конвейер `ToolRuntime` (preview → policy → execute → verify → trace/audit),
Policy Engine v1, подтверждения и `resolve_approval`, аудит, read-only инструменты файловой системы
и процессов, scripted-стадия, вызывающая инструменты, eval-сценарии `tool.*`.

Стартовая точка: `uv sync && uv run pytest`; ядро — `src/jarvis/core/runner.py` (такт, контрольная
точка, аренды), `src/jarvis/core/service.py` (API), хранилище — `src/jarvis/adapters/sqlite/`.
