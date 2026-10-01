-- 002: запросы подтверждения и журнал аудита (04-security.md §6, §8; Session 3).

CREATE TABLE approvals (
  id           TEXT PRIMARY KEY,           -- task_42.appr_1
  task_id      TEXT NOT NULL REFERENCES tasks (id),
  seq          INTEGER NOT NULL,           -- порядок запросов внутри задачи
  tool_call_id TEXT NOT NULL,
  status       TEXT NOT NULL,              -- меняется сравнением со старым значением
  request_json TEXT NOT NULL,
  UNIQUE (task_id, seq)
) STRICT;

CREATE TABLE audit_log (                   -- только добавление
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  action       TEXT NOT NULL,              -- decision | result | approval
  record_json  TEXT NOT NULL
) STRICT;

CREATE INDEX audit_log_task ON audit_log (task_id, seq);
