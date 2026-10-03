-- 003: рабочая память агента и вызовы модели (05-storage-and-trace.md §1; Session 4, ADR 0023).

ALTER TABLE tasks ADD COLUMN state_json TEXT;   -- AgentState; NULL — задачу вёл не агент

CREATE TABLE model_calls (                -- только добавление
  id          TEXT PRIMARY KEY,           -- task_42.mc_3
  task_id     TEXT NOT NULL REFERENCES tasks (id),
  seq         INTEGER NOT NULL,           -- порядок вызовов внутри задачи
  created_at  TEXT NOT NULL,
  status      TEXT NOT NULL,              -- ok | invalid | error | cancelled
  record_json TEXT NOT NULL,              -- ModelCallRecord: промпт, ответ, токены, тайминги
  UNIQUE (task_id, seq)
) STRICT;
