# 03. Публичные интерфейсы и контракты

Все интерфейсы — `typing.Protocol` (структурная типизация: адаптер не наследуется от ядра).
`async` только у операций ввода-вывода ([ADR 0013](../adr/0013-async-at-io-boundaries.md)).

## 1. Порты (`jarvis.ports`)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...

class UnitOfWork(Protocol):
    """Одна транзакция SQLite. Переход состояния и его события пишутся вместе."""
    tasks: TaskRepository
    plans: PlanRepository
    tool_calls: ToolCallRepository
    approvals: ApprovalRepository
    trace: TraceRepository
    model_calls: ModelCallRepository
    def __enter__(self) -> "UnitOfWork": ...
    def __exit__(self, *exc: object) -> None: ...        # без commit — откат
    def commit(self) -> None: ...

class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...

class TaskRepository(Protocol):
    def next_task_id(self) -> TaskId: ...
    def add(self, task: Task) -> None: ...
    def get(self, task_id: TaskId) -> Task: ...                    # TaskNotFound
    def save(self, task: Task, expected_version: int) -> None: ... # ConcurrentModification
    def list(self, *, status: set[TaskStatus] | None = None, limit: int = 50) -> list[TaskSummary]: ...
    def request_cancel(self, task_id: TaskId) -> None: ...

class TraceRepository(Protocol):
    def append(self, events: Sequence[TraceEvent]) -> None: ...
    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]: ...

class AuditLog(Protocol):
    def append(self, record: AuditRecord) -> AuditEntry: ...      # хэш-цепочка считается внутри
    def verify_chain(self) -> AuditVerification: ...

class ArtifactStore(Protocol):
    def put(self, task_id: TaskId, data: bytes, meta: ArtifactMeta) -> ArtifactRef: ...
    def read(self, ref: ArtifactRef, *, offset: int = 0, limit: int = 65_536) -> bytes: ...

class FileSystem(Protocol):
    def supports(self, target: ExecutionTarget) -> bool: ...
    def stat(self, path: TargetPath) -> FileStat | None: ...
    def list_dir(self, path: TargetPath, *, limit: int) -> list[DirEntry]: ...
    def walk(self, root: TargetPath, *, pattern: str, max_depth: int, limit: int) -> list[DirEntry]: ...
    def read_bytes(self, path: TargetPath, *, limit: int) -> bytes: ...
    def write_bytes_atomic(self, path: TargetPath, data: bytes) -> None: ...
    def make_dirs(self, path: TargetPath) -> None: ...
    def move(self, src: TargetPath, dst: TargetPath) -> None: ...

class ProcessRunner(Protocol):
    async def run(self, spec: ProcessSpec) -> ProcessResult: ...
    # ProcessSpec: argv, target, cwd, env (только из allowlist), timeout_s, stdin=None, max_output_bytes
    # отмена (asyncio.CancelledError) и таймаут убивают всё дерево процессов
    def list_processes(self, *, limit: int) -> list[ProcessInfo]: ...

class ShellParser(Protocol):
    dialect: Literal["powershell", "posix"]
    async def parse(self, command: str) -> ParsedCommand: ...
    # ParsedCommand: commands (имя, аргументы, перенаправления), pipeline, unsupported: list[str]

class KnownFolders(Protocol):
    def resolve(self, name: KnownFolder) -> TargetPath | None: ...   # downloads, documents, desktop, home

class ModelBackend(Protocol):                       # см. §3
class SkillProvider(Protocol):                      # см. §4
```

Реализации Stage 0:

| Порт | Реализации |
| --- | --- |
| `UnitOfWork`, репозитории, `AuditLog`, `ArtifactStore` | `adapters.sqlite` (blobs — файлы по sha256) |
| `FileSystem` | `adapters.local_fs` (HOST; WSL через `\\wsl$`, если Q2 = WSL) |
| `ProcessRunner` | `adapters.local_process`; `adapters.fake_process` для eval |
| `ShellParser` | `adapters.shell_pwsh`, `adapters.shell_posix` |
| `KnownFolders` | `adapters.known_folders` |
| `ModelBackend` | `adapters.llm_openai`, `adapters.scripted`, `adapters.recorded` |
| `SkillProvider` | `core.skills.NullSkillProvider`, `adapters.scripted.StaticSkillProvider`, `adapters.ai_dev_mcp` (M9) |
| `Clock` | системные часы; управляемые часы в тестах |

Для каждого порта есть общий набор контрактных тестов (`tests/contract`), который проходят все его
реализации, включая фейки.

## 2. Tool contract

```python
class Exposure(StrEnum):
    MODEL = "model"            # может выбрать модель (в своём профиле)
    INTERNAL = "internal"      # только direct-обработчики и проверки

@dataclass(frozen=True)
class ToolSpec(Generic[I, O]):
    id: str                    # "fs.delete"
    version: int
    description: str           # для модели: когда использовать и когда НЕ использовать
    input_model: type[I]       # pydantic → JSON Schema
    output_model: type[O]
    side_effects: SideEffects
    idempotent: bool
    timeout_s: float
    targets: frozenset[TargetKind]
    exposure: Exposure

class Tool(Protocol[I, O]):
    spec: ToolSpec[I, O]

    def normalize(self, args: I, ctx: ToolContext) -> I: ...
        # относительные пути → TargetPath от рабочей папки или корня проекта; без ввода-вывода

    async def preview(self, args: I, ctx: ToolContext) -> EffectPreview: ...
        # только чтение: что будет затронуто (файлы, количество, размер, команда); используется
        # для оценки риска, карточки подтверждения и dry run

    def evaluate_risk(self, args: I, preview: EffectPreview, ctx: RiskContext) -> RiskAssessment: ...
        # чистая функция: риск конкретного вызова; RiskContext — зоны путей, корни проектов, цель

    async def execute(self, args: I, ctx: ToolContext) -> ToolOutput[O]: ...
        # побочные эффекты — только здесь и только через порты из ctx

    def simulate(self, args: I, preview: EffectPreview) -> ToolOutput[O]: ...
        # правдоподобный результат без эффектов (dry run); для side_effects = NONE не нужен

    async def verify(self, args: I, output: ToolOutput[O], ctx: ToolContext) -> PostconditionResult: ...
        # постусловие инструмента: файл действительно создан, путь действительно отсутствует …
```

Соответствие формулировке из требований: `id`, `description`, `input_schema` (из `input_model`),
`output_schema` (из `output_model`), `risk_evaluator` (`evaluate_risk`), `timeout`, `executor`
(`execute`), `verifier` (`verify`). Дополнительно `preview` и `simulate` — без них нет dry run и
осмысленной карточки подтверждения.

### Риск зависит от аргументов

| Вызов | Зона | Оценка | Решение по умолчанию |
| --- | --- | --- | --- |
| `fs.delete("C:\projects\gofra\tmp\temp.txt")` | project, 1 файл | MEDIUM (в корзину Jarvis, обратимо) | разрешено, если задача не заражена недоверенным контентом; иначе подтверждение |
| `fs.delete("C:\projects\gofra\tmp")` | project, папка, 14 файлов | HIGH | подтверждение |
| `fs.delete("C:\Users\me\Documents\report.docx")` | user_docs | HIGH | подтверждение |
| `fs.delete("C:\Users")` | system / профили пользователей | CRITICAL | запрет |
| `fs.read_text("C:\Users\me\.ssh\id_ed25519")` | secrets | HIGH | подтверждение |
| `fs.write_text("C:\Windows\System32\drivers\etc\hosts")` | system | CRITICAL | запрет |

### Инструменты Stage 0

| Инструмент | Эффекты | Доступ модели | Заметки |
| --- | --- | --- | --- |
| `fs.list_directory` | нет | да | |
| `fs.search_files` | нет | да | glob, подстрока имени, диапазон дат изменения, лимит |
| `fs.read_text` | нет | да | содержимое → артефакт, `untrusted_content=true`; `.env` — значения маскируются |
| `fs.ensure_directory` | обратимые | да | идемпотентный: уже есть — ничего не делает |
| `fs.write_text` | обратимые | да | бэкап прежней версии; одинаковое содержимое — ничего не делает |
| `fs.delete` | обратимые | да | перемещение в корзину Jarvis с манифестом восстановления; безвозвратного удаления нет |
| `process.list` | нет | да | |
| `project.list`, `project.get` | нет | да | из реестра |
| `artifact.read` | нет | да | фрагмент артефакта по смещению или по строкам с подстрокой |
| `logs.extract_errors` | нет | да | детерминированная выжимка ошибок и стектрейсов из артефакта |
| `docker.ps`, `docker.logs`, `docker.compose_validate` | нет | да (профиль `dev`) | только чтение; для eval «сломанный Docker-конфиг» |
| `shell.execute` | любые | по режиму доступа | escape hatch, см. [04-security.md §4](04-security.md#4-shell--escape-hatch) |

Два правила для всех инструментов с эффектами:

- **Идемпотентность по возможности:** `fs.ensure_directory` вместо `mkdir`, запись с проверкой содержимого
  («уже так — ничего не делать»). Это нужно для повтора после сбоя и для replay; неидемпотентный
  инструмент автоматически не повторяется.
- **Успех инструмента ≠ успех задачи.** `verify` проверяет только постусловие самого вызова (файл записан,
  путь исчез). Достигнута ли цель — решает Verifier по критериям плана: `docker compose up` с кодом 0 ещё
  не значит, что backend отвечает на healthcheck.

Инструменты регистрируются **явным списком** в composition root. Профиль агента (`workspace`, `dev`)
— это список ID инструментов, видимых модели; всё остальное ей не показывается.

## 3. Model Gateway

Ядро обращается к модели только через `ModelGateway` и только по **роли**. Температура, лимиты, режим
structured output, отключение «размышлений», стоп-токены и особенности шаблона чата — в профиле модели
и адаптере, не в ядре.

```python
class ModelGateway:                                   # core.gateway, конкретный класс
    def capabilities(self, role: ModelRole) -> ModelCapabilities: ...
    def context_budget(self, role: ModelRole) -> int: ...          # окно минус резерв на ответ
    def count_tokens(self, role: ModelRole, text: str) -> int: ... # точный (через бэкенд) или оценка

    async def generate_structured(
        self, role: ModelRole, prompt: Prompt, schema: type[T], *,
        validate: Callable[[T], list[str]] | None = None,          # семантические проверки вызывающего
        call: ModelCallContext,                                    # task_id, id вызова, режим
    ) -> StructuredResult[T]: ...                                  # value, attempts, usage, call_id

    async def generate_text(self, role: ModelRole, prompt: Prompt, *, call: ModelCallContext) -> TextResult: ...

class ModelBackend(Protocol):                         # реализуют адаптеры
    info: ModelInfo                                   # id, capabilities, profile
    async def complete(self, request: BackendRequest) -> BackendResponse: ...
    async def count_tokens(self, text: str) -> int | None: ...
    async def health(self) -> BackendHealth: ...

class BackendRequest(BaseModel, frozen=True):
    messages: list[ChatMessage]                       # уже отрисованный Prompt
    json_schema: dict[str, Any] | None                # если нужен structured output
    sampling: SamplingParams                          # из профиля модели для роли
    max_tokens: int
    stream: bool                                      # для замера времени до первого токена

class BackendResponse(BaseModel, frozen=True):
    text: str
    usage: TokenUsage                                 # prompt_tokens, completion_tokens
    timings: BackendTimings                           # ttft_ms, total_ms, tokens_per_s (если сервер отдаёт)
    finish_reason: str
```

### Матрица возможностей и требования ролей

Требования ролей объявлены в ядре, а не в конфиге:

| Роль | Требования |
| --- | --- |
| `router` | `structured_output`, `context_window ≥ 4096` |
| `planner` | `structured_output`, `context_window ≥ 8192` |
| `executor` | `structured_output`, `context_window ≥ 8192` |
| `verifier` | `structured_output`, `context_window ≥ 4096` |
| `responder` | `context_window ≥ 4096` |

При старте Gateway проверяет, что модель, назначенная роли в конфиге, удовлетворяет требованиям; иначе —
`ConfigError` с объяснением. Ядро спрашивает `capabilities(role)`, а не имя модели.

### Конвейер structured output

```
Prompt + schema
  → стратегия из профиля модели:
       json_schema   — ограниченное декодирование на сервере (по умолчанию для llama.cpp)
       prompt_repair — схема в промпте, разбор, ремонт
  → ответ бэкенда → разбор JSON → валидация pydantic → validate() вызывающего (семантика)
  → ошибка → повтор с текстом ошибки (не больше repair_attempts из профиля)
  → всё ещё ошибка → InvalidModelOutput
```

В историю агента попадает только ответ, прошедший проверку. Каждая попытка записывается как вызов
модели (`model_calls`), с хэшем промпта и ответом — это основа replay.

### Промпт как секции с доверием

```python
class PromptSection(BaseModel, frozen=True):
    kind: Literal["system", "request", "plan", "state", "tools", "data", "skills"]
    trust: Literal["trusted", "untrusted"]
    source: str | None                    # "file:C:\...\README.md", "tool:task_42.call_3", "skill:ai-dev/…"
    content: str

class Prompt(BaseModel, frozen=True):
    template_id: str                      # "executor.v1"
    sections: list[PromptSection]         # порядок: стабильные секции — первыми
```

Секции `untrusted` отрисовываются только внутри блоков данных
([04-security.md §5](04-security.md#5-недоверенный-контент-и-архитектура-промптов)). Отрисовка — в ядре
(`core.agent.prompts`), одинаковая для всех моделей; профиль модели может выбрать только вариант разметки.

## 4. SkillProvider

```python
class SkillQuery(BaseModel, frozen=True):
    text: str                             # задача пользователя
    project_root: TargetPath | None
    limit: int = 3

class SkillProvider(Protocol):
    def info(self) -> ProviderInfo: ...                        # имя, версия, доступен ли
    async def search(self, query: SkillQuery) -> list[SkillSummary]: ...
    async def get(self, skill_id: str) -> Skill: ...           # SkillNotFound
    async def get_project_context(self, project: Project, task_text: str) -> ProjectContextPack | None: ...

class NullSkillProvider:                  # core.skills — поставляется всегда
    def info(self) -> ProviderInfo: return ProviderInfo(name="null", version="0", available=True)
    async def search(self, query: SkillQuery) -> list[SkillSummary]: return []
    async def get(self, skill_id: str) -> Skill: raise SkillNotFound(skill_id)
    async def get_project_context(self, project: Project, task_text: str) -> ProjectContextPack | None: return None
```

- `SkillResolver` (ядро) вызывает провайдера с таймаутом; любая `SkillProviderError` → деградация:
  событие `skill.degraded`, задача продолжается без навыков.
- Содержимое навыков и пакета контекста — всегда секции `untrusted`: навык не даёт прав и не меняет политику.
- Поведение Jarvis с `NullSkillProvider` — полноценное, а не «урезанное»: все eval проходят и без AI-Dev-System.
- Адаптер AI-Dev-System (M9) реализует порт через `recommend_skills`, `read_skill_card` / `read_skill` и
  `compile_project_context`, с allowlist вызовов на стороне Jarvis ([05-ai-dev-system.md](../architecture/05-ai-dev-system.md)).

## 5. Компоненты ядра

Каждая стадия — отдельный класс с узкими зависимостями; runner ничего не знает об их устройстве.

```python
class Intake:                                         # чисто, без I/O
    def normalize(self, request: TaskRequest, entities: EntityIndex) -> NormalizedRequest: ...

class Router:
    def __init__(self, grammar: GrammarMatcher, llm_router: LlmRouter, resolver: EntityResolver): ...
    async def route(self, request: NormalizedRequest, ctx: RoutingContext) -> RouteDecision: ...

class ContextBuilder:
    def __init__(self, skills: SkillResolver, projects: ProjectRegistry, gateway: ModelGateway): ...
    async def build(self, task: Task, role: ModelRole) -> Prompt: ...   # бюджет — из gateway.context_budget

class Planner:
    async def plan(self, task: Task, prompt: Prompt) -> Plan: ...
    async def revise(self, task: Task, prompt: Prompt, reason: str) -> Plan: ...

class Executor:
    async def next_action(self, task: Task, prompt: Prompt) -> ProposedAction: ...

class Verifier:
    def __init__(self, checks: CheckLibrary): ...
    async def verify(self, task: Task, plan: Plan, answer: FinishAction | None) -> VerificationReport: ...

class ToolRuntime:
    async def run(self, request: ToolCallRequest, ctx: ExecutionContext) -> ToolRunResult: ...
    # ToolRunResult = Executed(call, result) | NeedsConfirmation(call, approval) | Denied(call, decision)

class PolicyEngine:                                   # чисто
    def decide(self, request: PolicyInput) -> PolicyDecision: ...

class TaskService:                                    # публичный API ядра
    def submit(self, request: TaskRequest) -> TaskId: ...
    async def run_until_blocked(self, task_id: TaskId) -> TaskSnapshot: ...   # до терминального или WAITING_CONFIRMATION
    def pending_approval(self, task_id: TaskId) -> ApprovalRequest | None: ...
    def resolve_approval(self, approval_id: ApprovalId, decision: ApprovalDecision, via: str) -> None: ...
    def cancel(self, task_id: TaskId, reason: str) -> None: ...
    def get(self, task_id: TaskId) -> TaskSnapshot: ...
    def trace(self, task_id: TaskId) -> list[TraceEvent]: ...
```

`ApprovalDecision` в Stage 0: `approve_once`, `deny`, `deny_and_abort`. Ни один из этих методов не
доступен модели: подтверждение может прийти только от клиента (CLI, движок eval, позже UI и голос).

## 6. Intake и Router: русский + технический английский

Пользователь говорит «Джарвис, открой Docker Desktop и перезапусти backend GOFRA»: русская грамматика,
английские названия, имя проекта латиницей, которое в речи склоняется («гофру»). Конвейер Intake учитывает
это явно, а не надеется на LLM.

```
1 обращение      удалить «Джарвис», «Jarvis» в начале фразы
2 токенизация    сохранить целыми: строки в кавычках, `команды`, пути Windows и POSIX, URL, маски (*.pdf),
                 расширения (.log), числа с единицами; каждому токену — тип: cyr | lat | mixed | path | code | number
3 нормализация   кириллица: нижний регистр, ё→е, лемма (pymorphy3): «загрузках» → «загрузка», «гофру» → «гофра»
                 латиница: только нижний регистр, без лемматизации («Downloads», «backend» не трогаются)
                 составные: «PDF-файлы» → «pdf» + «файл»
4 сущности       сопоставление с EntityIndex по формам: точно → транслитерация (ru↔lat) → нечётко (rapidfuzz, ≥ 90)
5 L0             шаблоны работают с леммами и слотами сущностей, слова могут быть на любом языке
6 L2             LLM получает исходный текст **и** разметку токенов и сущностей, а не угадывает их заново
```

Индекс сущностей Stage 0:

| Вид | Источник | Примеры форм |
| --- | --- | --- |
| Известная папка | `KnownFolders` + словарь | Downloads / загрузки / «Загрузки»; Documents / документы; Desktop / рабочий стол |
| Проект | реестр: имя, алиасы, транслитерация | GOFRA / gofra / гофра / «гофру» |
| Тип файла | словарь | pdf / пдф / «пдфки»; docx / ворд; log / лог |
| Название с латиницей | любое слово или фраза из латиницы с заглавной буквы | Docker Desktop, Telegram — кандидат «приложение», даже если в Stage 0 действий с приложениями нет |
| Служебные слова | словарь | backend / бэкенд, frontend / фронтенд, контейнер / container |

Правила:

- Транслитерация двунаправленная и детерминированная (таблица + частые фонетические варианты: «докер» ↔ docker).
- Нечёткое совпадение допускается для слов от 5 символов и только если второй кандидат заметно хуже; иначе — `AmbiguousEntity` → clarify.
- Распознанная сущность, для которой в Stage 0 нет действия (приложение), не теряется: агент честно отвечает, что действие не поддерживается.
- Набор фраз роутера ([07-evals-and-benchmarks.md §3](07-evals-and-benchmarks.md#3-набор-фраз-роутера)) проверяет все пять видов смешения.
