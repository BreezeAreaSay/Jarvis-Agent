# 06. Ошибки и конфигурация

## 1. Таксономия ошибок

### Принцип

- **Ожидаемые исходы — значения, а не исключения.** Отказ политики и необходимость подтверждения
  возвращаются из `ToolRuntime.run()` как `Denied` и `NeedsConfirmation`. В таксономии они есть как
  категории (`PolicyDenied`, `ConfirmationRequired`) — для `ErrorInfo`, трассы и аудита.
- **Исключения — для сбоев.** Все они наследуют `JarvisError` и несут категорию, признак повтора и
  **диспозицию** — что с этим делает ядро.
- Голый `Exception` в ядре не поднимается и не перехватывается, кроме одного места в runner: неизвестное
  исключение — это ошибка программы → `FATAL`, категория `internal`, стек — в системный лог.

```python
class Disposition(StrEnum):
    RETRY = "retry"          # повторить то же действие (с лимитом и задержкой)
    FEEDBACK = "feedback"    # вернуть модели как наблюдение, засчитать сбой
    REPLAN = "replan"        # перейти в REPLANNING
    ASK_USER = "ask_user"    # WAITING_CONFIRMATION (или clarify на этапе роутинга)
    DEGRADE = "degrade"      # продолжить без компонента
    FATAL = "fatal"          # FAILED
    STOP = "stop"            # CANCELLED или BUDGET_EXCEEDED

class JarvisError(Exception):
    category: ClassVar[str]
    disposition: ClassVar[Disposition]
    retryable: ClassVar[bool] = False
    def to_info(self) -> ErrorInfo: ...     # сериализуемый вид для трассы, аудита и итога

class ErrorInfo(BaseModel, frozen=True):
    category: str; disposition: Disposition; retryable: bool; message: str; details: dict[str, JsonValue] = {}
```

### Категории

| Класс | Пример | Повтор | Диспозиция | Переход |
| --- | --- | --- | --- | --- |
| `ConfigError` | роли назначена модель без structured output | нет | FATAL | старт не выполняется |
| `StorageError` | БД недоступна, нет места | нет | FATAL | FAILED (если можно записать) |
| `ConcurrentModification` | задачу продолжает другой процесс | нет | FATAL для текущего запуска | состояние не меняется |
| `ModelError` → `ModelUnavailable`, `ModelTimeout` | сервер моделей не отвечает | да, ≤ 2 с задержкой | RETRY, затем FATAL | FAILED |
| `InvalidModelOutput` | ответ не прошёл схему после всех попыток ремонта | нет | FEEDBACK (executor) / FATAL (planner, router без запасного пути) | остаётся / FAILED |
| `TaskNotFound` | `jarvis trace task_999` | нет | ошибка команды | — |
| `TaskBusy` | `jarvis cancel` для задачи, которую ведёт другой процесс | нет | ошибка команды | состояние не меняется |
| `LeaseLost` | процесс завис дольше срока аренды, задачу забрали | нет | FATAL для текущего запуска | состояние не меняется |
| `ProjectRegistryError` | запись реестра не прошла схему | нет | DEGRADE | запись недоступна, остальные работают |
| `ToolNotFound`, `ToolNotAllowed` | модель выбрала инструмент вне профиля | нет | FEEDBACK | остаётся EXECUTING |
| `InvalidToolArguments` | аргументы не прошли схему инструмента | нет | FEEDBACK | остаётся EXECUTING |
| `ToolExecutionFailed` | процесс завершился с ошибкой, файл занят | если инструмент пометил ошибку как повторяемую и он идемпотентен | RETRY или FEEDBACK | остаётся EXECUTING |
| `ToolTimeout` | команда не уложилась в таймаут | один раз, если идемпотентен | RETRY или FEEDBACK | остаётся EXECUTING |
| `UnsupportedTarget` | цель DOCKER/SSH в Stage 0 | нет | FEEDBACK | остаётся EXECUTING |
| `PolicyDenied` | удаление в зоне `system` | нет (тот же вызов не повторяется) | FEEDBACK | остаётся EXECUTING |
| `ConfirmationRequired` | HIGH-действие | — | ASK_USER | WAITING_CONFIRMATION |
| `VerificationFailed` | постусловие не выполнено | нет | REPLAN, затем FATAL | REPLANNING / FAILED |
| `BudgetExceeded` | 21-й шаг при лимите 20 | нет | STOP | BUDGET_EXCEEDED |
| `TaskCancelled` | Ctrl+C, `jarvis cancel` | нет | STOP | CANCELLED |
| `SkillProviderError` | AI-Dev-System не ответил | нет | DEGRADE | без изменений |
| `ProjectNotFound`, `AmbiguousEntity` | «запусти проект» при двух проектах | нет | ASK_USER | ROUTING → COMPLETED с вопросом |
| `InvalidTransition` | стадия вернула недопустимый переход | нет | FATAL | FAILED (это баг) |

Ошибки инструментов (`ToolNotFound`, `ToolNotAllowed`, `InvalidToolArguments`, `ToolExecutionFailed`,
`ToolTimeout`, `UnsupportedTarget`) наследуют общий `ToolError`, ошибки модели — `ModelError`.

Каждый сбой с диспозицией FEEDBACK увеличивает `usage.failures`; когда сбоев становится больше
`max_failures`, задача переходит в BUDGET_EXCEEDED. Три одинаковые ошибки подряд — сигнал детектору
зацикливания.

## 2. Конфигурация

### Слои

```
defaults            значения в коде (pydantic-модели)            — всегда
  ↓
user config         %LOCALAPPDATA%\Jarvis\config\config.toml     — пользователь
  ↓
project config      секция overrides в записи реестра проекта    — пользователь, только разрешённые ключи
  ↓
runtime overrides   флаги CLI, параметры сценария eval           — на один запуск
```

- Слияние — глубокое, по ключам; результат валидируется **один раз** одной схемой `JarvisConfig`.
- Схема — чистые pydantic-модели в `jarvis.domain.settings`; загрузчик (чтение файлов и окружения) — в
  `jarvis.config`. Ядро получает готовый объект настроек и `jarvis.config` не импортирует.
- **Умолчания:** у каждого поля есть значение по умолчанию, кроме назначения моделей. Пустой конфиг
  валиден; команда, которой нужна модель, сообщает `ConfigError` «роли router не назначена модель».
- Неизвестный ключ — ошибка (`extra="forbid"`): опечатка в настройке политики не должна молча игнорироваться.
- Переменные окружения читаются только загрузчиком и только две: `JARVIS_HOME` (где данные) и
  `JARVIS_CONFIG` (путь к конфигу). Остальной код окружение не читает (проверяется тестом архитектуры).
- **Файлы конфигурации внутри репозиториев не читаются.** Проектный слой — это раздел `overrides` в
  записи реестра, которую пишет сам пользователь. Разрешённые ключи: `budgets.<маршрут>.<лимит>`,
  `policy.shell.model_access` (только строже: `catalog` → `confirm_all` → `disabled`),
  `policy.large_change_files` (только меньше). Любой другой ключ — ошибка записи реестра.
- `jarvis config show --sources` показывает итоговое значение каждого ключа и слой, откуда оно пришло;
  `jarvis config check` проверяет конфиг; с M3 — ещё доступность модели и требования ролей, с M5 — реестр.

### Схема (эскиз)

```python
class JarvisConfig(BaseModel, extra="forbid"):
    schema_version: Literal[1] = 1
    paths: PathsConfig                         # home, database, blobs, trash, logs, projects_dir
    models: ModelsConfig                       # endpoints и назначение ролей
    model_profiles: dict[str, ModelProfile]
    budgets: BudgetsConfig                     # routing и маршруты direct, chat, agent
    policy: PolicyConfig
    tools: ToolsConfig
    skills: SkillsConfig
    router: RouterConfig
    trace: TraceConfig
    cli: CliConfig

class ModelEndpoint(BaseModel, extra="forbid"):
    id: str
    backend: Literal["openai_compat"]          # единственный вид бэкенда в Stage 0
    base_url: HttpUrl
    model: str                                 # имя модели на сервере
    profile: str                               # ключ в model_profiles
    capabilities: ModelCapabilities            # объявленные; `jarvis model check` сверяет с пробами
    request_timeout_s: float = 120

class ModelProfile(BaseModel, extra="forbid"):
    structured_strategy: Literal["json_schema", "prompt_repair"] = "json_schema"
    repair_attempts: int = 2
    sampling: dict[ModelRole, SamplingParams]  # temperature, top_p, seed, max_tokens по ролям
    disable_reasoning: ReasoningSwitch | None  # как отключить «размышления» у этой модели
    stop: list[str] = []
    extra_body: dict[str, JsonValue] = {}      # особенности сервера (например, chat_template_kwargs)

class PolicyConfig(BaseModel, extra="forbid"):
    zones: ZonesConfig                         # пути по зонам; корни проектов добавляются автоматически
    trusted_roots: list[str] = []              # дополнительные доверенные папки для MEDIUM
    large_change_files: int = 50
    approval_ttl_s: int = 1800
    shell: ShellPolicyConfig                   # model_access: disabled | catalog | confirm_all
    dry_run_approvals: Literal["simulate", "stop"] = "simulate"

class SkillsConfig(BaseModel, extra="forbid"):
    provider: Literal["null", "static", "ai_dev_mcp"] = "null"
    timeout_s: float = 5.0
    max_skills: int = 3
    static_dir: str | None = None
    ai_dev: AiDevConfig | None = None          # команда запуска, закреплённая версия, allowlist
```

### Пример пользовательского конфига

```toml
schema_version = 1

[[models.endpoints]]
id = "main"
backend = "openai_compat"
base_url = "http://127.0.0.1:8080/v1"
model = "local-main"
profile = "default"
[models.endpoints.capabilities]
structured_output = true
constrained_decoding = true
native_tools = false
vision = false
embeddings = false
context_window = 16384
max_output_tokens = 2048

[models.roles]
router = "main"
planner = "main"
executor = "main"
responder = "main"

[model_profiles.default]
structured_strategy = "json_schema"
repair_attempts = 2
[model_profiles.default.sampling.router]
temperature = 0.0
max_tokens = 256
[model_profiles.default.sampling.executor]
temperature = 0.2
max_tokens = 768

[budgets.agent]
max_steps = 20
max_tool_calls = 30
max_failures = 5
max_replans = 3
max_wall_time_s = 300
max_model_calls = 60
max_model_tokens = 250000

[policy.shell]
model_access = "catalog"

[skills]
provider = "null"
```

### Запись реестра проектов

`%LOCALAPPDATA%\Jarvis\config\projects\gofra.yaml`:

```yaml
id: gofra
name: GOFRA
aliases: [гофра, gofra]
root:
  target: host                     # host | "wsl:<distro>"
  path: 'C:\projects\gofra'
repository: https://github.com/<owner>/gofra
stack: [node, react, postgres, docker-compose]
commands:                          # в Stage 0 только хранятся
  start: {argv: [docker, compose, up, -d], cwd: .}
overrides:                         # проектный слой конфига; только разрешённые ключи
  budgets: {agent: {max_steps: 15}}
  policy: {shell: {model_access: confirm_all}}
```

Реестр валидируется при старте (`jarvis projects validate`): уникальность ID и алиасов (с учётом
нормализации и транслитерации), существование корня, корректность цели исполнения. Ошибка в одной записи
не ломает остальные: запись помечается недоступной, а `jarvis projects list` показывает причину.
