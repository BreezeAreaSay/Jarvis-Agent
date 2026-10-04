-- 005: состояние провайдеров моделей по итогам вызовов (ADR 0027): следующая команда CLI не бьёт в
-- провайдера с исчерпанным лимитом или неверным ключом. Не история: одна строка на провайдера.

CREATE TABLE provider_states (
  provider     TEXT PRIMARY KEY,           -- ID эндпоинта из конфига
  status_json  TEXT NOT NULL,              -- ProviderStatus: состояние, причина, срок
  updated_at   TEXT NOT NULL
) STRICT;
