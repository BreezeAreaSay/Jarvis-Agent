# 02. Доменные модели и машина состояний

Пакет `jarvis.domain` не делает ввода-вывода и зависит только от pydantic. Ниже — эскизы: имена и поля
согласуются здесь, точные типы — в коде M1.

## 1. Идентификаторы

| Сущность | Формат | Пример |
| --- | --- | --- |
| Задача | `task_<n>`, n — последовательность из БД | `task_42` |
| План (версия) | `<task>.plan_<n>` | `task_42.plan_2` |
| Шаг плана | `<task>.step_<n>` (сквозная нумерация в задаче) | `task_42.step_4` |
| Вызов модели | `<task>.mc_<n>` | `task_42.mc_7` |
| Вызов инструмента | `<task>.call_<n>` | `task_42.call_12` |
| Артефакт | `<task>.art_<n>` (содержимое адресуется по sha256 отдельно) | `task_42.art_3` |
| Запрос подтверждения | `<task>.appr_<n>` | `task_42.appr_1` |
| Событие трассы | `<task>.ev_<n>` | `task_42.ev_31` |

- ID выдаёт порт `IdAllocator`: счётчик увеличивается сразу при выдаче, отдельно от контрольной точки
  задачи. Поэтому ID никогда не повторяются, даже если такт задачи не дошёл до сохранения; пропуски
  номеров допустимы.
- ID читаются человеком, уникальны глобально и **детерминированы**: replay задачи `task_42` создаёт
  `task_43` с той же нумерацией вызовов, поэтому трассы сравниваются по шагам.
- В тестах последовательность задач начинается с 1 — снапшоты стабильны.

```python
TaskId = NewType("TaskId", str)          # и так же PlanId, StepId, ModelCallId, ToolCallId,
                                         # ArtifactId, ApprovalId, EventId

ChildKind = Literal["plan", "step", "mc", "call", "art", "appr", "ev"]

class IdAllocator(Protocol):                     # порт; реализации — память (тесты) и SQLite
    def next_task_id(self) -> TaskId: ...
    def next_child_id(self, task_id: TaskId, kind: ChildKind) -> str: ...
```

## 2. Доменные модели

### Задача и запрос

```python
class Origin(StrEnum):   CLI = "cli"; EVAL = "eval"; REPLAY = "replay"
class Route(StrEnum):    DIRECT = "direct"; AGENT = "agent"; CHAT = "chat"; CLARIFY = "clarify"
class ExecutionMode(StrEnum):
    NORMAL = "normal"
    DRY_RUN = "dry_run"                  # побочные эффекты симулируются
    REPLAY_SIMULATED = "replay_simulated"
    REPLAY_LIVE = "replay_live"

class TaskRequest(BaseModel, frozen=True):
    text: str                            # как пришло от пользователя
    origin: Origin
    working_directory: TargetPath        # текущая папка CLI
    project_hint: str | None = None      # --project gofra
    mode: ExecutionMode = ExecutionMode.NORMAL
    replay_of: TaskId | None = None

class Task(BaseModel):
    id: TaskId
    version: int                         # оптимистическая блокировка при сохранении
    request: TaskRequest
    status: TaskStatus
    route: Route | None                  # подробности решения роутера (RouteDecision) — отдельным полем с M7
    profile: str | None                  # "workspace" | "dev"
    project_id: str | None
    plan_id: PlanId | None               # текущая версия плана
    state: AgentState                    # рабочая память задачи
    budget: Budget
    usage: BudgetUsage
    tainted: bool                        # в контекст модели попал недоверенный контент
    outcome: TaskOutcome | None
    created_at: datetime; updated_at: datetime
    # аренда (кто сейчас ведёт задачу) хранится отдельно, чтобы не конфликтовать с version
```

### Маршрутизация

```python
class NormalizedRequest(BaseModel, frozen=True):
    raw: str
    text: str                            # без обращения «Джарвис», ё→е, нормализованные числа
    tokens: list[Token]                  # тип: cyr | lat | mixed | path | code | number | quoted
    entities: list[EntityCandidate]      # проекты, известные папки, расширения, сервисы, приложения

class RouteDecision(BaseModel, frozen=True):
    route: Route
    source: Literal["grammar", "llm"]
    intent: str | None                   # для direct: "fs.show_cwd", "fs.find_by_extension"
    slots: dict[str, JsonValue]
    profile: str | None
    project_id: str | None
    confidence: float
    question: str | None                 # для clarify
```

### План и действия

```python
class CheckSpec(BaseModel, frozen=True):
    type: str                            # из библиотеки проверок: "file.exists", "path.absent", "evidence.present" …
    args: dict[str, JsonValue]

class SuccessCriterion(BaseModel, frozen=True):
    id: str                              # "c1"
    description: str
    check: CheckSpec | None              # None — «мягкий» критерий, проверить нечем
    required: bool = True

class PlanStep(BaseModel, frozen=True):
    id: StepId
    title: str
    tool_hints: list[str] = []           # инструменты, которые вероятно понадобятся

class Plan(BaseModel, frozen=True):
    id: PlanId
    version: int
    goal: str
    steps: list[PlanStep]                # 1–7 шагов
    criteria: list[SuccessCriterion]     # 0–5 критериев
    reason: str | None = None            # почему план пересмотрен

class ToolAction(BaseModel, frozen=True):
    type: Literal["tool"]; step_id: StepId; tool: str; arguments: dict[str, JsonValue]
class StepDoneAction(BaseModel, frozen=True):
    type: Literal["step_done"]; step_id: StepId; note: str
class ReplanAction(BaseModel, frozen=True):
    type: Literal["replan"]; reason: str
class FinishAction(BaseModel, frozen=True):
    type: Literal["finish"]; answer: str; evidence: list[str]   # ID вызовов и артефактов

class ProposedAction(BaseModel, frozen=True):
    decision: str                        # ≤ 280 символов: зачем это действие (не chain-of-thought)
    action: ToolAction | StepDoneAction | ReplanAction | FinishAction   # дискриминатор "type"

class AgentState(BaseModel):
    current_step: StepId | None
    step_status: dict[StepId, Literal["pending", "in_progress", "done", "skipped", "failed"]]
    observations: list[Observation]      # последние полностью, старые — сжатые заметки
    pending_call: ToolCallId | None      # вызов, ожидающий подтверждения
    candidate_answer: FinishAction | None
```

### Инструменты, риск, подтверждения

```python
class RiskLevel(IntEnum):  SAFE = 0; MEDIUM = 1; HIGH = 2; CRITICAL = 3
class SideEffects(StrEnum):
    NONE = "none"                        # только чтение
    LOCAL_REVERSIBLE = "local_reversible"
    LOCAL_IRREVERSIBLE = "local_irreversible"
    EXTERNAL = "external"                # данные уходят за пределы машины

class RiskReason(BaseModel, frozen=True):
    code: str                            # "zone.system", "delete.directory", "shell.unparseable" …
    message: str                         # для человека

class RiskAssessment(BaseModel, frozen=True):
    level: RiskLevel
    reasons: list[RiskReason]
    resources: list[ResourceRef]         # затронутые пути, процессы, хосты
    reversible: bool

class PolicyOutcome(StrEnum): ALLOW = "allow"; CONFIRM = "require_confirmation"; DENY = "deny"
class PolicyDecision(BaseModel, frozen=True):
    outcome: PolicyOutcome
    effective_level: RiskLevel           # после модификаторов (taint и др.)
    rules: list[str]                     # сработавшие правила — для аудита и объяснения
    reason: str

class ToolCallStatus(StrEnum):
    PROPOSED = "proposed"; AWAITING_APPROVAL = "awaiting_approval"; DENIED = "denied"
    RUNNING = "running"; SUCCEEDED = "succeeded"; FAILED = "failed"; TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"; SIMULATED = "simulated"

class ToolCall(BaseModel):
    id: ToolCallId; task_id: TaskId; step_id: StepId | None
    tool_id: str; tool_version: int
    arguments: dict[str, JsonValue]      # после валидации и нормализации
    arguments_hash: str                  # для replay и сопоставления подтверждений
    target: ExecutionTarget
    risk: RiskAssessment | None; policy: PolicyDecision | None
    approval_id: ApprovalId | None
    status: ToolCallStatus
    result: ToolResult | None
    started_at: datetime | None; finished_at: datetime | None

class ToolResult(BaseModel, frozen=True):
    status: ToolCallStatus
    output: dict[str, JsonValue] | None  # прошёл валидацию output-схемы инструмента
    summary: str                         # для модели, ≤ 1 000 символов (в трассу — первые 200)
    artifacts: list[ArtifactRef]         # полный вывод — здесь
    effects: list[Effect]                # что изменилось (для аудита и replay)
    postcondition: PostconditionResult | None
    error: ErrorInfo | None
    untrusted_content: bool              # в выводе есть внешнее содержимое (файл, лог, вывод команды)

class ApprovalStatus(StrEnum):
    PENDING = "pending"; APPROVED = "approved"; DENIED = "denied"; ABORTED = "aborted"
    EXPIRED = "expired"; CANCELLED = "cancelled"; SIMULATED = "simulated"

class ApprovalRequest(BaseModel):
    id: ApprovalId; task_id: TaskId; tool_call_id: ToolCallId
    title: str                           # «Удалить 14 файлов *.tmp в C:\projects\gofra\tmp»
    tool_id: str
    arguments: dict[str, JsonValue]      # с отредактированными секретами
    preview: EffectPreview               # что произойдёт: файлы, команда, объём
    risk: RiskAssessment
    decision_text: str                   # обоснование модели, помеченное как текст модели
    tainted_context: bool
    status: ApprovalStatus
    created_at: datetime; expires_at: datetime
    resolved_at: datetime | None; resolved_via: str | None   # "cli", "eval-auto", …
```

### Проверка и итог

```python
class CriterionStatus(StrEnum): PASSED = "passed"; FAILED = "failed"; UNKNOWN = "unknown"; SKIPPED = "skipped"
class CriterionResult(BaseModel, frozen=True):
    criterion_id: str; status: CriterionStatus; evidence: list[str]; message: str

class VerificationOverall(StrEnum):
    VERIFIED = "verified"                # все обязательные критерии прошли детерминированно
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"            # проверить было нечем — честно сообщаем
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"    # chat-ответ, уточняющий вопрос

class VerificationReport(BaseModel, frozen=True):
    overall: VerificationOverall; results: list[CriterionResult]

class TaskOutcome(BaseModel, frozen=True):
    status: TaskStatus                   # терминальный
    answer: str | None
    verification: VerificationReport | None
    error: ErrorInfo | None
    metrics: TaskMetrics

class TaskMetrics(BaseModel, frozen=True):      # определения — 05-storage-and-trace.md §3
    latency_ms: int; time_to_first_action_ms: int | None; total_duration_ms: int
    model_calls: int; tool_calls: int; prompt_tokens: int; completion_tokens: int
    replans: int; failures: int; success: bool

class TaskSnapshot(BaseModel, frozen=True):     # то, что видят клиенты (CLI, eval)
    id: TaskId; status: TaskStatus; route: Route | None; profile: str | None; project_id: str | None
    plan_goal: str | None; usage: BudgetUsage; budget: Budget
    pending_approval: ApprovalId | None; outcome: TaskOutcome | None
```

«Успех» задачи зависит от маршрута: agent — COMPLETED с `verified` или `partially_verified`; direct —
COMPLETED с выполненным постусловием инструмента (`verified`); chat и clarify — COMPLETED
(`not_applicable`). Прошёл ли сценарий eval, решают его ожидания, а не только эта метрика.

### Цели исполнения и пути

```python
class TargetKind(StrEnum):
    HOST = "host"                        # машина, где работает Jarvis (на ПК пользователя — Windows)
    WSL = "wsl"; DOCKER = "docker"; SSH = "ssh"   # DOCKER и SSH — только типы в Stage 0

class ExecutionTarget(BaseModel, frozen=True):
    kind: TargetKind
    os_family: Literal["windows", "posix"]
    name: str                            # "local", "Ubuntu-24.04", имя контейнера, хост

class TargetPath(BaseModel, frozen=True):
    target: ExecutionTarget
    path: str                            # в синтаксисе ОС цели; интерпретируется как PureWindowsPath/PurePosixPath

def convert_path(path: TargetPath, to: ExecutionTarget) -> TargetPath: ...
#   C:\projects\gofra (HOST/windows) ↔ /mnt/c/projects/gofra (WSL)
#   \\wsl$\Ubuntu-24.04\home\u\p (HOST/windows) ↔ /home/u/p (WSL)
#   остальные сочетания → UnsupportedTarget
```

Путь без цели не существует: функция, которой нужен путь, принимает `TargetPath`. Смешать
`C:\…` и `/mnt/c/…` можно только явным вызовом `convert_path`.

Имена целей отличаются от формулировки требований (WINDOWS, WSL, DOCKER, REMOTE_SSH) намеренно:
WINDOWS — это `HOST` с `os_family="windows"`, потому что тот же код работает на Linux CI, где HOST —
POSIX; REMOTE_SSH — это `SSH` ([ADR 0017](../adr/0017-windows-first-execution-target.md)).

### Проекты, навыки, модели

```python
class CommandSpec(BaseModel, frozen=True):
    argv: list[str]; cwd: str = "."; target: TargetKind = TargetKind.HOST

class Project(BaseModel, frozen=True):
    id: str; name: str; aliases: list[str] = []
    root: TargetPath
    repository: str | None = None
    stack: list[str] = []
    commands: dict[str, CommandSpec] = {}   # в Stage 0 только хранятся, не исполняются
    notes: str | None = None
    overrides: ProjectOverrides | None = None   # проектный слой конфига (06-errors-and-config.md §2)
# В YAML корень записывается коротко: root: {target: host | "wsl:<distro>", path: ...};
# загрузчик реестра превращает это в TargetPath с полным ExecutionTarget.

class SkillSummary(BaseModel, frozen=True):
    id: str; title: str; summary: str; source: str; score: float | None; trust: Literal["user", "imported"]
class Skill(SkillSummary, frozen=True):
    content: str                         # всегда недоверенный текст для промпта
class ProjectContextPack(BaseModel, frozen=True):
    project_id: str; source: str; content: str; fingerprint: str | None

class ModelRole(StrEnum):
    ROUTER = "router"; PLANNER = "planner"; EXECUTOR = "executor"; RESPONDER = "responder"
    # Verifier в Stage 0 детерминированный и модель не вызывает

class ModelCapabilities(BaseModel, frozen=True):
    structured_output: bool              # умеет отвечать JSON по схеме (с ограниченным декодированием или нет)
    constrained_decoding: bool           # бэкенд гарантирует соответствие схеме грамматикой
    native_tools: bool
    vision: bool
    embeddings: bool
    context_window: int
    max_output_tokens: int
# Ядро Stage 0 проверяет только structured_output, constrained_decoding и context_window;
# остальные поля — данные матрицы возможностей из требований, без потребителя в Stage 0.
```

В Stage 0 нет класса `Recipe` и пакета рецептов: «навык» в коде — это только знание для модели
(`Skill`), см. [ADR 0007](../adr/0007-skills-vs-recipes.md).

## 3. Машина состояний задачи

```
CREATED ─► ROUTING ─┬─ agent ──────────► PLANNING ─► EXECUTING
                    ├─ direct, chat ───────────────► EXECUTING
                    └─ clarify ────────────────────► COMPLETED   (итог — вопрос пользователю)

EXECUTING ◄──► WAITING_CONFIRMATION        нужно подтверждение / получено решение
EXECUTING ───► REPLANNING ───► EXECUTING   действие replan, детектор зацикливания
EXECUTING ───► VERIFYING ─┬──► COMPLETED   обязательные критерии пройдены
                          ├──► REPLANNING  критерий провален, перепланирования остались
                          └──► FAILED      критерий провален, перепланирований нет

любое нетерминальное состояние ─► FAILED | CANCELLED | BUDGET_EXCEEDED
WAITING_CONFIRMATION ─► CANCELLED          «отклонить и остановить»
```

| Из | В | Условие |
| --- | --- | --- |
| CREATED | ROUTING | автоматически, первым тактом runner'а (обработчика у CREATED нет) |
| ROUTING | PLANNING | route = agent |
| ROUTING | EXECUTING | route = direct или chat |
| ROUTING | COMPLETED | route = clarify (итог — вопрос пользователю) |
| PLANNING | EXECUTING | план прошёл схему и семантическую проверку |
| EXECUTING | EXECUTING | — (не переход: шаги внутри состояния фиксируются событиями) |
| EXECUTING | WAITING_CONFIRMATION | политика требует подтверждения |
| WAITING_CONFIRMATION | EXECUTING | решение через `TaskService.resolve_approval` (внешнее событие, не такт runner'а): одобрено, отклонено или истёк срок; отказ возвращается агенту наблюдением |
| WAITING_CONFIRMATION | CANCELLED | «отклонить и остановить» или отмена |
| EXECUTING | REPLANNING | действие `replan` или детектор зацикливания |
| EXECUTING | VERIFYING | действие `finish`; direct-команда выполнена; chat-ответ готов |
| VERIFYING | COMPLETED | обязательные критерии пройдены или проверять нечего (`unverified` / `not_applicable`) |
| VERIFYING | REPLANNING | обязательный критерий провален, перепланирования остались |
| VERIFYING | FAILED | обязательный критерий провален, перепланирований нет |
| REPLANNING | EXECUTING | новая версия плана принята |
| любое нетерминальное | FAILED | фатальная ошибка (см. таксономию) |
| любое нетерминальное | CANCELLED | запрошена отмена |
| любое нетерминальное | BUDGET_EXCEEDED | превышен лимит бюджета |

Правила:

- Таблица переходов — данные в `domain` (`ALLOWED_TRANSITIONS`). Недопустимый переход —
  `InvalidTransition`, программная ошибка, тест падает.
- **Переход атомарен:** новое состояние задачи и событие `task.transition {from, to, reason}`
  записываются одной операцией хранилища (в SQLite — одна транзакция).
- Терминальные состояния: COMPLETED, FAILED, CANCELLED, BUDGET_EXCEEDED. Из них переходов нет.
- Задачу продвигает только процесс, который держит её **аренду** (владелец и срок; продлевается, пока
  идёт работа). Задача в активном состоянии с истёкшей арендой — прерванная: при старте CLI она
  переводится в FAILED (`interrupted`). Задачу с живой арендой другой процесс не трогает.
  WAITING_CONFIRMATION аренды не держит — её может продолжить любой процесс (`jarvis resume`).
- Время ожидания подтверждения **не входит** в `max_wall_time`: бюджет считает только активное время.

### Отмена

```
Ctrl+C в CLI → TaskService.cancel(task_id) в том же процессе
  → отмена текущего такта (asyncio): ToolRuntime завершает дерево процессов, вызов получает cancelled
  → runner переводит задачу в CANCELLED, событие task.transition с причиной
jarvis cancel task_42 из другого терминала — только для задачи без живой аренды (например, ожидающей
подтверждения): переход выполняется сразу; работающую задачу отменяет её процесс
второе Ctrl+C — немедленный выход; аренда истекает, и при следующем старте задача становится
FAILED (interrupted)
```

### Как runner использует машину состояний

```python
class TaskChanges(BaseModel, frozen=True):      # что изменить в задаче; None — не менять
    route: Route | None = None           # только из ROUTING
    answer: str | None = None            # попадает в итог при завершении
    # с потребителями в следующих milestone: решение роутера, профиль, проект, план, AgentState, tainted

class StageOutcome(BaseModel, frozen=True):
    next_status: TaskStatus
    reason: str
    changes: TaskChanges

class StageHandler(Protocol):
    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome: ...
```

Расход бюджета стадия списывает через `BudgetMeter` до действия (§4). Runner забирает расход из
счётчика и тогда, когда стадия завершилась исключением, поэтому потраченное до сбоя не теряется. Итог
задачи (`TaskOutcome`) собирает runner при переходе в терминальное состояние: ответ — из `answer`,
ошибка — из исключения.

Стадия не меняет статус сама: она возвращает `StageOutcome`, а runner проверяет переход по таблице,
применяет изменения и пишет их одной транзакцией. У CREATED обработчика нет (переход в ROUTING
автоматический), из WAITING_CONFIRMATION задачу выводит `TaskService.resolve_approval` через ту же
проверку переходов.

Записи бывают двух видов:

- **Журнальные** — события трассы, вызовы модели и инструментов, подтверждения, артефакты, аудит.
  Пишутся сразу, когда происходят (`Tracer.emit` и репозитории), и никогда не откатываются.
- **Контрольная точка** — строка задачи и событие `task.transition`. Пишется атомарно в конце такта.

После сбоя журнал показывает, что успело произойти (например, `tool.started` без `tool.finished`), а
задача остаётся в последней контрольной точке. Один вызов стадии EXECUTING — один «такт»: одно
действие модели или исполнение одного одобренного вызова. Поэтому состояние задачи всегда лежит в БД,
а не в стеке вызовов Python, и задачу можно продолжить после выхода из процесса.

## 4. Бюджеты

```python
class Budget(BaseModel, frozen=True):
    max_steps: int                       # действия агента (такты EXECUTING с вызовом модели)
    max_tool_calls: int
    max_failures: int                    # ошибки инструментов + невалидные ответы модели + отказы политики
    max_replans: int
    max_wall_time_s: float               # активное время, без ожидания подтверждения
    max_model_calls: int
    max_model_tokens: int                # вход + выход

class BudgetUsage(BaseModel):
    steps: int = 0; tool_calls: int = 0; failures: int = 0; replans: int = 0
    active_time_s: float = 0.0; model_calls: int = 0; model_tokens: int = 0
```

| Лимит | routing | direct | chat | agent |
| --- | --- | --- | --- | --- |
| `max_steps` | 0 | 1 | 1 | 20 |
| `max_tool_calls` | 0 | 3 | 0 | 30 |
| `max_failures` | 1 | 1 | 1 | 5 |
| `max_replans` | 0 | 0 | 0 | 3 |
| `max_wall_time_s` | 10 | 15 | 60 | 300 |
| `max_model_calls` | 3 | 0 | 3 | 60 |
| `max_model_tokens` | 8 000 | 0 | 16 000 | 250 000 |

Семантика:

- **Маршрутизация** расходует собственный лимит `routing` (вызов роутера плюс до двух ремонтов ответа);
  после решения роутера действует бюджет маршрута. В метриках учитывается всё.
- **Лимиты — включительные максимумы:** новый шаг не начинается, если `usage.steps == max_steps`;
  вызов модели не делается, если `usage.model_calls == max_model_calls`, и так далее. Токены известны
  только после вызова, поэтому новый вызов не делается, если `usage.model_tokens ≥ max_model_tokens`.
  Сбои считаются по факту: задача переходит в BUDGET_EXCEEDED, когда `usage.failures` становится
  больше `max_failures`.
- **Что считается:** `model_calls` — каждая попытка, включая ремонт ответа и повтор; `tool_calls` — каждое
  исполнение и симуляция (отказ политики не считается); `failures` — итоговые сбои после ремонтов и
  повторов, включая отказы политики.
- **Время** — активное, без ожидания подтверждения. Оно проверяется перед тактом и ограничивает сам такт
  (`asyncio.timeout` на остаток): долгий вызов прерывается, а не дожидается конца.

Значения — стартовые, задаются в конфиге и уточняются по eval. Превышение — переход в BUDGET_EXCEEDED с
событием `budget.exceeded {limit, value}` и частичным итогом (что успели сделать). Детектор зацикливания
(повтор одного вызова с теми же аргументами без изменения результата, осцилляция A → B → A → B, три
одинаковые ошибки подряд) переводит задачу в REPLANNING, а повторное срабатывание — в FAILED.
