-- 001: задачи, трасса, счётчики ID, аренды (05-storage-and-trace.md §1, ADR 0021).

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE tasks (
  id           TEXT PRIMARY KEY,           -- task_42
  seq          INTEGER NOT NULL UNIQUE,    -- 42: порядок задач
  version      INTEGER NOT NULL,           -- оптимистическая блокировка
  status       TEXT NOT NULL,
  route        TEXT,
  request_json TEXT NOT NULL,
  budget_json  TEXT NOT NULL,
  usage_json   TEXT NOT NULL,
  outcome_json TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
) STRICT;

CREATE INDEX tasks_status_seq ON tasks (status, seq);

CREATE TABLE trace_events (
  id           TEXT PRIMARY KEY,           -- task_42.ev_31
  task_id      TEXT NOT NULL REFERENCES tasks (id),
  seq          INTEGER NOT NULL,           -- порядок внутри задачи
  ts           TEXT NOT NULL,
  kind         TEXT NOT NULL,
  v            INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE (task_id, seq)
) STRICT;

CREATE TABLE id_counters (                 -- IdAllocator: пишется сразу, вне контрольной точки
  scope TEXT NOT NULL,                     -- "task" или ID задачи
  kind  TEXT NOT NULL,                     -- task | plan | step | mc | call | art | appr | ev
  last  INTEGER NOT NULL,
  PRIMARY KEY (scope, kind)
) STRICT, WITHOUT ROWID;

CREATE TABLE task_leases (                 -- кто сейчас ведёт задачу; меняется сравнением со старым
  task_id    TEXT PRIMARY KEY REFERENCES tasks (id),
  owner      TEXT NOT NULL,
  expires_at TEXT NOT NULL
) STRICT;
