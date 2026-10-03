# Разработка

Как поставить проект, запустить проверки и что нужно соблюдать в коде. Архитектура —
[docs/architecture](architecture/README.md), фундамент Stage 0 — [docs/stage0](stage0/README.md),
решения — [docs/adr](adr/README.md).

## Требования

- [uv](https://docs.astral.sh/uv/) — ставит нужный Python (3.12, см. `.python-version`) и зависимости.
- Git. Windows 11 или Linux.

## Установка и первый запуск

```
git clone https://github.com/BreezeAreaSay/Jarvis-Agent
cd Jarvis-Agent
uv sync
uv run pytest
uv run jarvis --version
```

## Проверки

Всё это запускает CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)); перед коммитом
достаточно тех же команд локально.

| Что | Команда |
| --- | --- |
| Тесты | `uv run pytest` |
| Линтер | `uv run ruff check` |
| Форматирование | `uv run ruff format` (в CI — `--check`) |
| Типы (strict для `domain`, `ports`, `core`) | `uv run pyright` |
| Правило зависимостей | `uv run lint-imports` |
| Eval (scripted-сценарии) | `uv run jarvis eval` |

Тесты разложены так: `tests/unit` — без ввода-вывода; `tests/contract` — общий набор для каждой реализации
порта; `tests/architecture` — правило зависимостей (import-linter и AST-проверки: ядро не импортирует
инфраструктуру, не работает с файлами, окружение читает только `jarvis.config`).

## Команды Jarvis

| Команда | Что делает |
| --- | --- |
| `jarvis --version` | версия |
| `jarvis config check` | проверить конфиг: синтаксис TOML, схема, неизвестные ключи |
| `jarvis config show [--sources]` | итоговые значения и слой, откуда пришло каждое |
| `jarvis eval [пути] [-s ID] [--report-dir папка]` | прогнать сценарии из `evals/scenarios`, записать отчёт `.json` и `.md` |
| `jarvis run "<запрос>" [--dry-run]` | выполнить запрос: модель выбирает действия, Jarvis исполняет их через Tool Runtime; подтверждения спрашивает в терминале, Ctrl+C отменяет задачу |
| `jarvis model check [--no-probe]` | сверить модели ролей с сервером: требования, доступность, окно контекста, structured output, схема решения исполнителя |
| `jarvis tasks [-s running\|waiting\|finished\|<статус>] [-n N]` | последние задачи: статус, маршрут, время, причина завершения |
| `jarvis trace <task_id> [--json] [--model-io]` | таймлайн задачи и метрики; `--json` — полная трасса; `--model-io` — промпты и ответы модели |
| `jarvis cancel <task_id> [--reason текст]` | отменить задачу, которую не ведёт другой живой процесс |
| `jarvis tools` | встроенные инструменты: ID, возможные эффекты, краткое описание |
| `jarvis tools show <id>` | определение инструмента: описание, эффекты, цели, таймаут, схемы аргументов и результата |
| `jarvis bench run [кандидаты.toml] [--server путь] [--only ID] [-t задача]` | бенчмарк кандидатов: запуск llama-server с параметрами кандидата, проверка GPU offload, память и скорость, датасет агентных задач, отчёты и сравнение (`benchmarks/README.md`) |
| `jarvis bench agent [--reference] [-t задача] [--label имя]` | датасет на модели из конфига (сервер запущен вручную) или на эталонных решениях (`--reference`, проверка датасета) |
| `jarvis bench compare <отчёты или папки>` | сравнить отчёты и применить правило выбора модели |

Исполнить инструмент из CLI напрямую нельзя: вызов возможен только из стадии задачи через Tool Runtime
(`jarvis run` принимает только текст запроса — инструменты выбирает модель, исполняет runtime).

Данные Jarvis лежат в `JARVIS_HOME` (по умолчанию `AppData\Local\Jarvis` в профиле пользователя
на Windows, `~/.local/share/jarvis` на Linux); конфиг — `JARVIS_HOME/config/config.toml` или путь из `JARVIS_CONFIG`.
Других переменных окружения Jarvis не читает. База задач — `JARVIS_HOME/data/jarvis.db` (SQLite, WAL);
команды `tasks`, `trace`, `cancel` создают её при первом обращении и сначала переводят в FAILED
(`interrupted`) задачи процессов, которые завершились посреди работы. Пример конфига:

```toml
schema_version = 1

[budgets.agent]
max_steps = 15

[runtime]
lease_ttl_s = 30   # через сколько секунд задача упавшего процесса считается прерванной

[policy]
workspace_roots = ["C:/projects"]   # где запись допустима с подтверждением (инструментов записи пока нет)
approval_ttl_s = 1800               # срок запроса подтверждения
```

Раздел `[models]` — в «Настройке модели» ниже.

## Настройка модели

Jarvis работает с локальной моделью через OpenAI-совместимый HTTP API. Проверенный рантайм —
llama.cpp `llama-server` ([ADR 0023](adr/0023-model-gateway-v1.md)); сервер должен слушать этот компьютер
(`localhost`, `127.0.0.1`, `[::1]`): адрес в другой сети конфиг не примет — в промпт попадают ваши файлы.

1. **llama.cpp.** На Windows с AMD — сборка с Vulkan из
   [релизов llama.cpp](https://github.com/ggml-org/llama.cpp/releases) (`llama-<сборка>-bin-win-vulkan-x64.zip`);
   на Linux можно собрать из исходников (`cmake -B build && cmake --build build --target llama-server`).
2. **Модель** — инструктивная модель в GGUF, которая помещается в VRAM вместе с окном контекста. Основная
   модель будет выбрана по бенчмарку (M4); для начала подойдёт модель 7–8B в квантовании Q4_K_M (≈5 ГБ)
   с окном 16k на 8 ГБ VRAM. Если модель «размышляет» (блоки `<think>`), отключите это через
   `extra_body` (пример ниже): ответ исполнителя ограничен 1024 токенами.
3. **Запуск сервера:**

   ```
   llama-server -m C:\models\model.gguf -c 16384 -np 1 -ngl 99 --host 127.0.0.1 --port 8080 --jinja
   ```

   `-c` — окно контекста (не меньше 8192: требование роли executor), `-np 1` — один запрос за раз и всё
   окно ему, `-ngl 99` — все слои на GPU, `--jinja` — шаблон чата из модели.
4. **Конфиг** (`JARVIS_HOME/config/config.toml`):

   ```toml
   [models]
   repair_attempts = 2              # повторов, если ответ не прошёл схему

   [models.endpoints.main]
   base_url = "http://127.0.0.1:8080/v1"
   model = "local"                  # имя модели на сервере (llama-server его не проверяет)
   request_timeout_s = 120

   [models.endpoints.main.capabilities]
   structured_output = true         # сервер применяет JSON Schema (у llama-server — да)
   context_window = 16384           # не больше, чем -c сервера

   [models.endpoints.main.sampling]
   temperature = 0.2
   # seed = 1

   # [models.endpoints.main.extra_body]          # особенности сервера и шаблона чата
   # chat_template_kwargs = { enable_thinking = false }

   [models.roles]
   executor = "main"
   ```

5. **Проверка:** `jarvis model check` — сервер отвечает, окно контекста совпадает с объявленным, проба
   structured output и проба настоящей схемы решения исполнителя проходят; печатает задержку и скорость.
6. **Запуск:** `jarvis run "какие файлы лежат в этой папке?"` в нужной папке; ход задачи — строками
   `· шаг N: …`, подтверждения — вопросом `Разрешить? [y/N]`, подробности — `jarvis trace task_N`
   и `jarvis trace task_N --model-io`.

| Симптом | Что делать |
| --- | --- |
| `сервер модели недоступен` | сервер не запущен или другой порт в `base_url` |
| `exceeds the available context size` | увеличить `-c` сервера или уменьшить `context_window` |
| `объявлен structured_output, но ответ не по схеме` | сервер не применяет JSON Schema: `structured_output = false` (схема пойдёт в промпт) |
| `окно контекста N токенов, нужно не меньше 8192` | запустить сервер с `-c 8192` или больше |

Живой тест (в CI пропускается): `JARVIS_TEST_LLM_URL=http://127.0.0.1:8080/v1 uv run pytest tests/integration/test_live_model.py`.
Другие OpenAI-совместимые серверы (LM Studio, Ollama) должны работать через тот же адаптер, но не
проверялись.

## Правила кода

- **Зависимости:** `domain` ← `ports` ← `core`; адаптеры реализуют порты; объекты собираются только в
  `app/composition.py`, конструкторами. Никаких синглтонов и реестров, заполняемых при импорте.
- **Ядро чистое:** `domain`, `ports`, `core` не делают ввода-вывода и не знают о SQLite, HTTP, MCP,
  процессах, Windows API и голосе — только через порты.
- **Без LLM там, где хватает кода:** переходы состояний, права, бюджеты, таймауты, ID, проверка схем —
  детерминированный код.
- **Без заготовок на будущее:** поле модели, порт или настройка появляются вместе с первым потребителем.
- **Ошибки:** сбои — исключения из таксономии (`jarvis.domain.errors`) с категорией и диспозицией;
  ожидаемые исходы — значения.
- **Изменение архитектуры** — только явно: какой ADR затронут, почему, что меняется.
- **Коммиты** небольшие и по одной теме: `feat(core): …`, `test(core): …`, `docs: …`.

## Как устроен M1

`TaskService.submit` создаёт задачу в CREATED; `run_until_blocked` продвигает её тактами `TaskRunner`:
загрузить задачу → проверить отмену и бюджет → вызвать стадию текущего состояния → проверить
`StageOutcome` по таблице переходов → записать контрольную точку (строка задачи + событие
`task.transition` и события, которые его объясняют, одной транзакцией вместе с проверкой аренды).
Стадии получают `BudgetMeter` и списывают расход до действия. Пока стадии — scripted
(`jarvis.evals.scripted`): так проверяется механика ядра без модели и инструментов.

Хранилище: `adapters.memory` (тесты, eval) и `adapters.sqlite` (CLI) проходят один набор контрактных
тестов (`tests/contract`). Аренды и восстановление — [ADR 0021](adr/0021-task-leases-and-optimistic-unit-of-work.md).

## Как устроен Tool Runtime (Session 3)

Стадия вызывает инструмент только через `ToolRuntime.call(task, budget, tool_id, arguments)`:
реестр → цель → схема аргументов → `preview` (канонические пути, эффекты вызова) → `PolicyEngine`
(трасса `policy.decided` и аудит — до исполнения) → отказ, запрос подтверждения или dry run → бюджет →
повторный `preview` перед побочным эффектом → `execute` нормализованных аргументов с таймаутом и
отменой → схема результата → `verify` → трасса и аудит. Отказ, ожидание подтверждения и dry run —
значения `ToolOutcome`; сбои — исключения `ToolError`. Вызову, которому нужен человек, стадия отвечает
переходом в WAITING_CONFIRMATION; `TaskService.resolve_approval` записывает решение, `run_until_blocked`
продолжает задачу, и стадия доводит тот же вызов (`ToolRuntime.resume`).

Инструменты — `jarvis/adapters/tools` (только чтение). Новый инструмент: модели аргументов и результата
с `extra="forbid"`, `preview` без побочных эффектов и с каноническими путями, `verify` с проверяемыми
постусловиями, регистрация в `builtin_tools()`, пример в `tests/contract/test_tool_contract.py`.
В unit-тестах — `tests/fakes.FakeTool`, в интеграционных — временные папки.
Решения — [ADR 0022](adr/0022-tool-runtime-v1.md).

## Как устроен агент (Session 4)

`jarvis run` создаёт задачу; ROUTING и PLANNING пока передают её агенту без модели. Такт EXECUTING
(`core.agent.stages.Executor`): шаг бюджета → промпт `executor.v1` (`core.agent.context`: правила,
инструменты, запрос, история; результаты вызовов — только блоками DATA) → `ModelGateway.generate`
(`core.models.gateway`: схема решения из реестра инструментов, разбор, проверка, ремонт, бюджет, запись
`model_calls` и `model.called`) → действие `tool` уходит в `ToolRuntime.call`, итог становится
наблюдением в `Task.state`; действие `finish` ведёт в VERIFYING, где ответ проверяется по исполненным
вызовам. Модель ничего не исполняет сама: её выход — проверенный JSON.

Бэкенды моделей — `jarvis/adapters/models` (создаёт только `app/composition.py`); в тестах и eval —
`jarvis.evals.models.ScriptedModel` (реплики по порядку). Сценарий eval с полем `model` ведут настоящие
стадии агента со scripted-моделью (`evals/scenarios/agent`, `evals/scenarios/model`); ожидания
`observations`, `answer_contains`, `data_only`. Адаптер проверяется на записанных ответах llama-server
(`tests/fixtures/llama_server`), CLI — на заглушке сервера по настоящему HTTP
(`tests/integration/llm_stub.py`). Решения — [ADR 0023](adr/0023-model-gateway-v1.md).

## Бенчмарк модели (Session 4.5)

`jarvis.evals.bench` — код бенчмарка, не продукта: `server.py` запускает `llama-server` с явными
параметрами кандидата (`Candidate`, `server_args`), разбирает лог (`parse_server_log`) и выносит вердикт
offload (`offload_verdict`), меряет память и скорость; `dataset.py` — датасет и условия задач;
`agent.py` — прогон задачи по настоящему пути Jarvis во временной рабочей папке (подтверждения
отклоняются, настоящие `~/.ssh` и `JARVIS_HOME` закрыты); `grading.py` — оценка и сводка; `report.py` —
отчёты и правило выбора; `run.py` — сценарии прогона. Ядро о бенчмарке не знает.

Проверки: `tests/unit/evals/test_bench_agent.py` (датасет, эталон на 100 %, ошибки модели, которые
должен увидеть оценщик), `test_bench_server.py` (аргументы, логи CPU и Vulkan, offload, отчёты),
`tests/integration/test_cli_bench.py` (CLI). Живой прогон с настоящим сервером:

```bash
JARVIS_TEST_LLAMA_SERVER=/путь/к/llama-server JARVIS_TEST_GGUF=/путь/к/model.gguf \
    uv run pytest tests/integration/test_live_bench.py
```

Протокол прогона на ПК — `benchmarks/README.md`, методика — [ADR 0024](adr/0024-agent-benchmark.md).
