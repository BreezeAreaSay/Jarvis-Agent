# Architecture Decision Records

Значимые решения проекта. Новое решение — новый файл со следующим номером; принятое решение не
переписывается, а заменяется новым ADR со ссылкой «заменяет ADR NNNN».

## Формат

```markdown
# ADR NNNN. Заголовок

- **Статус:** Предложено | Принято | Заменено ADR NNNN | Отклонено
- **Дата:** ГГГГ-ММ-ДД

## Контекст        (Context)
## Решение         (Decision)
## Альтернативы    (Alternatives)
## Последствия     (Consequences)
```

«Принято» — решение, зафиксированное владельцем проекта; «Предложено» — ждёт его решения. ADR 0001–0020
приняты как **Stage 0 baseline** (2026-10-01, тег `stage0-baseline`). Дальше решение меняется только
новым ADR — когда код или замеры обнаруживают реальную проблему.

## Список

| ADR | Решение | Статус |
| --- | --- | --- |
| [0001](0001-python-single-package.md) | Python 3.12+, один пакет, uv | Принято |
| [0002](0002-ports-and-adapters.md) | Порты и адаптеры; правило зависимостей проверяется автоматически | Принято |
| [0003](0003-sqlite-storage.md) | SQLite: один файл, WAL, синхронный sqlite3, Unit of Work | Принято |
| [0004](0004-tool-runtime-security-boundary.md) | Tool Runtime — граница безопасности; риск вычисляется для вызова | Принято; уточняется [0029](0029-specialist-agents.md) |
| [0005](0005-shell-escape-hatch.md) | Shell — escape hatch | Принято |
| [0006](0006-untrusted-content-is-data.md) | Недоверенный контент — данные, а не инструкции | Принято; уточняется [0028](0028-cloud-privacy-boundary.md), [0030](0030-direct-actions-and-launch-policy.md) |
| [0007](0007-skills-vs-recipes.md) | Skills ≠ Recipes; SkillProvider и NullSkillProvider | Принято; уточняется [0026](0026-orchestration-router-and-strategies.md) |
| [0008](0008-model-gateway.md) | Model Gateway и матрица возможностей | Принято; уточняется [0026](0026-orchestration-router-and-strategies.md), [0027](0027-hybrid-model-providers.md) |
| [0009](0009-structured-model-output.md) | Структурированный вывод модели; схема проверяется до политики | Принято; уточнён [0023](0023-model-gateway-v1.md), [0026](0026-orchestration-router-and-strategies.md) |
| [0010](0010-task-state-machine.md) | Явная машина состояний и пошаговый runner | Принято |
| [0011](0011-human-approval-as-task-state.md) | Подтверждение человеком — состояние задачи | Принято |
| [0012](0012-identifiers.md) | Идентификаторы | Принято |
| [0013](0013-async-at-io-boundaries.md) | asyncio только на границах ввода-вывода | Принято |
| [0014](0014-error-taxonomy.md) | Таксономия ошибок и диспозиции | Принято |
| [0015](0015-trace-without-chain-of-thought.md) | Трасса без chain-of-thought | Принято |
| [0016](0016-dry-run-and-replay.md) | Dry run и replay | Принято |
| [0017](0017-windows-first-execution-target.md) | Windows-first и ExecutionTarget | Принято |
| [0018](0018-config-layers.md) | Слои конфигурации | Принято; уточняется [0027](0027-hybrid-model-providers.md) |
| [0019](0019-no-daemon-in-stage0.md) | Без демона в Stage 0 | Принято; срок пересматривается по замеру (Architecture V2, 04 §6) |
| [0020](0020-eval-first.md) | Eval с первого milestone и бенчмарк до выбора модели | Принято |
| [0021](0021-task-leases-and-optimistic-unit-of-work.md) | Аренды задач и оптимистическая единица работы (M2) | Принято |
| [0022](0022-tool-runtime-v1.md) | Tool Runtime v1: эффекты вызова, политика по зонам, подтверждение по отпечатку preview | Принято; уточняется [0030](0030-direct-actions-and-launch-policy.md) |
| [0023](0023-model-gateway-v1.md) | Model Gateway v1 и первый агент на модели: роль executor, решения по возможностям, llama-server | Принято; уточняется [0027](0027-hybrid-model-providers.md), [0028](0028-cloud-privacy-boundary.md) |
| [0024](0024-agent-benchmark.md) | Бенчмарк агента на настоящей модели: методика | Принято (методика) |
| [0025](0025-local-model-choice.md) | Основная локальная модель — Qwen3-8B Q4_K_M; архитектура B | Принято |
| [0026](0026-orchestration-router-and-strategies.md) | Router ядра решает стратегию исполнения: DIRECT, RECIPE, AGENT, DELEGATE, CLARIFY | Предложено |
| [0027](0027-hybrid-model-providers.md) | Гибридные провайдеры моделей: уровни, цепочки, состояние, ограниченный fallback | Предложено |
| [0028](0028-cloud-privacy-boundary.md) | Граница приватности облака: классы данных, согласие, аудит выхода данных | Предложено |
| [0029](0029-specialist-agents.md) | Внешние агенты-специалисты: отдельный порт, Tool Runtime, копия репозитория | Предложено |
| [0030](0030-direct-actions-and-launch-policy.md) | Прямые действия: эффект LAUNCH и политика по исполнителю вызова | Предложено |

ADR 0025–0030 — Architecture V2 ([docs/architecture-v2](../architecture-v2/README.md)): выбор модели
принят владельцем; остальные предложены и ждут решения. «Уточняется» в таблице значит, что более новый
ADR меняет часть решения, а остальное действует (уточнение предложенным ADR вступает в силу, когда его
примут); принятый текст старых ADR не переписывается.
