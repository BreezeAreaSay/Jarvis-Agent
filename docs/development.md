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
| Eval (scripted-сценарии и набор фраз Router) | `uv run jarvis eval` |

Тесты разложены так: `tests/unit` — без ввода-вывода; `tests/contract` — общий набор для каждой реализации
порта; `tests/architecture` — правило зависимостей (import-linter и AST-проверки: ядро не импортирует
инфраструктуру, не работает с файлами, окружение читает только `jarvis.config`).

## Команды Jarvis

| Команда | Что делает |
| --- | --- |
| `jarvis --version` | версия |
| `jarvis config check` | проверить конфиг: синтаксис TOML, схема, неизвестные ключи |
| `jarvis config show [--sources]` | итоговые значения и слой, откуда пришло каждое |
| `jarvis eval [пути] [-s ID] [--report-dir папка]` | прогнать сценарии из `evals/scenarios` и наборы фраз Router из `evals/routing`, записать отчёт `.json` и `.md` |
| `jarvis run "<запрос>" [--dry-run] [--mode M] [--local-only] [--allow-cloud КЛАСС]` | выполнить запрос: прямая команда — без модели, остальное — модель выбирает действия; исполняет всегда Tool Runtime; подтверждения (и согласие на облако) спрашивает в терминале, Ctrl+C отменяет задачу. `--mode`: auto, local_only, smart, coding |
| `jarvis route "<запрос>" [--json] [--cwd папка] [--mode M]` | решение Router без исполнения: стратегия, уровень, режим, намерение, сущности, правила, инструмент прямой команды, сколько вызовов модели потребуется, время решения |
| `jarvis model check [--no-probe] [--remote]` | сверить модели ролей с сервером: требования, доступность, окно контекста, structured output, схема решения исполнителя; облачные провайдеры: цепочки, задан ли ключ (без значения), с `--remote` — пробный запрос |
| `jarvis tasks [-s running\|waiting\|finished\|<статус>] [-n N]` | последние задачи: статус, маршрут, время, причина завершения |
| `jarvis trace <task_id> [--json] [--model-io]` | таймлайн задачи и метрики; `--json` — полная трасса; `--model-io` — промпты и ответы модели |
| `jarvis cancel <task_id> [--reason текст]` | отменить задачу, которую не ведёт другой живой процесс |
| `jarvis tools` | встроенные инструменты: ID, возможные эффекты, краткое описание |
| `jarvis tools show <id>` | определение инструмента: описание, эффекты, цели, таймаут, схемы аргументов и результата |
| `jarvis bench run [кандидаты.toml] [--server путь] [--only ID] [-t задача]` | бенчмарк кандидатов: запуск llama-server с параметрами кандидата, проверка GPU offload, память и скорость, датасет агентных задач, отчёты и сравнение (`benchmarks/README.md`) |
| `jarvis bench agent [--reference] [-t задача] [--label имя]` | датасет на модели из конфига (сервер запущен вручную) или на эталонных решениях (`--reference`, проверка датасета) |
| `jarvis bench compare <отчёты или папки>` | сравнить отчёты и применить правило выбора модели |

Исполнить инструмент из CLI напрямую нельзя: вызов возможен только из стадии задачи через Tool Runtime
(`jarvis run` принимает только текст запроса — инструмент выбирает Router или модель, исполняет runtime).

### Прямые команды (V2.1)

Частые команды Router ([ADR 0026](adr/0026-orchestration-router-and-strategies.md)) распознаёт без модели и
исполняет через Tool Runtime за миллисекунды — модель для них не нужна вовсе, даже не настроенная:

| Что | Примеры | Инструмент |
| --- | --- | --- |
| запустить приложение из инвентаря | «открой телеграм», «запусти браузер», «open VS Code» | `app.launch` |
| открыть веб-адрес (только http/https) | «открой github.com», «перейди на ya.ru» | `url.open` |
| открыть папку | «открой загрузки», «открой папку src», «открой папку ~/projects» | `folder.open` |
| текущая папка | «где я», «pwd» | `system.cwd` |
| содержимое папки | «покажи файлы», «что в загрузках?», «покажи файлы в папке docs» | `filesystem.list` |
| найти файл | «найди README», «найди все pdf», «где лежит config.toml» | `filesystem.search` |
| процессы | «покажи процессы», «покажи процессы python» | `process.list` |

Правило — точность важнее полноты: если команда не распознана целиком (вопрос, составная задача,
опечатка, неизвестное приложение), задачу ведёт агент на модели. Вопрос («открыть браузер?») ничего не
запускает. Приложения — только из инвентаря компьютера (Windows: ярлыки меню «Пуск» и App Paths;
Linux: desktop-файлы), без произвольных путей и командной строки. Запуск по прямой команде проходит
без подтверждения; тот же запуск, предложенный моделью, — только с подтверждением человека
([ADR 0030](adr/0030-direct-actions-and-launch-policy.md)). Как Router понял запрос — `jarvis route
"<запрос>"`; в ходе `jarvis run` — строка `· маршрут: …`, в `jarvis trace` — событие `route` с ID правил.

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
2. **Модель** — основная локальная модель по итогам бенчмарка — **Qwen3-8B Q4_K_M** (GGUF ≈5 ГБ, окно
   16k, все слои на GPU; [ADR 0025](adr/0025-local-model-choice.md)). Другая инструктивная модель
   7–8B, которая помещается в VRAM вместе с окном контекста, подключается так же. Qwen3 «размышляет»
   (блоки `<think>`): отключите это (`--reasoning off` у сервера или `extra_body`, пример ниже) — ответ
   исполнителя ограничен 1024 токенами. Куда идёт Jarvis дальше (маршрутизация, облако, внешние
   агенты) — [Architecture V2](architecture-v2/README.md).
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
   # max_output_tokens = 4096        # лимит вывода эндпоинта за ответ, если он есть (ADR 0027)
   # reasoning_behavior = "shares_output"  # рассуждения тратят тот же лимит вывода (по умолчанию none)
   # reasoning_budget = 2048         # сколько из лимита отдать рассуждениям, если это известно

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
| `max_output_tokens … меньше ответа роли` или `… не помещаются в max_output_tokens` | лимит вывода эндпоинта меньше ответа исполнителя (1024) с бюджетом рассуждений: увеличить лимит, уменьшить `reasoning_budget` или отключить рассуждения |

Живой тест (в CI пропускается): `JARVIS_TEST_LLM_URL=http://127.0.0.1:8080/v1 uv run pytest tests/integration/test_live_model.py`.
Другие OpenAI-совместимые серверы (LM Studio, Ollama) должны работать через тот же адаптер, но не
проверялись.

## Облачные провайдеры (V2.2)

Jarvis работает и без облака: простое — прямые команды без модели, остальное — локальная Qwen3-8B.
Облако — ускоритель интеллекта для сложных задач, и оно **выключено по умолчанию**
([ADR 0027](adr/0027-hybrid-model-providers.md), [ADR 0028](adr/0028-cloud-privacy-boundary.md)).

**Кто что решает.** Router выбирает уровень модели: `local` (по умолчанию), `smart` (глубокий анализ),
`coding` (работа с кодом); в режиме `auto` — по сильным признакам в начале запроса («проанализируй…»,
«напиши функцию…»), сомнение — `local`. Конкретного провайдера внутри уровня выбирает Model Gateway по
цепочке `[models.routing]`; основная локальная модель — последней в каждой цепочке. Уровни `fast` и
`local` и режим `local_only` облако не вызывают никогда.

**Подключение.** Удалённый провайдер — любой OpenAI-совместимый API (шаблоны: [configs/cloud](../configs/cloud/README.md)):

```toml
[cloud]
enabled = true                      # по умолчанию false: без этого — ни одного удалённого вызова
allow_file_content = false          # содержимое файлов — только с согласием на задачу
allow_source_code = false           # код — только с согласием
allow_personal_data = false         # личные папки (Документы, Рабочий стол, Загрузки …) — только с согласием
private_roots = ["C:/work/commercial"]   # отсюда не уходит ничего и никогда

[models.remote.smart_primary]
base_url = "https://api.provider.example/v1"   # только https; перенаправлениям Jarvis не следует
model = "<имя модели у провайдера>"
api_key = "env:JARVIS_SMART_API_KEY"           # ссылка на переменную окружения; ключ в конфиге — ошибка
json_object = true                             # провайдер не применяет JSON Schema, но умеет JSON-объект
[models.remote.smart_primary.capabilities]
structured_output = false
context_window = 131072
max_output_tokens = 16384

[models.routing]
mode = "auto"                       # auto | local_only | smart | coding
smart = ["smart_primary"]           # локальная модель добавляется последней сама
coding = ["smart_primary"]
max_providers = 3                   # попыток на один вызов модели, последняя — локальная
```

**Что уходит в облако.** Перед **каждым** удалённым вызовом граница приватности проверяет ровно тот
промпт, который уйдёт: классы данных по происхождению (запрос; результат инструмента — содержимое файла,
код по расширению, личные папки, `private_roots`, зона секретов) и поиск секретов по тексту.

| Класс | По умолчанию |
| --- | --- |
| `local_metadata` (запрос, имена файлов, процессы) | уходит |
| `file_content`, `source_code`, `personal_data` | только с настройкой или согласием на задачу |
| `private` (`private_roots`, задача, начатая там), `secrets` (`.env`, `*.pem`, `.ssh`, найденные ключи) | никогда |

Когда облачному провайдеру мешает только неразрешённый класс, Jarvis один раз за задачу спрашивает
разрешение тем же подтверждением, что и для инструментов (служебный инструмент `cloud.share`, модель
его не видит). Отказ — задача продолжается локально. Заранее: `jarvis run --allow-cloud source_code "…"`.

**Fallback.** Провайдер не ответил по своей причине (нет связи, таймаут, 401/403, 429, квота, неверная
настройка) — тот же вызов переходит к следующему кандидату цепочки, затем к локальной модели; каждая
попытка — из бюджета `model_calls`. Состояние провайдера (`rate_limited`, `limit_exceeded`, …)
запоминается со сроком (в SQLite: следующая команда не бьёт в исчерпанный лимит). Ответ не по схеме после
ремонта, переполнение окна и отмена к fallback не ведут.

**Как понять, что произошло.** В ходе `jarvis run` — строки `· модель smart → smart_primary (облако)`,
`облако …: нужно согласие на source_code`, `smart_primary не ответил (model_rate_limited) → main`; в
`jarvis trace` — события `plan`, `privacy`, `fallback`, у вызова модели — эндпоинт и пометка «облако».
Ключей, заголовков авторизации и текста промпта в трассе нет. Каждый удалённый вызов — запись аудита
`egress` (провайдер, классы данных, размер, хеш промпта).

| Симптом | Что делать |
| --- | --- |
| `… не ответил (model_misconfigured)` сразу | переменной из `api_key` нет в окружении, неверный `base_url` или `model` (`jarvis model check`) |
| `model_auth_required` | ключ не принят провайдером: проверьте переменную окружения |
| `model_rate_limited` / `model_limit_exceeded` | лимит запросов или квота: провайдер пропускается до конца срока, отвечает следующий кандидат |
| облако не вызывается вовсе | `cloud.enabled = false`, режим `local_only`, уровень `local` или граница приватности (`jarvis trace`) |

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

Инструменты — `jarvis/adapters/tools`: чтение (`builtin_tools()`) и запуск с эффектом `LAUNCH`
(`launch_tools()`; набор по умолчанию — `host_tools()`). Новый инструмент: модели аргументов и результата
с `extra="forbid"`, `preview` без побочных эффектов и с каноническими путями, `verify` с проверяемыми
постусловиями, регистрация в `adapters/tools/__init__.py`, пример в `tests/contract/test_tool_contract.py`.
В unit-тестах — `tests/fakes.FakeTool`, в интеграционных — временные папки.
Решения — [ADR 0022](adr/0022-tool-runtime-v1.md).

## Как устроен агент (Session 4)

`jarvis run` создаёт задачу; ROUTING решает Router (ниже), PLANNING передаёт задачу агенту без модели.
Такт EXECUTING
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

## Как устроены Router и прямые команды (V2.1)

Стадия ROUTING — `core.routing.router.RoutingStage`: `Router.decide(текст, рабочая папка)` возвращает
`RouteDecision` (стратегия, уровень, намерение, сущности, ID правил, причина), стадия пишет его в задачу
(`tasks.routing_json`, миграция 004) и событие `route.decided` с временем решения. Router — чистая
функция над текстом и портом `Inventory`: модели, инструментов и политики он не видит (архитектурные
тесты), словарь команд — `core/routing/lexicon.py`, названий программ в ядре нет — их знает инвентарь
(`adapters/inventory`: Windows, XDG, статический для тестов и eval). У бюджета `routing` вызовов модели
нет вовсе.

DIRECT ведёт `core.direct.stage.DirectStage`: аргументы вызова — только из сущностей решения
(`core.direct.commands.direct_call`), вызов — `ToolRuntime.call(..., invoker=Invoker.DIRECT)`, ответ —
шаблон по результату инструмента. Model Gateway этой стадии не передаётся, а бюджет `direct` не
допускает вызовов модели. Подтверждение, отказ и dry run — тот же путь, что у агента; отказ политики
завершает задачу (модели, которая искала бы обход, здесь нет). CLARIFY завершает задачу вопросом.

Точность проверяет набор фраз `evals/routing/dataset.yaml` (`kind: routing_dataset`): ложных DIRECT и
DIRECT не того действия должно быть 0, полнота и p95 решения — с порогами набора; он же — параметры
`tests/unit/core/test_router.py`. Новая прямая команда: шаблон в `router.py` и слова в `lexicon.py`,
вызов и ответ в `core/direct/commands.py`, фразы — положительные и трудные отрицательные — в набор,
сценарий `evals/scenarios/direct/*.yaml` (инвентарь сценария — поле `inventory`, запуск в eval только
записывается: ожидание `launched`). Решения — [ADR 0026](adr/0026-orchestration-router-and-strategies.md),
[ADR 0030](adr/0030-direct-actions-and-launch-policy.md).

## Как устроены гибридные провайдеры (V2.2)

`core/models/gateway.py` строит план на каждый вызов модели (`ModelGateway.plan`): цепочка уровня из
`RoutingSettings` → фильтры (вид провайдера × уровень и режим, `cloud.enabled`, состояние из
`ProviderAvailability`, требования роли, `CloudPrivacyPolicy`) → порядок и предел `max_providers`. Затем
`generate` проходит кандидатов: перед отправкой удалённому — повторная проверка того, что уходит
(`_guard`), ошибки провайдера (`PROVIDER_FAILURES`) — запись состояния и `model.fallback`. Порт
`ModelBackend` не изменился; удалённый адаптер — `adapters/models/remote.py`, ключ ему передаёт
сборка приложения (`jarvis.config.resolve_secret`). Классы данных результата ставит
`core/tools/provenance.py` (`DataClassifier`), промпт несёт их в `Prompt.data_classes`
(`core/agent/context.py`), согласие — `Executor._ask_consent` через инструмент `cloud.share`
(`adapters/tools/consent.py`, политика `cloud.share.consent`).

Тесты: `tests/contract/test_remote_model_backend.py` (адаптер на поддельном HTTP), `tests/unit/core/
test_hybrid_gateway.py` (план, fallback, состояние, согласие, секреты), `test_hybrid_properties.py`
(инварианты: local_only, уровни local/fast, выключенное облако, ограниченный fallback),
`test_cloud_privacy.py` (политика, поиск секретов, происхождение), `tests/unit/config/test_cloud_config.py`
(ссылки на ключи), сценарии `evals/scenarios/cloud` (scripted-провайдеры, ожидания `model_endpoints`,
`privacy`, `cloud_never_saw`). Помощник для тестов — `tests/hybrid.py`. Настоящих провайдеров и ключей в
тестах нет.

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
