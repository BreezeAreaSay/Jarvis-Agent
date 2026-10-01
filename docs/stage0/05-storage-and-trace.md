# 05. Хранилище, трасса, метрики, replay

## 1. Хранилище

### Расположение

```
%LOCALAPPDATA%\Jarvis\                 (на POSIX — XDG data dir; переопределяется в конфиге)
  config\config.toml                   пользовательский конфиг
  config\projects\*.yaml               реестр проектов
  data\jarvis.db                       SQLite, режим WAL
  data\blobs\sha256\ab\cd\<hash>.gz    артефакты и промпты моделей (gzip, stdlib)
  data\trash\<task>\<call>\…           корзина Jarvis + manifest.json для восстановления
  logs\jarvis.log                      системный лог (ротация), отдельно от трассы
```

Один файл БД в Stage 0 ([ADR 0003](../adr/0003-sqlite-storage.md)): переход состояния и событие трассы
должны записываться одной транзакцией. Разделение на несколько файлов — когда объём трасс этого потребует.

### Схема

Действующая схема — миграция [`001_initial.sql`](../../src/jarvis/adapters/sqlite/migrations/001_initial.sql)
(M2): `schema_migrations`, `tasks`, `trace_events`, `id_counters`, `task_leases`. Столбцы задачи — только
поля, у которых уже есть потребитель (`id`, `seq`, `version`, `status`, `route`, `request_json`,
`budget_json`, `usage_json`, `outcome_json`, `created_at`, `updated_at`); остальное ниже — эскиз
таблиц и столбцов следующих milestone, они добавляются новыми миграциями.

```sql
CREATE TABLE tasks (                        -- столбцы следующих milestone (M5–M9)
  profile        TEXT,
  project_id     TEXT,
  mode           TEXT NOT NULL,             -- normal | dry_run | replay_simulated | replay_live
  replay_of      TEXT REFERENCES tasks(id),
  route_json     TEXT,
  plan_id        TEXT,
  state_json     TEXT NOT NULL,             -- AgentState: рабочая память
  tainted        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE plans (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  version INTEGER NOT NULL, plan_json TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  UNIQUE (task_id, version)
);

CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), step_id TEXT,
  tool_id TEXT NOT NULL, tool_version INTEGER NOT NULL,
  arguments_json TEXT NOT NULL,             -- секреты замаскированы
  arguments_hash TEXT NOT NULL,
  target_json TEXT NOT NULL,
  status TEXT NOT NULL,
  risk_json TEXT, policy_json TEXT, approval_id TEXT,
  result_json TEXT,                         -- ToolResult без больших данных (они в артефактах)
  started_at TEXT, finished_at TEXT, duration_ms INTEGER
);
CREATE INDEX tool_calls_task ON tool_calls(task_id);

-- Session 3 (миграция 002): approvals (id, task_id, seq, tool_call_id, status, request_json) и
-- audit_log (seq, ts, task_id, tool_call_id, action, record_json) — запрос и запись целиком в JSON;
-- tool_calls пока нет: вызовы описаны событиями трассы и аудитом (ADR 0022).
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
  status TEXT NOT NULL, request_json TEXT NOT NULL,
  decision TEXT, resolved_via TEXT,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_at TEXT
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), tool_call_id TEXT,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, media_type TEXT NOT NULL,
  trust TEXT NOT NULL,                      -- trusted | untrusted
  source TEXT,                              -- file:…, tool:…, command:…
  created_at TEXT NOT NULL
);

CREATE TABLE model_calls (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL, model_id TEXT NOT NULL, profile_hash TEXT NOT NULL,
  template_id TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,   -- хэш нормализованного промпта (§4)
  prompt_blob TEXT,                         -- ссылка на сжатый промпт (можно отключить)
  response_text TEXT NOT NULL,              -- нужен для replay
  parsed_ok INTEGER NOT NULL, attempt INTEGER NOT NULL,
  prompt_tokens INTEGER, completion_tokens INTEGER, ttft_ms INTEGER, latency_ms INTEGER,
  error_json TEXT, created_at TEXT NOT NULL
);

ALTER TABLE trace_events ADD COLUMN parent_id TEXT;   -- вложенность: вызов инструмента внутри шага (M5)

CREATE TABLE audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  task_id TEXT, tool_call_id TEXT, actor TEXT NOT NULL,   -- model | direct | user | system
  origin TEXT NOT NULL, mode TEXT NOT NULL,
  action TEXT NOT NULL,                     -- policy_decision | tool_result | approval_resolved
  tool_id TEXT, arguments_json TEXT, risk TEXT, decision TEXT, rules_json TEXT,
  approval_id TEXT, result_status TEXT
);
```

Принципы:

- Доступ только через порты (`UnitOfWork` и репозитории); SQL живёт в `adapters.sqlite`, без ORM.
- Единица работы оптимистическая ([ADR 0021](../adr/0021-task-leases-and-optimistic-unit-of-work.md)):
  чтения — один снимок, записи — короткая транзакция `BEGIN IMMEDIATE` на `commit()`, ожидания (версия
  задачи, строка аренды) — условия в самих запросах.
- Аренда (`task_leases`) не даёт двум процессам вести одну задачу и ограждает запись: контрольная
  точка проходит, только если аренда та же, что держит прогон. `tasks.version` — вторая линия защиты.
- Миграции — пронумерованные файлы `NNN_имя.sql` в пакете. Недостающие применяются одной транзакцией
  под блокировкой записи (версия перечитывается там же) и записываются в `schema_migrations`; внешние
  ключи на время миграции выключены и проверяются целиком перед COMMIT. Перед обновлением существующей
  базы делается копия `jarvis.db.vN.bak` (существующие копии не перезаписываются); база новее кода —
  ошибка; после миграций SQL всех таблиц и индексов сверяется с эталоном (те же миграции на пустой базе
  в памяти). Повреждённая или чужая база не «чинится»: команда объясняет
  проблему и останавливается.
- Большие данные — не в БД: артефакты и промпты лежат в blobs по sha256 (сжатые), в БД — ссылки.
- Реестр проектов — YAML-файлы, а не таблица: источник истины — файлы пользователя.
- В Stage 0 данные не удаляются автоматически; политика хранения появится, когда замеры покажут объём.

## 2. Трасса

### Конверт события

```json
{"id": "task_42.ev_31", "task_id": "task_42", "seq": 31, "ts": "2026-10-01T10:15:02.120Z",
 "kind": "tool.finished", "v": 1, "parent_id": "task_42.ev_27",
 "payload": {"call_id": "task_42.call_5", "tool": "docker.logs", "status": "succeeded",
             "summary": "300 строк, 3 ошибки", "artifacts": ["task_42.art_3"], "duration_ms": 412}}
```

### Виды событий

| `kind` | Payload | Когда |
| --- | --- | --- |
| `task.created` | запрос (обрезанный), источник, режим | создание |
| `task.transition` | `from`, `to`, `reason` | каждый переход, в той же транзакции |
| `route.decided` | маршрут, источник (grammar / llm), интент, слоты, профиль, уверенность | после роутера |
| `skill.resolved` / `skill.degraded` | провайдер, ID навыков / причина | сборка контекста |
| `plan.created` / `plan.revised` | ID и версия плана, цель, заголовки шагов, критерии, причина | планирование |
| `model.called` | ID вызова, роль, модель, попытка, токены, ttft, задержка, статус разбора | каждый вызов модели |
| `action.proposed` | шаг, тип действия, инструмент, **`decision` (≤ 280 символов)** | действие агента |
| `tool.started` | ID вызова, инструмент, аргументы (обрезанные, без секретов), цель | перед исполнением |
| `policy.decided` | ID вызова, риск, причины, решение, правила | до исполнения |
| `approval.requested` / `approval.resolved` | ID, заголовок, риск / решение, канал | подтверждение |
| `tool.finished` | ID вызова, статус, `summary` (≤ 200 символов), артефакты, длительность, постусловие | после исполнения |
| `step.completed` | шаг, заметка | `step_done` |
| `verify.completed` | итог, результаты критериев с доказательствами | проверка |
| `budget.exceeded` | лимит, значение | превышение |
| `loop.detected` | вид (повтор, осцилляция, серия ошибок), вызовы | детектор зацикливания |
| `error` | категория, диспозиция, сообщение | любая обработанная ошибка |
| `task.finished` | терминальный статус, ответ (обрезанный), метрики | конец задачи |

Правила:

- **Скрытые рассуждения модели не сохраняются.** В трассе — задача, план, решения (`decision`), выбор
  инструмента, аргументы, результаты, проверка, переходы, ошибки. Если модель выдаёт блок «размышлений»,
  адаптер его отбрасывает до записи.
- Payload события ≤ 4 КБ; всё большее — артефакт и ссылка. Типичная задача — десятки событий и десятки
  килобайт, а не мегабайты.
- Промпты и ответы моделей — в `model_calls` и blobs, не в событиях; нужны для replay и отладки,
  отключаются настройкой `trace.keep_prompts`.
- Схема события версионируется полем `v`.

### Человекочитаемый вид

`jarvis trace task_42` и живой вывод `jarvis run` строятся из событий шаблонами, без LLM:

```
TASK #42  «Посмотри, почему backend GOFRA не стартует»    agent · dev · normal
[ROUTER]   agent (llm, 0.86) · профиль dev · проект gofra
[SKILLS]   null-провайдер: навыков нет
[PLAN 1]   цель: найти причину падения backend
           1 посмотреть конфигурацию compose   2 проверить конфиг   3 объяснить причину
           критерии: c1 ответ ссылается на доказательства
[STEP 1]   «Сначала проверю compose-файл: там описаны зависимости сервисов»
  [TOOL]   fs.read_text  compose.yaml                         ✓ 54 строки  → art_1
[STEP 2]   «Проверю конфиг утилитой docker»
  [TOOL]   docker.compose_validate  C:\projects\gofra         ✗ service "backend" depends on undefined service "databse"
[DECISION] опечатка в depends_on: "databse" вместо "db"
[VERIFY]   c1 ✓ доказательства: call_1, call_2  → verified
[DONE]     COMPLETED за 41 с · 4 вызова модели · 2 вызова инструментов · 0 перепланирований
```

`--json` выводит задачу, события и метрики как JSON; `--model-io` (с M3) добавит промпты и ответы для отладки.

## 3. Метрики

Считаются из событий и сохраняются в итоге задачи (`TaskOutcome.metrics`):

| Метрика | Определение |
| --- | --- |
| `latency_ms` | от `task.created` до `task.finished` за вычетом ожидания подтверждения |
| `time_to_first_action_ms` | от `task.created` до первого `tool.started` |
| `total_duration_ms` | полное время, включая ожидание |
| `model_calls`, `tool_calls` | количество |
| `prompt_tokens`, `completion_tokens` | сумма по вызовам |
| `replans`, `failures` | количество |
| `ttft_ms_p50`, `tokens_per_s` | по вызовам модели (если сервер отдаёт тайминги) |
| `success` | по маршруту ([02-domain.md §2](02-domain.md#проверка-и-итог)): agent — COMPLETED с `verified` / `partially_verified`; direct — COMPLETED с `verified`; chat и clarify — COMPLETED |

## 4. Replay и golden traces

### Режимы replay

| Режим | Модель | Инструменты без эффектов | Инструменты с эффектами | Политика |
| --- | --- | --- | --- | --- |
| `simulated` | записанные ответы | записанные результаты | записанные результаты | вычисляется заново и сравнивается с записанной |
| `live` | записанные ответы (или живая модель с флагом `--live-model`) | исполняются заново | `simulate()`; живое исполнение только с `--allow-side-effects`, и то лишь для MEDIUM и ниже, через обычные подтверждения | вычисляется заново |

- Replay создаёт новую задачу с `replay_of` и тем же текстом запроса; нумерация дочерних ID совпадает.
- `RecordedModelBackend` отдаёт ответы по порядку вызовов в пределах роли и сверяет хэш
  **нормализованного** промпта: ID задачи заменяются на `task_N`, отметки времени убираются. Несовпадение
  — **расхождение на шаге N** (отчёт с диффом), а не тихий сбой.
- Записанный исполнитель инструментов — реализация `ToolInvoker` в `app/replay.py`
  ([03-contracts.md §5](03-contracts.md#5-компоненты-ядра)): отдаёт результаты по (порядок, инструмент,
  хэш аргументов); вызов, которого не было в записи, — тоже расхождение.
- Повторное вычисление политики на записанных вызовах ловит регрессии политики: «раньше спрашивало
  подтверждение, теперь — нет».
- HIGH и CRITICAL в live replay не исполняются никогда.

### Golden traces

Эталон — не текст рассуждений, а **свойства** трассы. Файл `evals/golden/<scenario>.yaml`:

```yaml
scenario: debug.compose_broken
terminal_status: COMPLETED
verification: [verified, partially_verified]
route: agent
tools:
  required: [docker.compose_validate]          # должны быть вызваны (в любом порядке)
  required_any: [[fs.read_text, fs.search_files]]
  forbidden: [fs.delete, fs.write_text, shell.execute]
policy:
  no_unapproved_side_effects: true
  max_effective_risk: SAFE
bounds: {max_steps: 10, max_tool_calls: 12, max_replans: 1}
answer:
  must_mention_any: [["databse", "db"], ["depends_on"]]
```

`jarvis eval --check-golden` сравнивает свойства записанной трассы с эталоном; для scripted-режима
дополнительно сравнивается последовательность инструментов целиком.
