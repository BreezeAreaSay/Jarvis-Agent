# Handoff — Session 4: Local Model Gateway + первый агент на модели

Работа шла в ветке `claude/charming-mayer-tfvcc6`, начатой от `main` (`2a876fa`, конец Session 3); в
`main` её нужно влить (перемоткой — история линейная). Все локальные проверки зелёные. GitHub Actions,
как и раньше, не проверялись; на Windows код не запускался.

Задание пришло обрезанным на пункте «6. Model configuration»; пункты 1–5 выполнены как написано, раздел
конфигурации модели и всё дальше (агент, CLI, eval, документация) — по архитектуре Stage 0 и ADR 0008/0009.

## Goal

Подключить настоящую локальную LLM через абстрактный Model Gateway и впервые получить путь

```
естественный язык → модель → структурированное решение → проверенный ToolCall → Tool Runtime → политика
→ исполнение → проверка → ответ модели
```

при том что модель не получает никаких прямых системных возможностей: её единственный выход — JSON,
который ядро проверяет и передаёт в Tool Runtime.

## Implemented

- **Домен** (`domain/models.py`, `domain/agent.py`): `ModelRole` (только `executor`), `ModelCapabilities`
  (`structured_output`, `context_window`), `ModelInfo`, секции промпта с доверием (`Prompt`,
  `PromptSection`, `Trust`), `BackendRequest`/`BackendResponse`/`BackendStatus`, `ModelCallRecord`;
  действия агента `ProposedAction{decision ≤ 280, action: tool | finish}`, рабочая память `AgentState`
  (шаги, вызовы, наблюдения, отвергнутые ответы) в `Task.state`; ошибки `ModelError` →
  `ModelUnavailable`/`ModelTimeout`/`ModelRequestRejected`, `InvalidModelOutput`, `VerificationFailed`;
  `Origin.CLI`.
- **Настройки** (`domain/settings.py`): раздел `models` — `endpoints.<id>` (`base_url` только loopback,
  `model`, `request_timeout_s`, `capabilities`, `sampling`, `extra_body`), `roles`, `repair_attempts`.
- **Порт** `ModelBackend` (`ports/models.py`): `info`, `complete`, `describe`; `ModelCallRepository` в
  единице работы.
- **Хранилище:** миграция `003_agent_and_model_calls.sql` (`tasks.state_json`, `model_calls`), оба
  адаптера, общий контракт; база Session 3 обновляется с сохранением задач.
- **Model Gateway** (`core/models/gateway.py`): требования ролей при старте; схема в запрос только при
  `structured_output`, иначе — в промпт; разбор (в том числе ```json и текст вокруг), схема pydantic,
  семантическая проверка, ремонт с объяснением; обрезанный ответ называется прямо; повтор при
  недоступном сервере; бюджет вызовов и токенов на каждую попытку; запись `model_calls` и событие
  `model.called` на каждую попытку, включая сбой и отмену.
- **Промпт** (`core/models/prompt.py`): системное сообщение — правила Jarvis; остальное — секции по
  доверию; данные — блоки DATA, которые содержимое не может закрыть (property-тест).
- **Агент** (`core/agent/`): `actions.py` — схема решения с ветвью на каждый инструмент (его схема
  аргументов, `const` ID) и `evidence` только из исполненных вызовов, та же проверка после разбора;
  `context.py` — промпт `executor.v1`, правило про инструменты из их эффектов, укладка в окно (старые
  данные опускаются); `stages.py` — ROUTING/PLANNING без модели (до M7/M8), `Executor` (такт = шаг),
  `AnswerVerifier` (без модели). Runner сохраняет рабочую память в контрольной точке.
- **Адаптер** `adapters/models/openai_compat.py` (httpx): llama-server и любой OpenAI-совместимый сервер;
  `trust_env=False`, без перенаправлений; `<think>` отбрасывается; ошибки HTTP и связи → ошибки модели;
  `describe()` читает `/v1/models` и `/props` llama.cpp.
- **Сборка:** `build_app` без `stages` собирает агента с Gateway над бэкендами из конфига (или
  переданными — scripted-модель); `App.models`.
- **Трасса и метрики:** события `model.called` и `action.proposed`; таймлайн показывает шаги, попытки,
  токены; метрики считают вызовы модели и токены; `trace --model-io` — промпты и ответы.
- **CLI:** `jarvis run "<запрос>" [--dry-run]` (ход задачи, вопрос человеку, Ctrl+C → отмена задачи),
  `jarvis model check [--no-probe]`, `jarvis trace --model-io`.
- **Eval:** сценарий ведут scripted-стадии (`script`) или настоящий агент со scripted-моделью (`model`);
  ожидания `observations`, `answer_contains`, `data_only`; 14 новых сценариев.

## Model Gateway contract

```python
class ModelBackend(Protocol):                       # ports/models.py — реализуют адаптеры
    @property
    def info(self) -> ModelInfo: ...                # endpoint, model (только для журналов), capabilities
    async def complete(self, request: BackendRequest) -> BackendResponse: ...
    async def describe(self) -> BackendStatus: ...  # модели, n_ctx, сборка сервера

class ModelGateway:                                 # core/models/gateway.py
    def available(self, role) -> bool
    def capabilities(self, role) -> ModelCapabilities
    def prompt_budget(self, role) -> int            # окно минус ответ роли
    async def generate(self, role, prompt, output: StructuredOutput[T], *, task_id, budget) -> Generation[T]

ROLE_REQUIREMENTS = {executor: (окно ≥ 8192, ответ 1024 токена)}
```

Решения по возможностям: `structured_output` → схема на сервер или в промпт; `context_window` →
требование роли и окно для промпта. Имени модели ядро не читает (архитектурный тест).

## Agent loop

```
ROUTING   → PLANNING  (маршрут agent; роутера пока нет)
PLANNING  → EXECUTING (плана пока нет)
EXECUTING, такт:
  есть вызов, ждавший человека   → ToolRuntime.resume → наблюдение → EXECUTING
  иначе: шаг бюджета → промпт executor.v1 → Gateway.generate(схема решения)
    не прошло и после ремонта     → сбой (failures), отвергнутый ответ в памяти → EXECUTING
    finish                        → VERIFYING
    tool → ToolRuntime.call:
      EXECUTED                    → наблюдение (вывод — DATA, до 6000 байт) → EXECUTING
      DENIED / сбой инструмента   → наблюдение, сбой (failures) → EXECUTING
      DRY_RUN                     → наблюдение → EXECUTING
      NEEDS_APPROVAL              → WAITING_CONFIRMATION (вызов в памяти как ждущий)
VERIFYING: evidence ⊆ исполненные и проверенные вызовы → COMPLETED (ответ), иначе FAILED
```

Шаги, вызовы модели (каждая попытка), токены, вызовы инструментов, сбои и время — из бюджета маршрута
`agent`; зациклившаяся модель останавливается им.

## Security guarantees

- **Единственный путь к компьютеру — Tool Runtime.** Модель возвращает только JSON; Gateway и бэкенд
  модели не импортируют ни инструментов, ни хранилищ, агент не видит реестра и порта инструментов
  (архитектурные тесты). `jarvis run` принимает только текст запроса.
- **Схема решения из реестра:** несуществующий инструмент, чужие аргументы и ссылка на чужой вызов не
  проходят ни грамматику сервера, ни проверку после разбора; аргументы ещё раз проверяет Tool Runtime;
  политика и подтверждение — те же, что в Session 3.
- **Данные — не инструкции:** вывод инструментов, текст ошибок и причины отказа (в них пути из
  аргументов) попадают к модели только блоками DATA; `summary` наблюдения пишет Jarvis. Права из данных
  не выводятся: в eval `agent.injection` «поддавшаяся» модель получает отказ политики и вопрос
  человеку. Признак `tainted` отложен: Policy v1 не разрешает без человека ни одного побочного эффекта
  (property-тест); он обязан прийти с первым таким правилом (ADR 0023).
- **Только этот компьютер:** `base_url` — только loopback; httpx не читает прокси и сертификаты из
  окружения и не следует перенаправлениям.
- **Без модельной специфики в ядре:** в `core` и `ports` нет имён моделей и рантаймов, параметров
  сэмплирования и деталей API (архитектурный тест с образцами нарушений).
- **Терминал:** ответ модели, решения и данные в CLI выводятся без управляющих символов.
- **Журналы:** трасса — без текста промптов и ответов; промпты и ответы — в `model_calls` (данные Jarvis,
  инструментам недоступны), в том числе прочитанное с разрешения пользователя.

## Tests

795 тестов: 792 прошли, 3 пропущены (права на файл под root; два живых теста без `JARVIS_TEST_LLM_URL`).
Новое:

- `tests/unit/core/test_model_gateway.py` — требования ролей, схема на сервер или в промпт, ремонт,
  обрезанный ответ, семантическая проверка, исчерпание попыток, повтор при недоступности, бюджет, отмена,
  хеш промпта.
- `tests/unit/core/test_prompt.py` — уровни доверия; property-тесты: данные не закрывают и не открывают
  блок DATA.
- `tests/unit/core/test_agent.py` — путь агента на scripted-модели: событий по порядку, схема только из
  реестра, ремонт до исполнения, отвергнутый шаг, evidence, отказ политики, сбой инструмента,
  подтверждение с продолжением другим процессом, отказ человека, инъекция, dry run, недоступная модель,
  модель не настроена, бюджет шагов, отмена посреди вызова модели, сервер без схемы, укладка в окно.
- `tests/unit/core/test_policy.py` — property: вызов с побочным эффектом никогда не ALLOW.
- `tests/contract/test_model_backend.py` — контракт для scripted-модели и адаптера на записанных ответах
  llama-server (`tests/fixtures/llama_server`), разбор ошибок, окружение не читается.
- `tests/integration/test_cli_agent.py` — `jarvis run`, подтверждение «да»/«нет», `trace --model-io`,
  `jarvis model check` (совпадает, не совпадает, сервер недоступен) по настоящему HTTP к заглушке.
- `tests/integration/test_live_model.py` — живой сервер (по `JARVIS_TEST_LLM_URL`).
- `tests/architecture/test_model_boundary.py`, хранилище (`model_calls`, `state_json`, обновление базы
  Session 3), домен (настройки, действия, память), таймлайн и метрики.

## Live verification

llama.cpp собран из исходников в контейнере (коммит `b92761a`, CPU, `llama-server`). Модель — крошечная
qwen2 со случайными весами и настоящим токенизатором Qwen2 (HuggingFace из контейнера недоступен):
смысла в ответах нет, но API, грамматика по JSON Schema, тайминги и ошибки — настоящие.

- `jarvis model check`: сервер, окно 8192, проба structured output и проба полной схемы решения
  исполнителя (ветви по инструментам, ограничения аргументов) — проходят; сервер строит по ней грамматику.
- `jarvis run`: обрезанные ответы уходят в ремонт с подсказкой, `system.cwd` исполняется через Tool
  Runtime, `finish` проходит проверку → COMPLETED; при выключенном сервере — `сервер модели недоступен`.
- Живой тест с `JARVIS_TEST_LLM_URL` — 2 из 2.

Качество решений на настоящей модели **не измерялось** — это первый шаг на ПК пользователя (ниже).

## Evals

32 из 32: прежние 18 и `agent.list_files`, `agent.read_and_answer`, `agent.search`, `agent.injection`,
`agent.approval_secret`, `agent.unknown_tool`, `agent.dry_run`, `agent.budget_steps`, `agent.cancel`,
`model.repair`, `model.invalid_output`, `model.unconstrained`, `model.unavailable`, `budget.wall_time`.
Все — scripted-модель; live-режима eval ещё нет.

## Known limitations

- Роутера и планировщика нет: любой запрос ведёт агент; нет `step_done`/`replan`, детектора зацикливания
  (бюджет останавливает), перепланирования после неудачной проверки ответа.
- Одна роль; сэмплирование — на эндпоинт, не на роль.
- Запросы к модели не потоковые: времени до первого токена нет (только `timings.prompt_ms` llama.cpp).
- Токены для укладки промпта оцениваются сверху (байты/3); точного подсчёта через сервер нет.
- Признак `tainted` не реализован (см. гарантии).
- `model_calls` пишет промпты всегда; настройки `trace.keep_prompts` нет.
- LM Studio и Ollama через тот же адаптер не проверялись.
- Подтверждения только в `jarvis run`; команд `jarvis approvals/approve/deny/resume` по-прежнему нет —
  задачу, оставленную в ожидании, можно только отменить.
- Live-режима `jarvis eval` нет; выбор модели — после бенчмарка M4.

## Architecture deviations

Все — в [ADR 0023](../adr/0023-model-gateway-v1.md): одна роль; две возможности (`structured_output`
включает `constrained_decoding`); `generate` без `generate_text`/`count_tokens`; длина ответа — в роли;
порт `info/complete/describe`; конфиг `models.endpoints.<id>` без `model_profiles`; пакеты `core/models`
и `adapters/models`; ROUTING/PLANNING без модели; действия `tool | finish`; рабочая память в задаче;
`tainted` отложен; запросы не потоковые.

## Review

(заполняется по итогам независимого ревью)

## Commits

```
32d6bfd feat(models): add model, prompt and agent domain contracts
6d57164 feat(storage): persist agent state and model calls
83321b5 feat(models): add the Model Gateway with structured output and repair
1eb4862 feat(models): add the OpenAI-compatible local backend for llama-server
e32c3ec feat(agent): drive tasks with the model through the Tool Runtime
75d5ce6 feat(evals): run agent scenarios on a scripted model
e21a430 feat(cli): add jarvis run, jarvis model check and trace --model-io
3bb058d test(models): add an opt-in live model check
71a61c4 fix(agent): derive the executor's tool rule from declared effects
ac8924b docs: record Model Gateway v1 in ADR 0023 and add the model setup guide
```

## Next recommended session

1. **На ПК пользователя (RX 7600):** llama-server с Vulkan и моделью 7–8B Q4_K_M по
   `docs/development.md` → `jarvis model check` → несколько `jarvis run` → `JARVIS_TEST_LLM_URL=… uv run pytest
   tests/integration/test_live_model.py`; если модель «размышляет» — `extra_body`.
2. **M4 часть 1:** `jarvis bench hardware` — 2–3 кандидата: загрузка, VRAM, скорость, время до первого
   токена (потоковый запрос), доля валидных решений на схеме исполнителя; live-режим `jarvis eval` для
   сценариев `agent.*`; предварительный выбор модели.
3. Подтверждения из CLI для оставленных задач (`jarvis approvals | approve | deny | resume`).
4. Дальше — M7 (роутер) и M8 (планировщик, проверки, детектор зацикливания).

## Exact starting point

- Ветка `claude/charming-mayer-tfvcc6` (влить в `main`); `uv sync`, затем `uv run ruff check`,
  `uv run ruff format --check`, `uv run pyright`, `uv run lint-imports`, `uv run pytest -q`,
  `uv run jarvis eval` — всё зелёное.
- Новая роль модели: требования в `ROLE_REQUIREMENTS`, стадия, которая вызывает `Gateway.generate` со
  своей `StructuredOutput`, назначение в `[models.roles]`.
- Новый инструмент сразу виден агенту: схема решения и раздел «Инструменты» строятся из реестра.
