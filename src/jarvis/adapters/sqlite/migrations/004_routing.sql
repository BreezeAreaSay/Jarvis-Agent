-- 004: решение Router о стратегии исполнения (Architecture V2, ADR 0026).

ALTER TABLE tasks ADD COLUMN routing_json TEXT;   -- RouteDecision; NULL — задача без решения Router
