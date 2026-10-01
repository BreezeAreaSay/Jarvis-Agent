# Handoff — Session 3: Tool Runtime + Security Foundation

Работа шла в `main` (по просьбе пользователя ветка `claude/elegant-bohr-d8y2sb` влита в `main`
перемоткой, дальше — коммиты прямо в `main`). Все локальные проверки зелёные. GitHub Actions по-прежнему
завершается `startup_failure` до старта jobs (внешняя проблема); на Windows код пока не запускался.
Модель не подключалась: ни Ollama, ни llama.cpp, ни промптов.

## Goal

Дать Jarvis первый реальный, безопасный и детерминированный способ взаимодействовать с компьютером:
Task → Stage → ToolCall → Registry → Preview → Policy → Execution → Verification → Trace/Audit → Result,
где Tool Runtime — единственная граница для любых будущих побочных эффектов.

## Implemented

- **Домен инструментов** (`domain/tools.py`): `ToolId`, `ToolCallId`, `ToolDefinition`, `ToolCall`,
  `ToolPreview` (+ отпечаток), `ToolEffect`/`EffectKind`, `ToolResult`, `ToolVerification`,
  `PolicyDecision`, `ToolOutcome`, `ExecutionTarget`/`TargetKind`. Подтверждения
  (`domain/approvals.py`), аудит (`domain/audit.py`), сравнение путей без ввода-вывода (`domain/paths.py`).
- **Ошибки** расширены: `ToolNotFound`, `InvalidToolArguments`, `ToolPreviewFailed`, `UnsupportedTarget`,
  `ToolDenied`, `ApprovalRequired`, `ToolExecutionFailed`, `ToolTimeout`, `ToolCancelled`,
  `ToolVerificationFailed`, `ApprovalNotFound`, `ApprovalClosed`.
- **Хранилище:** миграция `002_approvals_and_audit.sql`; репозитории `approvals` (сравнение-и-запись
  статуса) и `audit` (только добавление) в обоих адаптерах, общий контрактный набор.
- **Порт `Tool`** (`ports/tools.py`): `definition`, `preview`, `execute`, `verify`; `ToolContext` —
  цель, рабочая папка задачи, защищённые корни; ни хранилища, ни трассы.
- **Реестр** (`core/tools/registry.py`): register / get / definitions.
- **Policy Engine v1** (`core/policy.py`) и **Tool Runtime** (`core/tools/runtime.py`).
- **Жизненный цикл подтверждений** (`core/approvals.py`), продолжение задачи из WAITING_CONFIRMATION в
  `TaskRunner`, `TaskService.resolve_approval` и `TaskService.approvals`.
- **Инструменты HOST только для чтения** (`adapters/tools/`): `system.cwd`, `filesystem.list`,
  `filesystem.stat`, `filesystem.search`, `filesystem.read_text`, `process.list` (psutil).
- **Сборка** (`app/composition.py`): реестр встроенных инструментов, зоны политики этого компьютера,
  `App.tools`; стадии можно передать фабрикой, получающей Tool Runtime.
- **Scripted-шаги с инструментами** (`evals/scenario.py`, `evals/scripted.py`): шаг EXECUTING вызывает
  инструмент через runtime; ожидание человека — переход в WAITING_CONFIRMATION, после решения тот же шаг
  доводит вызов.
- **Таймлайн и метрики** показывают события инструментов; управляющие символы из данных экранируются.
- **CLI:** `jarvis tools`, `jarvis tools show <id>`; команды задач передают `JARVIS_HOME` в зоны.
- **Eval:** 13 сценариев `tool.*`, временный «компьютер» на сценарий, авто-клиент подтверждений,
  инструмент `eval.sleep` только для eval.

## Tool contract

```python
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...   # id, description, input/output model → JSON Schema,
                                                   # возможные EffectKind, targets, timeout_s, untrusted_output
    async def preview(self, arguments, context) -> ToolPreview: ...   # без побочных эффектов: summary,
                                                   # normalized_arguments (канонические пути), effects, target
    async def execute(self, arguments, context) -> BaseModel: ...    # получает нормализованные аргументы
    async def verify(self, arguments, output, context) -> ToolVerification: ...  # постусловие, без LLM
```

Статического уровня риска нет: эффекты (`ToolEffect(kind, resource)`) объявляет preview конкретного
вызова; они должны входить в объявленные определением, иначе `ToolPreviewFailed`.

## Tool Runtime pipeline

```
call(task, budget, tool_id, arguments)
  владение задачей        LeaseLost (версия, статус, живая своя аренда; задача — из хранилища)
  реестр                  ToolNotFound
  цель                    UnsupportedTarget (исполняется только HOST)
  схема аргументов        InvalidToolArguments (extra="forbid")
  preview (+таймаут)      ToolPreviewFailed / ToolTimeout; эффекты ⊆ объявленных;
                          нормализованные аргументы — по схеме                        → tool.previewed
  политика                ALLOW | DENY | REQUIRE_APPROVAL; аудит DECISION до исполнения → policy.decided
                          (в той же транзакции — ограждение владения и расход решения человека)
    DENY                  → ToolOutcome DENIED (бюджет не тратится)
    REQUIRE_APPROVAL      → ApprovalRequest (PENDING) → NEEDS_APPROVAL                → approval.requested
                            (в dry run — DRY_RUN, would_execute=false, запроса нет)
  бюджет tool_calls       BudgetExceeded (аудит RESULT not_executed)
  dry run                 → DRY_RUN, would_execute=true, аудит RESULT dry_run; execute не вызывается
  TOCTOU                  у вызова с побочным эффектом preview повторяется; другой отпечаток —
                          ToolPreviewFailed, аудит RESULT not_executed
  execute (+таймаут)      нормализованные аргументы; отмена и таймаут                → tool.started
  схема результата        ToolExecutionFailed
  verify (+таймаут)       исключение = непройденная проверка                          → tool.finished, tool.verified
  аудит RESULT            succeeded | failed | timed_out | cancelled; verification passed | failed
  не прошла verify        ToolVerificationFailed
  → ToolOutcome EXECUTED (результат помечен недоверенным)
resume(task, budget)      доводит вызов по решённому запросу (после WAITING_CONFIRMATION)
```

## Policy semantics

Чистая функция `(ToolCall, ToolPreview, PolicyZones) → PolicyDecision(outcome, rules, reason)`; самое
строгое правило среди эффектов вызова побеждает; правила отсортированы.

| Условие | Решение | Правило |
| --- | --- | --- |
| цель не HOST или preview для другой цели | DENY | `target.unsupported` |
| эффектов нет | ALLOW | `effect.none` |
| ресурс в данных Jarvis (`JARVIS_HOME`) | DENY | `zone.internal` |
| DELETE / PROCESS_CONTROL / NETWORK / SYSTEM_CHANGE | DENY | `effect.<вид>` |
| WRITE/CREATE в секретах | DENY | `zone.secrets.write` |
| WRITE/CREATE в рабочей папке (`policy.workspace_roots`) | REQUIRE_APPROVAL | `effect.write.workspace` |
| WRITE/CREATE вне рабочих папок | DENY | `effect.write.outside` |
| READ секрета (папки `~/.ssh`, `~/.gnupg`, `~/.aws`, … или имя `*.pem`, `id_rsa*`, `.env`, …) | REQUIRE_APPROVAL | `zone.secrets.read` |
| READ остального | ALLOW | `effect.read` |

Зоны — канонические абсолютные пути (без учёта регистра и стиля разделителя на Windows), сравнение по
компонентам, а не по префиксу строки; относительный или пустой корень зоны — ошибка сборки. Решение не
зависит от прочитанного содержимого.

## Approval semantics

- Вызов, которому нужен человек: `ApprovalRequest` (id, задача, сам вызов, summary, эффекты, цель,
  нормализованные аргументы, отпечаток preview, created/expires) сохраняется; стадия переводит задачу в
  WAITING_CONFIRMATION (без ждущего запроса этот переход запрещён — `InvalidTransition`). Аренда на
  время ожидания отпускается.
- `TaskService.resolve_approval(id, approve|deny, via=…)` только записывает решение (+ событие
  `approval.resolved` и аудит APPROVAL). Закрытый запрос, задача не в ожидании — `ApprovalClosed`;
  поздний ответ записывается как EXPIRED и тоже `ApprovalClosed`.
- `run_until_blocked` продолжает ждущую задачу, когда её запрос решён или истёк: переход
  WAITING_CONFIRMATION → EXECUTING берёт аренду; истёкшие запросы становятся EXPIRED в той же
  транзакции. Стадия доводит вызов (`ToolRuntime.resume`): после одобрения — повторный preview и сверка
  отпечатка (изменилось — новый запрос), исполнение; после отказа или истечения — `ToolOutcome DENIED`
  из того, что видел человек, без нового preview.
- Решение расходуется ровно один раз: статус → USED сравнением-и-записью в той же транзакции, что и
  решение политики. Отмена задачи и любое завершение отзывают неприменённые запросы (WITHDRAWN); решение
  и отмена не проходят обе.

## Implemented tools

| Инструмент | Эффект | Что делает | Пределы |
| --- | --- | --- | --- |
| `system.cwd` | нет | рабочая папка задачи (канонический путь) | — |
| `filesystem.list` | READ папки | записи одной папки: имя, путь, вид, размер файла | `max_entries` ≤ 1000, `total`, `truncated` |
| `filesystem.stat` | READ объекта | вид, размер, время изменения цели (ссылка раскрыта) | — |
| `filesystem.search` | READ корня | шаблон имени без учёта регистра во вложенных папках | `max_depth` ≤ 32, `max_results` ≤ 1000, 200 000 просмотренных записей, `limits_hit`, `skipped` |
| `filesystem.read_text` | READ файла | текст UTF-8; двоичный файл — ошибка | `max_bytes` ≤ 1 МБ, `truncated` |
| `process.list` | READ `process-table` | pid, имя, исполняемый файл; фильтр по подстроке имени | `max_results` ≤ 2000 |

Порядок результатов детерминирован (без учёта регистра, при равенстве — точное имя; поиск — записи папки,
затем вложенные папки). Блокирующая работа — в потоке, который останавливается флагом при отмене или
таймауте.

## Security guarantees

- **Единственная граница:** инструменты создаёт только composition root, держит только реестр, видит его
  только runtime (проверки графа импортов). CLI исполнять инструменты не умеет.
- **Только владелец задачи действует:** в начале вызова и в транзакциях решения и `tool.started` задача в
  хранилище должна быть той же версии, активной и под живой арендой этого процесса (иначе `LeaseLost`);
  dry run и рабочая папка — из хранилища, а не от стадии.
- **Политика до эффекта:** решение пишется в трассу и аудит до `execute`; DENY и REQUIRE_APPROVAL не
  исполняются; dry run не исполняет ничего.
- **Исполняется то, что видели политика и человек:** нормализованные аргументы preview; подтверждение
  привязано к отпечатку preview; перед побочным эффектом preview повторяется.
- **Пути:** канонические в preview (`..`, `~`, ссылки раскрыты; существование и вид проверены), поэтому
  ссылка в данные Jarvis или на ключ судится по цели. Перед исполнением путь перепроверяется
  (подменённая ссылкой папка — `ToolExecutionFailed`); `read_text` сверяет открытый дескриптор с
  проверенным путём (Linux `/proc/self/fd`, Windows `GetFinalPathNameByHandleW`, иначе dev/inode) и
  открывает с `O_NOFOLLOW | O_NONBLOCK`, принимая только обычный файл. Поиск не ходит по ссылкам и
  junction, не заходит в защищённые зоны и перепроверяет папку прямо перед чтением. Пути Windows вида
  `\\?\…`, `\\.\…`, UNC и потоки NTFS инструменты не принимают (до обращения к диску), политика
  запрещает (`path.unsupported_form`). Разрешение путей в preview идёт в потоке: таймаут и отмена
  работают и на медленном диске.
- **Недоверенные данные:** результат помечен `untrusted_content`; ни политика, ни runtime его не читают
  (тесты и eval `tool.injection_data`). Управляющие символы из данных в таймлайне экранируются.
- **Секреты не оседают в журналах:** в аудите аргументы — только хешем, ресурсы — укороченными; в
  трассе у результата — размер и sha256, сам результат — только если он мал и прочитан не из зоны
  секретов.
- **Инструменты без хранилища:** `ToolContext` не даёт ни базы, ни трассы; import-linter запрещает
  `adapters.tools` импортировать хранилища и ядро.

## Tests

678 тестов: 677 прошли, 1 пропущен (проверка прав на файл под root). Новое в Session 3:

- `tests/unit/core/test_policy.py` — правила по видам эффектов и зонам, Windows-регистр, цели.
- `tests/unit/core/test_tool_runtime.py` — конвейер на FakeTool: порядок событий, аудит, нормализация,
  отказ, подтверждение, одобрение один раз, отказ человека, истечение, устаревшее подтверждение, TOCTOU,
  dry run, таймаут, отмена, сбой инструмента, схема результата, verify, бюджет, хеш аргументов, инъекция;
  по итогам ревью — владение задачей, секреты в трассе, отмена и таймаут проверки, отмена посреди
  повторного preview, срок одобрения, отзыв старых запросов.
- `tests/unit/core/test_approval_flow.py` — поток через TaskService и runner, отмена и отзыв, другой
  процесс продолжает задачу.
- `tests/contract/test_tool_contract.py` — контракт для каждого встроенного инструмента.
- `tests/integration/test_host_tools.py` — настоящие папки: `..`, ссылки в зоны, подмена после preview,
  петля ссылок, пределы, порядок, двоичный файл, инъекция.
- `tests/integration/test_tool_slice.py` — вертикальный срез на SQLite; `test_tool_performance.py` —
  5000 файлов, дерево 2000 файлов, 300 вызовов.
- `tests/architecture/test_tool_boundary.py` — граница исполнения по графу импортов.
- Тесты гонок и отмены прогнаны 8 раз до ревью и 6 раз после исправлений, полный набор — дважды
  подряд: стабильно. Исправления ревью проверены мутациями: откат каждого роняет свой тест.

## Evals

18 из 18: `runtime.*` (5) и `tool.cwd`, `tool.list`, `tool.search`, `tool.stat`, `tool.process_list`,
`tool.invalid_arguments`, `tool.denied`, `tool.dry_run`, `tool.cancel`, `tool.timeout`,
`tool.injection_data`, `tool.approval_approve`, `tool.approval_deny`. Ожидания сценария: порядок событий
инструментов, итоги вызовов, правила последнего решения, найденные пути.

## Known limitations

- Подтверждения из CLI нет (`jarvis approvals / approve / deny / resume`): только API сервиса и eval.
- Истечение запроса применяется лениво: при ответе человека или при следующем `run_until_blocked`.
- У list/stat/search между перепроверкой пути и чтением остаётся узкое окно (только метаданные; у
  `read_text` его закрывает проверка дескриптора). Полностью его закроет обход по дескрипторам.
- Ограждение владения проверяется в снимке транзакции записи, а не условием на аренду при commit:
  аренда, истекающая в те же миллисекунды, теоретически успевает перейти к другому процессу; следующая
  контрольная точка такого прогона всё равно не пройдёт (сравнение версии задачи).
- Итог вызова, начатого до потери задачи, записывается и после её завершения (после `task.finished`).
- Поток, застрявший в системном вызове (сетевой диск), не прерывается: вызов возвращает таймаут, поток
  дорабатывает сам.
- Результат больше 2 КБ в трассу не пишется (только размер); хранилища артефактов нет.
- Зоны v1 — данные Jarvis, секреты, рабочие папки; system / app_config / user_docs из 04 §2 и уровни
  риска — с инструментами записи.
- На Windows код не запускался: junction, `GetFinalPathNameByHandleW`, регистр путей проверены только
  рассуждением и тестами на Linux.

## Architecture deviations

Все записаны в [ADR 0022](../adr/0022-tool-runtime-v1.md): инструменты — адаптеры, а не
`core/tools/builtin`; `ToolDefinition` без `version`/`idempotent`/`exposure`; нормализация в preview;
эффекты вызова вместо `SideEffects`; политика по зонам вместо `evaluate_risk`; подтверждение по отпечатку
preview; dry run ничего не исполняет (отступление от ADR 0016); `simulate` нет; таблицы `tool_calls` нет —
вызовы описаны трассой и аудитом.

## ADR changes

- Новый [ADR 0022](../adr/0022-tool-runtime-v1.md). Короткие отсылки к нему в 01-structure, 03-contracts,
  04-security (§3, §7), 05-storage-and-trace; `development.md` — команды и как добавить инструмент.

## Review

Независимый ревьюер (отдельный агент, только чтение и эксперименты в scratchpad) нашёл 5 важных и 6
мелких проблем; критических нет. Исправлено:

| # | Находка | Исправление |
| --- | --- | --- |
| 1 (важная) | `\\?\…`, UNC (`\\localhost\C$`), потоки NTFS обходили сравнение зон на Windows | политика: `path.unsupported_form`; инструменты: такие пути не принимаются до обращения к диску |
| 2 (важная) | runtime верил объекту задачи: прогон, потерявший задачу, продолжал вызывать инструменты | ограждение владения (версия, статус, живая своя аренда) в начале и в записи решения и старта; режим — из хранилища |
| 3 (важная) | одобренное чтение секрета попадало в трассу целиком | результат из зоны секретов в трассу не пишется; размер и sha256 — всегда |
| 4 (важная) | разрешение пути в preview блокировало цикл событий: таймаут не срабатывал | preview разрешает пути в потоке; `system.cwd` — тоже |
| 5 (важная) | отмена во время verify теряла итог исполненного вызова; у verify не было таймаута | таймаут verify; при отмене итог записывается (`verification_status=cancelled`) |
| 6 | отмена во время повторного preview оставляла решение без итога | итог `not_executed` и при отмене |
| 7 | подменённый канал (FIFO) вешал `read_text` | `O_NONBLOCK` и только обычный файл |
| 8 | окно подмены папки в поиске между проверкой и чтением | перепроверка прямо перед чтением; `list` — и после |
| 9 | нормализованные аргументы проверялись после решения политики | проверка сразу после preview, до политики |
| 10 | зоны: нет Windows-мест секретов; конфиг вне JARVIS_HOME | GitHub CLI, gcloud, история PowerShell, профили браузеров, `.npmrc`, `.pypirc`, `*_history`; файл конфига — внутренняя зона |
| 11 | одобрение без срока; разный выбор решения при продолжении; гонка решения с отменой давала `ConcurrentModification` | одобрение действует до срока запроса; новый запрос отзывает старые; берётся последнее решение; гонка — `ApprovalClosed` |

Каждое исправление закреплено тестом, который падает на старом коде (проверено откатом).

## Commits

```
5078d17 feat(tools): add tool, approval and audit domain contracts
c56e4d0 feat(storage): persist approvals and the audit log
4a2ac13 feat(security): add policy engine v1
1156382 feat(tools): add the tool port, registry and Tool Runtime
82550f3 feat(tools): add read-only host tools
4e7b6e6 feat(security): add the approval flow
25c95f0 feat(trace): show tool events in the timeline and count tool calls
8add738 feat(cli): add jarvis tools and jarvis tools show
ccc3931 test(architecture): guard the tool execution boundary
23a84f8 feat(evals): add tool scenarios
64abc7d test(tools): add the SQLite vertical slice and performance checks
0e83aa8 docs: record Tool Runtime v1 in ADR 0022 and sync the Stage 0 docs
1e6099f docs: add the session 3 handoff (review pending)
0163375 fix(security): deny path forms that bypass the zone checks
46f6756 fix(tools): address the Session 3 security review
```

Плюс коммит с этим обновлением handoff и ADR 0022.

## Next recommended session

Session 4: Local Model Gateway + structured output + llama.cpp + аппаратный бенчмарк + первый выбор
инструмента моделью. Эту сессию **не начинали**: ни Gateway, ни провайдеров, ни промптов в коде нет.

## Exact starting point

- `main` после коммита handoff; `uv sync`, затем `uv run ruff check`, `uv run ruff format --check`,
  `uv run pyright`, `uv run lint-imports`, `uv run pytest -q`, `uv run jarvis eval` — всё зелёное.
- Модель будет стадией, которая получает `ToolRuntime` (как `ScriptedStages.handlers(tools)`), видит
  `runtime.definitions()` (схемы для structured output) и вызывает `runtime.call(...)` /
  `runtime.resume(...)`; ответ инструмента для промпта — `ToolOutcome` с `untrusted_content`.
- Подтверждения из CLI и уровни риска — когда появятся инструменты записи.
