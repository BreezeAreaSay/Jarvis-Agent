# 04. Инструменты и навыки

## 1. Tool API

Инструмент — атомарное действие с типизированным контрактом. Это единственный способ, которым
Jarvis влияет на мир: рецепты, агент, GUI-драйвер, плагины и MCP-серверы вызывают инструменты
только через Tool Runtime.

### Контракт (эскиз)

```python
class Risk(IntEnum):
    SAFE = 0          # чтение, открытие приложения, громкость
    MEDIUM = 1        # обратимые изменения в доверенных зонах
    HIGH = 2          # удаление, установка, отправка наружу, неизвестные команды
    CRITICAL = 3      # платежи, секреты, защита системы; запрещено по умолчанию

class ToolSpec(BaseModel):
    name: str                         # "audio.set_volume"
    version: int = 1
    summary: str                      # ≤ 160 символов, для LLM
    args_model: type[BaseModel]       # → JSON Schema для модели и валидации
    result_model: type[BaseModel]
    effects: Literal["none", "local", "external"]
    base_risk: Risk                   # отправная точка; итог считает Policy Engine
    capabilities: list[str]           # шаблоны: "fs.write:{path}", "proc.exec:{argv0}", "net:{host}"
    idempotent: bool
    reversible: bool
    timeout_s: float
    platforms: frozenset[str]         # {"windows", "linux"}
    llm_visible: bool = True          # False — только для рецептов и внутренних вызовов

class Tool(Protocol):
    spec: ToolSpec
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutput: ...
    async def preview(self, args: BaseModel, ctx: ToolContext) -> Preview | None: ...       # что произойдёт: diff, список файлов
    async def check(self, args: BaseModel, out: ToolOutput, ctx: ToolContext) -> CheckResult | None: ...  # постусловие
    async def undo(self, effect: Effect, ctx: ToolContext) -> None: ...                     # если reversible
```

`ToolContext` даёт инструменту только то, что ему нужно: идентификатор задачи, токен отмены,
порты платформы, хранилище артефактов, логгер/span, резолвер секретов (по хэндлам), среду
исполнения. Глобального состояния и прямого доступа к ОС в обход портов нет.

Результат, который видят агент и трасса:

```python
class ToolResult(BaseModel):
    call_id: str
    status: Literal["ok", "error", "denied", "cancelled", "timeout"]
    data: dict | None                 # типизированный результат
    summary: str                      # то, что попадёт в контекст LLM (с лимитом)
    artifacts: list[ArtifactRef]      # полный вывод, скриншоты, файлы
    effects: list[Effect]             # что изменилось (для аудита и отмены)
    check: CheckResult | None         # постусловие
    error: ToolError | None           # kind, message, retryable
    duration_ms: int
```

### Конвейер Tool Runtime

```
resolve      найти инструмент и версию; проверить платформу
validate     аргументы по схеме; нормализация путей (абсолютные, без ..), разрешение ExecEnv
classify     ресурсы вызова: зоны путей, хосты, команды (AST), обратимость, масштаб
authorize    Policy Engine → allow | ask | deny; ask → подтверждение (с preview) или grant
journal      запись намерения в журнал эффектов и аудит
execute      таймаут, отмена, Job Object для процессов, блокировки ресурсов
normalize    выжимка, артефакты для большого вывода, редактирование секретов в выводе
postcheck    постусловие инструмента (если есть)
journal      итог: статус, эффекты, проверка; события tool.call.*
```

Ни один шаг не пропускается и не зависит от того, кто вызвал инструмент: агент, рецепт, GUI-драйвер
или плагин.

### Среды исполнения

```python
class ExecEnv(BaseModel):
    kind: Literal["host", "wsl", "ssh", "container"]
    name: str                         # "host", "wsl:Ubuntu-24.04", "ssh:prod-1", "container:gofra-backend"
    shell: Literal["pwsh", "bash", "sh"]
```

- `host` — PowerShell 7 (`pwsh -NoProfile -NonInteractive`).
- `wsl:<distro>` — bash в WSL; пути Windows ↔ WSL переводятся адаптером.
- `ssh:<host>` — команды на удалённом сервере; по умолчанию HIGH, политика задаётся на хост
  (staging строже или мягче, production — всегда подтверждение).
- `container:<id>` — `docker exec`; удобная песочница для dev-команд.

У проекта в реестре указана его среда; `shell.run` без явной среды берёт среду проекта.
Классификатор риска разбирает команду парсером соответствующего диалекта (AST PowerShell, парсер
bash), а не регулярными выражениями.

### Начальный набор инструментов (v1)

| Пространство | Инструменты | Базовый риск |
| --- | --- | --- |
| `app` | `open`, `close`, `list_installed` | SAFE / MEDIUM / SAFE |
| `window` | `list`, `focus`, `close` | SAFE / SAFE / MEDIUM |
| `proc` | `list`, `start`, `stop` | SAFE / MEDIUM / MEDIUM |
| `shell` | `run(command, env, cwd, timeout)` | по классификатору команды |
| `fs` | `read`, `list`, `search`, `write`, `patch`, `move`, `delete` (в Корзину) | SAFE ×3 / MEDIUM ×3 / HIGH |
| `files` | `search` по имени, дате, типу, папке, признаку «скачан» | SAFE |
| `audio` | `get_volume`, `set_volume`, `mute`, `list_devices`, `set_device` | SAFE |
| `media` | `play_pause`, `next`, `now_playing` | SAFE |
| `notify` | `show` | SAFE |
| `clipboard` | `read`, `write` | MEDIUM (буфер может содержать секреты) / SAFE |
| `http` | `probe` (localhost и allowlist), `get` | SAFE / MEDIUM |
| `browser` | `open(url)` | SAFE для localhost и известных доменов, иначе MEDIUM |
| `git` | `status`, `diff`, `log`, `commit`, `push` | SAFE ×3 / MEDIUM / HIGH |
| `docker` | `ps`, `logs`, `inspect`, `compose_up`, `restart`, `compose_down` | SAFE ×3 / MEDIUM ×2 / MEDIUM, с `-v` — HIGH |
| `logs` | `extract_errors(artifact)` — детерминированная выжимка ошибок и стектрейсов | SAFE |
| `artifact` | `read(ref, range, grep)` | SAFE |
| `memory` | `remember`, `recall`, `forget` | MEDIUM / SAFE / MEDIUM |
| `skill` | `read(name)` — полный текст руководства | SAFE |
| — | `ask_user(question, options)` | SAFE |

GUI-инструменты (`screen.*`, `ui.*`, `input.*`) появляются в Stage 6 ([08-computer-use.md](08-computer-use.md)).

### Правила проектирования инструментов

- **Узкие инструменты лучше универсальных.** `docker.logs` с выжимкой надёжнее, чем `shell.run("docker logs …")`:
  известная семантика, точный риск, структурированный результат. `shell.run` — запасной путь.
- **Внутри инструментов — argv, а не строки для shell.** Значения параметров никогда не
  подставляются в командную строку конкатенацией.
- **Большой вывод — в артефакт.** В `summary` — выжимка с лимитом, полный текст доступен через
  `artifact.read`.
- **Постусловие у каждого инструмента с эффектом**, где это возможно: после `audio.set_volume(30)`
  прочитать громкость, после `app.open` — дождаться окна.
- **Постусловие инструмента ≠ проверка задачи.** Инструмент проверяет, что его действие
  выполнено; Verifier проверяет, что достигнута цель.
- **Версия — часть контракта.** Ломающее изменение схемы — новая версия; контрактные тесты на
  каждый инструмент запускаются против `platform/fake` и против реальной Windows.
- **Описание для модели — в форме «когда использовать / когда не использовать»**, с малыми лимитами
  результата по умолчанию (число записей, длина текста) — так малая модель реже путает инструменты.

### Видимость инструментов для модели

Модель видит инструменты своего профиля (8–12 штук) с короткими описаниями. Если профиль широкий,
перед шагом выбирается подмножество по релевантности к текущему шагу плана. Инструменты с
`llm_visible=False` (например, низкоуровневые `input.*` вне профиля `gui`) доступны только
рецептам и внутренним компонентам.

### Журнал эффектов и отмена

```python
class Effect(BaseModel):
    kind: str                         # "file.write", "file.delete", "process.start", "setting.change", "network.send", …
    target: str
    before: BlobRef | None            # бэкап для отмены
    after: BlobRef | None
    reversible: bool
    undo: UndoSpec | None
```

Эффекты пишутся до и после исполнения ([03-agent-core.md](03-agent-core.md#9-ошибки-и-восстановление)).
Это даёт «верни как было», восстановление после сбоя и полный аудит изменений.

### MCP-инструменты

MCP-инструмент оборачивается в `ToolSpec` через **манифест политики** в конфиге Jarvis:

```toml
[[mcp.servers]]
id = "playwright"
transport = "stdio"
command = "npx"
args = ["@playwright/mcp@<закреплённая версия>"]
expose_to_llm = ["browser_navigate", "browser_snapshot", "browser_click"]
default_risk = "medium"
risk_overrides = { browser_click = "medium", browser_type = "high" }
```

Аннотации MCP (`readOnlyHint`, `destructiveHint`, `openWorldHint`) — подсказки, а не основание для
доверия: итоговый риск задаёт локальный манифест, а без манифеста инструмент считается MEDIUM с
подтверждением. В контекст модели попадают только перечисленные в `expose_to_llm`.

## 2. Skills: руководства и рецепты

### Два вида навыков

| | Руководство (guide) | Рецепт (recipe) |
| --- | --- | --- |
| Суть | Знание: как подходить к задаче | Процедура: какие действия выполнить |
| Исполнитель | LLM читает и применяет | Движок рецептов, детерминированно |
| Формат | `SKILL.md` | `recipe.yaml` (или Python для сложной логики) |
| Источник | AI-Dev-System (тысячи навыков), локальные | Локальные (написанные, выученные) |
| Права | Не даёт никаких прав | Объявляет нужные права; исполнение — по grant'у |
| Пример | `bugfix-investigator`, `container-deployment-reviewer` | `user.gofra.start`, `user.audio.meeting_mode` |

### Формат папки навыка

```
skills/user.gofra.start/
├── SKILL.md        # руководство: когда использовать, что делает, как проверить — для LLM и людей
├── recipe.yaml     # исполняемая грань (необязательна)
└── evals/          # сценарии проверки (необязательно)
```

`SKILL.md` — с frontmatter, совместимым с AI-Dev-System (`name`, `description`, при необходимости
`use_when`). Поэтому выученный рецепт одновременно становится руководством, которое можно
опубликовать в AI-Dev-System и которым смогут пользоваться Claude Code или Codex («чтобы запустить
GOFRA: `docker compose up -d` в WSL, затем `pnpm dev` в `frontend/`, проверить `/health`»).

### Рецепт (эскиз формата)

```yaml
id: user.gofra.start
version: 3
title: Запустить проект GOFRA
triggers:
  examples: ["запусти гофру", "запусти GOFRA", "подними GOFRA"]
params:
  project: {type: project, default: gofra}
preconditions:
  - check: docker.daemon_running
steps:
  - id: up
    tool: docker.compose_up
    args: {project_dir: "${params.project.path}", env: "${params.project.exec_env}", detach: true}
  - id: frontend
    tool: proc.start
    args:
      argv: ["pnpm", "dev"]
      cwd: "${params.project.path}/frontend"
      env: "${params.project.exec_env}"
      name: gofra-frontend
  - id: wait_backend
    wait_until:
      check: http.status
      args: {url: "http://localhost:8000/health", expect: 200}
      timeout_s: 90
  - id: open
    tool: browser.open
    args: {url: "http://localhost:3000"}
verification:
  - check: http.status
    args: {url: "http://localhost:8000/health", expect: 200}
on_failure: escalate              # передать цель, трассу и ошибку агентному циклу
permissions: [docker, proc.exec, net:localhost]
provenance:
  source: learned                 # builtin | user | learned | imported
  from_tasks: [t_0123, t_0141, t_0160]
  approved_at: 2026-10-01
  sha256: "<хэш содержимого>"
```

Типы шагов:

| Тип | Смысл |
| --- | --- |
| `tool` | Вызов инструмента через Tool Runtime |
| `recipe` | Вызов другого рецепта (композиция; циклы запрещены и проверяются при загрузке) |
| `check` | Проверка из библиотеки проверок; провал → `on_error` |
| `wait_until` | Повторять проверку до успеха или таймаута |
| `ask_user` | Вопрос пользователю (с вариантами) |
| `agent` | Ограниченный подагент: цель, разрешённые инструменты, бюджет — для шагов вроде «разобрать логи» |
| `notify` / `say` | Уведомление или фраза голосом |

Правила:

- **Шаблоны — только подстановка значений:** `${params.x}`, `${steps.<id>.data.<поле>}`, пара
  фильтров (`default`). Никаких выражений и кода — рецепт остаётся проверяемым и безопасным.
- **Значения передаются структурно** (argv, поля аргументов), а не склеиваются в строку shell —
  так параметр не может внедрить команду.
- **У шага есть `timeout_s`, `retry` (только для идемпотентных) и `on_error`:**
  `fail | continue | escalate | ask`.
- **Состояние сохраняется после каждого шага** — рецепт можно возобновить после сбоя.
- **Рецепт на Python** — для сложной логики: `async def run(params, ctx)` с доступом только к
  `ctx.tools`. Это доверенный локальный код (плагин), а не то, что можно скачать и запустить.

### Доверие и разрешения рецептов

- Риск рецепта вычисляется как максимум риска шагов с учётом аргументов.
- Первый запуск (и любой запуск после изменения содержимого) показывает рецепт целиком: шаги,
  среду, итоговый риск. Одобрение превращается в grant, привязанный к `sha256` и ограничениям
  параметров (например, только этот проект). Изменился рецепт — одобрение сбрасывается.
- Импортированные рецепты никогда не одобряются автоматически; «разрешить всегда» для них
  недоступно.

## 3. Skill Engine

```python
class SkillSource(Protocol):
    id: str
    async def find(self, query: SkillQuery) -> list[SkillCandidate]: ...
    async def load(self, ref: SkillRef) -> Skill: ...

class SkillCandidate(BaseModel):
    ref: SkillRef                     # источник + имя + версия или хэш
    kind: Literal["guide", "recipe"]
    title: str
    summary: str
    score: float
    reason: str                       # почему подходит (из источника)
    role: str | None                  # workflow / domain / verification / specialist — из recommend_skills
    trust: Literal["builtin", "user", "learned", "imported"]
```

Источники:

| Источник | Что даёт | Как ищет |
| --- | --- | --- |
| `local` | Рецепты и руководства пользователя в каталоге данных Jarvis | FTS5 + эмбеддинги по триггерам и описаниям |
| `ai_dev` | Руководства из AI-Dev-System | `recommend_skills` (≤ 3 с ролями) или `search_skills` на сервере |
| Другие MCP-серверы навыков | По мере появления | Через их API |

Два сценария использования:

1. **Роутер (L1)** ищет **рецепты** по триггерам: высокий порог, параметры должны разрешиться
   через Entity Resolver, иначе — подсказка для L2.
2. **Context Builder** подбирает **руководства** для профилей `dev` и `general`: берёт результат
   `recommend_skills`, отбрасывает навыки, чьи требования (`requires`) не покрываются
   инструментами Jarvis, укладывает карточки в бюджет токенов и помечает происхождение. Полный
   текст агент запрашивает инструментом `skill.read`, если карточки мало.

Кэш: результаты `recommend_skills` — по (хэш задачи, проект) с коротким TTL; тексты навыков — по
(имя, версия/хэш).

## 4. Skill graph

Что на самом деле нужно от «графа навыков» — и как это получить без отдельного движка:

| Потребность | Решение |
| --- | --- |
| Собирать большие процедуры из малых | Шаг `recipe` вызывает другой рецепт; дерево по построению, циклы проверяются при загрузке |
| Планировщик комбинирует готовые процедуры | Действие агента типа `recipe`: релевантные рецепты (top-5 по поиску) видны агенту как макродействия |
| Связи «связан с», «требует», «после» между руководствами | Метаданные и таксономия AI-Dev-System (группы, связи, проверки отношений) |
| Повторное использование частей | Маленькие рецепты-кирпичики (`docker.ensure_running`, `http.wait_healthy`) |

Не делаем: графовую БД, автоматический поиск плана по графу (это HTN-планирование — отдельная
исследовательская задача), вывод зависимостей из текста руководств. Вернуться к этому стоит,
когда рецептов будут сотни и появится измеримая проблема.

## 5. Обучение на повторяющихся workflow

Источник обучения — **собственные трассы Jarvis**, а не наблюдение за ручными действиями
пользователя: трассы уже есть, они структурированы, и в них записано, что сработало и прошло
проверку.

### Конвейер

```
завершённая задача (verified)
  → сигнатура эпизода: нормализованный запрос (эмбеддинг) +
    последовательность (инструмент, нормализованные аргументы)
  → кластеризация: близкие запросы и похожие последовательности
  → порог: ≥ 3 успешных повторения за 30 дней, все прошли проверку
  → обобщение: выравнивание последовательностей; различающиеся значения → параметры,
    совпадающие → константы; прошедшие проверки → verification
  → черновик рецепта + SKILL.md + триггеры из исходных формулировок
  → предложение пользователю (с превью шагов и рисков)
  → одобрение → рецепт с provenance: learned → роутер L1 начинает его находить
```

Нормализация аргументов: пути — относительно корня проекта, порты и URL — как есть, временные
значения (идентификаторы контейнеров, PID) — отбрасываются, числа — как параметры, если
различаются между повторениями.

Предложение выглядит так:

```
Я заметил повторяющийся сценарий (4 раза за 2 недели, все успешно):
  «запусти GOFRA» → docker compose up -d (WSL) → pnpm dev в frontend/ →
  ожидание /health → открыть http://localhost:3000
Сделать из этого команду «запусти GOFRA»?  [Создать]  [Показать шаги]  [Не предлагать]
```

После создания: учёт успешности рецепта; при провале — эскалация в агентный цикл; при повторных
провалах — предложение обновить рецепт по свежей успешной трассе.

### Другие способы получить рецепт

- **Объяснение словами:** «Джарвис, чтобы запустить GOFRA, нужно…» → LLM превращает описание в
  черновик рецепта из известных инструментов → показ → одобрение.
- **Из факта памяти:** «запомни, что GOFRA я запускаю через Docker Compose» → предложение рецепта
  ([02-overview.md](02-overview.md#76-запомни-что-этот-проект-я-обычно-запускаю-через-docker-compose)).
- **Режим записи (позже):** «запиши, что я сейчас делаю через тебя» → последовательность команд
  сессии становится черновиком.

### Связь с AI-Dev-System

В AI-Dev-System уже есть родственный механизм для dev-домена: инстинкты (`record_instinct`,
`propose_instincts`) и их обобщение в черновики SKILL.md (`evolve_instincts`). Разделение:

- Jarvis учит **исполняемые рецепты** из своих трасс (у других клиентов AI-Dev-System нет
  инструментов Jarvis, чтобы их исполнить);
- AI-Dev-System учит **поведенческие правила и руководства** для coding-агентов;
- `SKILL.md`-грань выученного рецепта можно (по желанию пользователя) опубликовать в AI-Dev-System,
  чтобы знание стало доступно другим агентам.
