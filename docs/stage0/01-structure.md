# 01. Структура репозитория и зависимости

## 1. Структура репозитория

Один Python-пакет `jarvis` в src-layout. Модули перечислены до уровня файлов только там, где это
важно для границ; остальное появляется вместе с кодом своего milestone.

```
Jarvis-Agent/
├── pyproject.toml               # зависимости, ruff, pyright, pytest, import-linter
├── uv.lock
├── README.md                    # для пользователя: что это
├── docs/
│   ├── architecture/            # архитектура верхнего уровня
│   ├── stage0/                  # этот проект
│   ├── adr/                     # решения
│   └── development.md           # для разработчика: установка, модель, тесты, запуск (M1)
├── examples/
│   ├── config.toml              # пример пользовательского конфига
│   └── projects/gofra.yaml      # пример записи реестра проектов
├── src/jarvis/
│   ├── domain/                  # модели, ID, ошибки, машина состояний, бюджеты, риск, цели исполнения,
│   │                            # settings — схема конфига (чистые pydantic-модели)
│   ├── ports/                   # Protocol-интерфейсы к инфраструктуре
│   ├── core/
│   │   ├── service.py           # TaskService — публичный API ядра для CLI, eval и будущих клиентов
│   │   ├── runner.py            # TaskRunner — переводит задачу между состояниями
│   │   ├── stages/              # routing, planning, executing, verifying, replanning
│   │   ├── router/              # intake, нормализация, сущности, грамматики L0, LLM-роутер
│   │   │   └── grammars/ru.yaml
│   │   ├── agent/               # planner, executor, verifier, проверки, context builder
│   │   │   └── prompts/         # версионируемые шаблоны промптов
│   │   ├── gateway/             # ModelGateway, требования ролей, конвейер structured output
│   │   ├── tools/               # контракт, реестр, ToolRuntime, артефакты, эффекты
│   │   │   └── builtin/         # fs, process, project, shell, docker (только чтение), artifact
│   │   ├── policy/              # PolicyEngine, зоны путей, классификатор команд
│   │   │   └── command_catalog.yaml
│   │   ├── skills/              # SkillResolver, NullSkillProvider
│   │   ├── projects/            # реестр проектов: валидация и поиск; записи — из порта ProjectStore
│   │   └── trace/               # Tracer, рендер человекочитаемой трассы, метрики
│   ├── adapters/
│   │   ├── memory/              # хранилище в памяти: репозитории, IdAllocator (тесты, M1)
│   │   ├── project_files/       # ProjectStore: YAML-файлы реестра
│   │   ├── llm_openai/          # ModelBackend поверх OpenAI-совместимого HTTP API
│   │   ├── sqlite/              # Unit of Work, репозитории, миграции, blobs
│   │   ├── local_fs/            # FileSystem для HOST (и WSL через \\wsl$, если Q2 = WSL)
│   │   ├── local_process/       # ProcessRunner: subprocess, таймауты, убийство дерева процессов
│   │   ├── shell_pwsh/          # ShellParser: AST PowerShell через долгоживущий pwsh
│   │   ├── shell_posix/         # ShellParser: консервативный разбор простых POSIX-команд
│   │   ├── known_folders/       # «Загрузки», «Документы» и т. п. для Windows и POSIX
│   │   ├── scripted/            # ScriptedModelBackend для тестов и eval
│   │   ├── recorded/            # записанные ответы модели и инструментов для replay
│   │   ├── fake_process/        # заранее заданные выводы команд (docker и др.) для eval
│   │   └── ai_dev_mcp/          # SkillProvider поверх MCP (M10, необязательно)
│   ├── app/
│   │   ├── composition.py       # build_app(config): единственное место, где создаются адаптеры
│   │   └── replay.py            # записанный ToolInvoker и сборка runner'а для replay
│   ├── config/                  # загрузчик слоёв: файлы и переменные окружения
│   ├── cli/                     # команды: run, tasks, trace, approvals, resume, cancel, replay, eval, bench
│   ├── evals/                   # движок eval: загрузка сценариев, авто-подтверждения, проверки, отчёты
│   └── bench/                   # аппаратный бенчмарк моделей
├── evals/
│   ├── scenarios/               # YAML-сценарии
│   ├── fixtures/                # рабочие папки, сломанный docker-проект, логи с инъекциями
│   ├── cassettes/               # записанные ответы для scripted-режима
│   ├── golden/                  # эталонные свойства трасс
│   ├── router/                  # набор фраз для роутера (русский + английские вставки)
│   └── reports/                 # результаты прогонов (в git — только итоговые отчёты)
├── benchmarks/
│   ├── hardware/                # кандидаты моделей, профили железа
│   └── results/                 # отчёты бенчмарка
└── tests/
    ├── unit/                    # домен, политика, роутер, конвейеры — без I/O
    ├── contract/                # общий набор тестов для каждой реализации порта
    ├── integration/             # SQLite, реальная ФС во временной папке, subprocess
    └── architecture/            # проверка import-linter и отсутствия запрещённых импортов
```

Почему так:

- **`domain` и `ports` отдельно от `core`** — это и есть правило зависимостей: ядро видит только
  интерфейсы, адаптеры их реализуют.
- **Встроенные инструменты лежат в `core/tools/builtin`**, потому что содержат логику инструмента
  (аргументы, риск, постусловия), а ввод-вывод делают через порты `FileSystem` и `ProcessRunner`.
  *Session 3:* инструменты — адаптеры `jarvis/adapters/tools` (порт `jarvis.ports.tools.Tool`); в ядре
  остались реестр, Tool Runtime и политика ([ADR 0022](../adr/0022-tool-runtime-v1.md)).
- **`app/composition.py` — единственный composition root.** Никаких реестров, заполняемых побочными
  эффектами импорта: список инструментов, стадий и адаптеров собирается явно, конструкторами.
- **Данные (грамматики, каталог команд, промпты) лежат рядом с кодом**, который их читает, и попадают в
  пакет через `importlib.resources`.
- **`evals/` (данные) отдельно от `jarvis/evals` (движок):** сценарии меняются чаще кода и читаются людьми.

## 2. Пакеты и ответственность

| Пакет | Отвечает за | Не отвечает за |
| --- | --- | --- |
| `domain` | Модели, ID, ошибки, таблица переходов состояний, бюджеты, уровни риска, цели исполнения и пути, схема настроек | Любой ввод-вывод |
| `ports` | Протоколы: модель, хранилище, ID, аренды, реестр проектов, ФС, процессы, разбор shell, навыки, известные папки, часы | Реализации |
| `core.service` | Публичный API: создать задачу, продвинуть, разрешить подтверждение, отменить, прочитать трассу | Логику стадий |
| `core.runner` | Цикл «загрузить задачу → проверить отмену и бюджет → вызвать стадию → применить переход» | Решения внутри стадий |
| `core.stages` | По одному обработчику на состояние | Хранение, политику |
| `core.router` | Нормализация, сущности, грамматики L0, LLM-роутер | Исполнение |
| `core.agent` | Planner, Executor, Verifier, библиотека проверок, сборка контекста, промпты | Вызов инструментов напрямую |
| `core.gateway` | Выбор модели по роли и возможностям, structured output, ремонт ответа, запись вызовов | Формат HTTP-API конкретного сервера |
| `core.tools` | Контракт инструмента, реестр, конвейер исполнения, артефакты, журнал эффектов | Решение «можно ли» (это политика) |
| `core.policy` | Зоны путей, классификация команд, решение allow / require_confirmation / deny | Исполнение |
| `core.skills` | Подбор руководств через `SkillProvider`, деградация при ошибке | Хранение навыков |
| `core.projects` | Валидация записей из `ProjectStore`, поиск по имени и алиасам | Чтение файлов, анализ кода |
| `core.trace` | Запись событий, метрики, человекочитаемый рендер | Хранение (через порт) |
| `adapters.*` | Реализация одного порта для одной технологии | Логику ядра |
| `app` | Сборка графа объектов из конфига; replay | Логику |
| `config` | Чтение файлов конфига и слияние слоёв; единственное место, где читаются переменные окружения | Схема (она в `domain.settings`) |
| `cli`, `evals`, `bench` | Точки входа | Логику ядра |

## 3. Граф зависимостей

Стрелка — «импортирует».

```
            cli ─────► evals        bench          ← точки входа (cli вызывает движок eval)
              \          |          /
               └──────► app ◄──────┘               ← composition root
                      /     \
                     ▼       ▼
              adapters        core  ──────────┐    ← реализации │ оркестрация и логика
                  │            │              │
                  ▼            ▼              │
                ports ◄────────┘              │    ← интерфейсы
                  │                           │
                  ▼                           ▼
                domain ◄──────────────────────┘    ← модели (зависит только от pydantic)

config (загрузчик) → domain; импортируется только app и точками входа
```

Внутри `core` (стрелки — разрешённые импорты):

```
service → runner → stages
stages.routing    → router
stages.planning   → agent.planner, agent.context, skills, projects
stages.executing  → agent.executor, tools.runtime
stages.verifying  → agent.verifier
stages.replanning → agent.planner
router, agent.*   → gateway
agent.verifier    → agent.checks → ports (FileSystem и др.)
tools.runtime     → policy, tools.builtin, ports
tools.builtin     → ports, policy.zones (для оценки риска)
gateway, trace, skills, projects → ports
policy            → domain (чистая логика, без портов)
```

`core.policy` не зависит ни от чего, кроме `domain`: решения политики — чистые функции, которые
проверяются property-тестами.

## 4. Правила импорта

Проверяются `import-linter` в CI и тестом в `tests/architecture`. Эскиз:

```toml
[tool.importlinter]
root_package = "jarvis"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Слои"
type = "layers"
layers = [
  "jarvis.cli",                          # команда `jarvis eval` вызывает движок eval
  "jarvis.evals | jarvis.bench",
  "jarvis.app",
  "jarvis.adapters | jarvis.core | jarvis.config",
  "jarvis.ports",
  "jarvis.domain",
]

[[tool.importlinter.contracts]]
name = "Ядро не знает инфраструктуры"
type = "forbidden"
source_modules = ["jarvis.core", "jarvis.ports", "jarvis.domain"]
forbidden_modules = ["jarvis.adapters", "jarvis.app", "jarvis.config",
                     "httpx", "sqlite3", "subprocess", "mcp", "psutil", "ctypes"]

[[tool.importlinter.contracts]]
name = "Адаптеры не зависят друг от друга и от ядра"
type = "independence"
modules = ["jarvis.adapters.memory", "jarvis.adapters.project_files", "jarvis.adapters.llm_openai",
           "jarvis.adapters.sqlite", "jarvis.adapters.local_fs", "jarvis.adapters.local_process",
           "jarvis.adapters.shell_pwsh", "jarvis.adapters.shell_posix"]

[[tool.importlinter.contracts]]
name = "Адаптеры видят только порты и домен"
type = "forbidden"
source_modules = ["jarvis.adapters"]
forbidden_modules = ["jarvis.core", "jarvis.app"]
```

Запрет модулей стандартной библиотеки (`subprocess`, `sqlite3`, `ctypes`, `os.system`/`os.popen`)
дополнительно проверяет простой AST-тест в `tests/architecture`: не стоит полагаться на то, что
import-linter учитывает stdlib. Тот же тест запрещает чтение `os.environ` вне `jarvis.config` и файловый
ввод-вывод в `domain`, `ports` и `core` (`open`, `Path.read_*`/`write_*`, `os` и `shutil` для файлов);
единственное исключение — данные пакета через `importlib.resources`.

Схема настроек — в `jarvis.domain.settings`; `jarvis.core` получает готовый объект настроек, созданный в
`app`, и не импортирует `jarvis.config` — поэтому ядро можно тестировать без файлов конфига.

## 5. Внешние зависимости

| Библиотека | Зачем | Где разрешена |
| --- | --- | --- |
| pydantic v2 | Модели, JSON Schema для модели и инструментов | везде |
| PyYAML (safe_load) | Реестр проектов, грамматики, каталог команд, сценарии eval | `core` (данные пакета через `importlib.resources`), `adapters.project_files`, `evals` |
| httpx | HTTP к серверу моделей | `adapters.llm_openai`, `bench` |
| sqlite3 (stdlib) | Хранилище | `adapters.sqlite` |
| psutil | Дерево процессов, память процессов | `adapters.local_process`, `bench` |
| pymorphy3 + словари ru | Лемматизация русских словоформ | `core.router` |
| rapidfuzz | Нечёткое сопоставление сущностей | `core.router` |
| typer, rich | CLI и рендер | `cli` |
| mcp (extra `ai-dev`) | Адаптер AI-Dev-System | `adapters.ai_dev_mcp` (M10) |
| pytest, anyio (плагин), hypothesis | Тесты | `tests` |
| ruff, pyright, import-linter | Качество | dev |

Никаких LangChain, LiteLLM, ORM и DI-фреймворков. Зависимости внедряются конструкторами.

## 6. Точки входа и composition root

```python
# app/composition.py — эскиз
def build_app(config: JarvisConfig, overrides: Overrides | None = None) -> App:
    storage = SqliteStorage(config.paths.database, blob_dir=config.paths.blobs)
    backends = {e.id: OpenAICompatBackend(e) for e in config.models.endpoints}   # или Scripted/Recorded
    gateway = ModelGateway(backends, roles=config.models.roles, profiles=config.model_profiles,
                           recorder=storage.model_calls)
    tools = ToolRegistry([
        ListDirectoryTool(fs), SearchFilesTool(fs), ReadTextTool(fs), WriteTextTool(fs),
        EnsureDirectoryTool(fs), DeleteTool(fs, trash), ProcessListTool(processes),
        ShellExecuteTool(processes, parsers), ...
    ])                                                            # явный список, без автообнаружения
    runtime = ToolRuntime(tools, PolicyEngine(config.policy), invoker, storage.unit_of_work, artifacts, audit, clock)
    stages = {TaskStatus.ROUTING: RoutingStage(...), TaskStatus.PLANNING: PlanningStage(...), ...}
    runner = TaskRunner(stages, storage.unit_of_work, storage.ids, storage.leases, tracer, budget_guard, clock)
    return App(tasks=TaskService(runner, storage.unit_of_work, storage.ids, storage.leases, tracer), ...)
```

`App` — простой контейнер из нескольких сервисов для точек входа, а не фасад со всей логикой: CLI
работает с `TaskService`, команды `projects` и `config` — со своими маленькими сервисами.
