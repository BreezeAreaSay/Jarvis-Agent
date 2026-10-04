# 05. AI-Dev-System и интеграция через MCP

> **Architecture V2:** coding-цикл внутри Jarvis заменяется внешними агентами-специалистами
> (ADR 0029); AI-Dev-System остаётся источником навыков через `SkillProvider` по MCP — позже.

Раздел опирается на фактическое устройство [AI-Dev-System](https://github.com/BreezeAreaSay/ai-dev-system)
(коммит `8b480aa`), а не на предположения.

## 1. Что такое AI-Dev-System сегодня

| Аспект | Факт |
| --- | --- |
| Платформа | MCP-сервер на Node.js 22, транспорт stdio без сетевого порта; есть режим демона через локальный сокет / named pipe (одна сессия на подключение, простой — до 30 минут) |
| Инструменты | 133 инструмента в 16 группах (62 read-only), собранные в 8 профилей; `AI_DEV_PROFILES` сужает список (`core` — 25 инструментов, всегда включён) |
| Важная оговорка | Профили сужают только `tools/list`; `tools/call` принимает любой инструмент по имени. Профиль — не граница безопасности |
| Навыки | 3 227 навыков (из них 3 074 — Membrane application-skills, 101 — ECC) + собственные; формат SKILL.md с frontmatter (schema v2: `use_when`, `requires` и др.) |
| Поиск | Гибридный: SQLite FTS + sparse + опциональная локальная BGE-M3 (ONNX в Node), реранкер, пресеты |
| Роутинг навыков | `recommend_skills`: детерминированно ≤ 3 навыка (workflow, domain, verification) + зарезервированные роли (capability, specialist); перевод русских формулировок в лексику каталога |
| Проекты | `project_identity`, `analyze_project`, `read_project`, `compile_project_context(project_path, task)` — ограниченный пакет контекста под задачу |
| Жизненный цикл задач | `begin_task` → `checkpoint_task` (снапшот рабочего дерева) → `verify_task` (quality gate, привязка к Git) → `complete_task` (отказ без доказательств); `rollback_task`, эпики, plan gate, worktrees |
| Память | ADR (`record_decision`), передачи между сессиями, инстинкты с уверенностью и затуханием, обобщение инстинктов в черновики SKILL.md |
| Хранение | Знания — в vault; состояние — в `~/.ai-dev/state`; факты проекта — в `.ai-dev/` внутри репозитория. SQLite и векторы — одноразовые индексы |

Вывод: AI-Dev-System — уже не просто «библиотека навыков», а **dev-контур** для coding-агентов:
знания, контекст репозитория и проверяемый жизненный цикл изменений. Jarvis должен этим
пользоваться, а не дублировать.

## 2. Роль в архитектуре Jarvis

AI-Dev-System закрывает для Jarvis четыре функции:

1. **Процедурная память (руководства):** поиск и роутинг навыков под задачу.
2. **Знание о репозиториях:** идентичность проекта, анализ стека и команд, пакет контекста под задачу.
3. **Жизненный цикл dev-изменений:** снапшоты, quality gate, завершение только по доказательствам, откат.
4. **История dev-работы:** задачи, решения (ADR), передачи между сессиями — для вопросов вроде
   «что мы в прошлый раз исправляли в AI-Dev-System?».

```
                          ┌─────────────────────────── AI-Dev-System (Node, MCP) ──┐
 Jarvis                   │                                                        │
 ┌───────────────────┐    │  recommend_skills / search_skills / read_skill(_card)  │
 │ Skill Engine      │───►│  ── каталог навыков, гибридный поиск, роутер навыков   │
 │  AiDevGuides      │    │                                                        │
 ├───────────────────┤    │  project_identity / analyze_project /                  │
 │ Projects          │───►│  compile_project_context ── .ai-dev/, карточки проектов│
 │  AiDevProjects    │    │                                                        │
 ├───────────────────┤    │  begin / checkpoint / verify / complete / rollback_task│
 │ Agent (dev)       │───►│  ── жизненный цикл, снапшоты, quality gate             │
 │  AiDevLifecycle   │    │                                                        │
 ├───────────────────┤    │  list_tasks / list_decisions / resume_session          │
 │ Memory / Recall   │───►│  ── история dev-работы                                 │
 │  AiDevHistory     │    │                                                        │
 └─────────┬─────────┘    └────────────────────────────────────────────────────────┘
           │ все вызовы — через AiDevClient (типизированная обёртка) и MCP bridge
```

## 3. Граница ответственности

| Данные или функция | Владелец | Как использует другая сторона |
| --- | --- | --- |
| Руководства (SKILL.md), каталог, поиск, роутинг навыков | AI-Dev-System | Jarvis вызывает `recommend_skills`, `search_skills`, `read_skill_card`, `read_skill` |
| Знания о репозитории: стек, команды, правила, ADR, пакет контекста | AI-Dev-System (`.ai-dev/` в репозитории + vault) | Jarvis вызывает `project_identity`, `analyze_project`, `compile_project_context`, `list_decisions` |
| Жизненный цикл изменений в репозитории | AI-Dev-System | Jarvis оборачивает им dev-задачи, которые меняют репозиторий |
| Инстинкты coding-агентов | AI-Dev-System | Jarvis по желанию передаёт поправки пользователя в dev-домене через `record_instinct` |
| Реестр проектов «для голоса»: алиасы, среда исполнения, рецепты запуска, URL | Jarvis | Связь через `project_id` / `repository_id` из `project_identity` |
| Исполняемые рецепты, grant'ы, политика, аудит | Jarvis | SKILL.md-грань рецепта можно опубликовать в AI-Dev-System |
| Личные данные ПК: приложения, файлы, предпочтения ОС, разговоры, голос | Jarvis | В AI-Dev-System не передаются |
| Эпизоды Jarvis (что делалось на ПК) | Jarvis | Для dev-задач хранится ссылка на запись задачи в AI-Dev-System |

Правило: **один владелец у каждого факта.** Если Jarvis кэширует что-то из AI-Dev-System (стек
проекта для быстрого ответа голосом), это помечается как кэш с источником и временем и никогда
не правится на стороне Jarvis.

## 4. Подключение

### Транспорт

- **v1 — stdio.** При старте `jarvisd` запускает `node <ai-dev>/ai-dev-mcp-server/src/server.mjs`
  и держит одну долгую сессию. `jarvisd` — сам долгоживущий процесс, поэтому холодный старт
  (включая прогрев BGE-M3) происходит один раз.
- **Профили сервера Jarvis не сужает.** `AI_DEV_PROFILES` экономит контекст модели, а модель Jarvis
  схемы инструментов AI-Dev-System не видит. Зато часть нужных Jarvis инструментов лежит вне
  профиля `core` (`read_skill_card` — в `advanced`, `rollback_task` — в `git`, `list_decisions` и
  `resume_session` — в `memory`), и полный `tools/list` нужен для проверки возможностей. Что можно
  вызывать, ограничивает allowlist на стороне Jarvis.
- **Позже — режим демона** через локальный сокет / named pipe: один тёплый экземпляр на Jarvis,
  Claude Code и других клиентов. В Python MCP SDK нет готового клиентского транспорта для этого
  сокета — понадобится маленький собственный транспорт (JSON построчно поверх named pipe).
- **Среда запуска сервера = среда проектов.** Если проекты лежат в файловой системе WSL,
  AI-Dev-System лучше запускать внутри WSL, а Jarvis переводит пути; иначе сканирование через
  `\\wsl$` медленное, а пути путаются (вопрос Q2 в [13-roadmap.md](13-roadmap.md#5-открытые-вопросы)).

### Жизненный цикл соединения

- `initialize` → версия сервера из `serverInfo`; `tools/list` → проверка, что нужные инструменты
  на месте (feature detection).
- Таймауты по видам вызовов: `recommend_skills` — секунды; `compile_project_context` — десятки
  секунд; `verify_task` — минуты (он запускает проверки проекта).
- Circuit breaker и перезапуск процесса с backoff; при недоступности — деградированный режим
  (Jarvis работает без руководств и контекста проектов и сообщает об этом в отчёте задачи).
- **Allowlist вызовов на стороне Jarvis.** Поскольку профили AI-Dev-System не ограничивают
  `tools/call`, MCP bridge Jarvis сам разрешает вызывать только перечисленные инструменты этого
  сервера.

```toml
[[mcp.servers]]
id = "ai-dev"
transport = "stdio"
command = "node"
args = ["C:/dev/ai-dev-system/ai-dev-mcp-server/src/server.mjs"]
version_pin = "8b480aa"                # проверяется при старте; расхождение — предупреждение
roles = ["skills", "projects", "dev_lifecycle", "dev_history"]
callable = ["recommend_skills", "search_skills", "read_skill", "read_skill_card",
            "project_identity", "analyze_project", "read_project", "compile_project_context",
            "begin_task", "checkpoint_task", "verify_task", "complete_task", "rollback_task",
            "list_tasks", "list_decisions", "resume_session"]
expose_to_llm = []                     # модель не видит инструменты AI-Dev-System напрямую
```

## 5. Адаптеры в Jarvis

```python
class AiDevClient:
    """Типизированная обёртка над MCP-сессией. Единственное место, знающее имена инструментов."""
    async def recommend_skills(self, task: str, project_path: str | None) -> list[SkillRec]: ...
    async def search_skills(self, query: str, limit: int = 10) -> list[SkillHit]: ...
    async def read_skill_card(self, name: str) -> str: ...
    async def read_skill(self, name: str) -> str: ...
    async def project_identity(self, path: str) -> ProjectIdentity: ...
    async def analyze_project(self, path: str) -> ProjectAnalysis: ...
    async def compile_project_context(self, path: str, task: str) -> ContextPack: ...
    async def begin_task(self, path: str, task: str) -> DevTask: ...
    async def checkpoint_task(self, task_id: str, summary: str) -> Checkpoint: ...
    async def verify_task(self, task_id: str) -> Verification: ...
    async def complete_task(self, task_id: str, summary: str) -> Completion: ...
    async def rollback_task(self, task_id: str, snapshot_id: str) -> Rollback: ...
    async def list_tasks(self, project_path: str) -> list[DevTaskSummary]: ...
    async def list_decisions(self, project_path: str) -> list[Decision]: ...
```

Над клиентом — доменные адаптеры, реализующие порты Jarvis:

| Адаптер | Порт Jarvis | Где используется |
| --- | --- | --- |
| `AiDevGuides` | `SkillSource` | Context Builder подбирает руководства для профилей `dev` и `general` |
| `AiDevProjects` | `ProjectKnowledgeSource` | Обогащение реестра проектов, пакет контекста для dev-задач |
| `AiDevLifecycle` | `DevLifecycle` | Агентный цикл в профиле `dev`, когда задача меняет репозиторий |
| `AiDevHistory` | `HistorySource` | Recall: вопросы о прошлой dev-работе |

Разбор результатов — толерантный: известные поля в pydantic-модели, неизвестные игнорируются;
отсутствие обязательного поля — ошибка контракта (видна в контрактных тестах, а не у пользователя).

Отображение `recommend_skills` в кандидатов Jarvis: `name` → ссылка на навык; `role` → роль
(workflow / domain / verification / specialist / capability); `reason` → объяснение; `card_path` →
есть карточка (читаем сначала её); `score` → оценка; источник навыка (`custom` или импорт) →
уровень доверия.

## 6. Где происходит «поиск → кандидаты → реранкинг → 1–3»

На стороне сервера. `recommend_skills` уже возвращает не больше трёх навыков с ролями, а гибридный
поиск с реранкером — внутри AI-Dev-System. Jarvis добавляет только то, чего сервер знать не может:

1. **Фильтр по исполнимости:** навык, чьи `requires` не покрываются инструментами Jarvis, не
   попадает в контекст.
2. **Бюджет токенов текущей модели:** сначала карточки; полный текст — по запросу агента
   (`skill.read`).
3. **Метка доверия:** импортированный текст помечается как недоверенный.
4. **Кэш** результатов на время задачи.

Собственный LLM-реранкинг на каждую задачу Jarvis не делает: это секунды латентности без
доказанной пользы. Вернуться к этому можно, если eval покажет, что выбор сервера ошибается.

Навыки Membrane (интеграции с облачными сервисами) для local-first Jarvis в основном нерелевантны;
`include_membrane` остаётся выключенным (это и так значение по умолчанию).

## 7. Почему не отдавать 133 инструмента модели

- **Объём:** по данным README AI-Dev-System, схемы всех инструментов занимают около 96 КБ, а
  `core` + `git` — около 15 КБ. При рабочем контексте 12–16k токенов даже 15 КБ — заметная доля.
- **Точность:** малая модель выбирает инструмент тем хуже, чем их больше.
- **Риск:** часть инструментов пишет в репозиторий (`bootstrap_project`, `prepare_project`,
  `install_project_rules`) или в vault; политика Jarvis не может оценить семантику 133 чужих
  инструментов без манифестов.

Поэтому вызовы AI-Dev-System делает **код Jarvis** (адаптеры) в нужные моменты цикла. Модель видит
не инструменты AI-Dev-System, а их результаты: руководства и контекст проекта в промпте, итог
проверки в наблюдениях.

## 8. Жизненный цикл dev-задачи

```
Jarvis (профиль dev, задача меняет репозиторий)
  begin_task(project_path, task)       → task_id, пакет контекста, ≤ 3 навыка, критерии, риск, baseline
    если plan_required: plan_task(…)    → план записан (крупные и рискованные задачи)
  Planner Jarvis объединяет критерии AI-Dev-System со своими runtime-проверками
  цикл Executor:
    правки — инструментами Jarvis (политика и аудит Jarvis)
    checkpoint_task(task_id, summary)  → снапшот рабочего дерева
  Verifier:
    детерминированные проверки Jarvis (контейнер, /health, логи)
    verify_task(task_id)               → quality gate, доказательства, привязка к Git
  complete_task(task_id, summary)      → отказ, если отчёт не подтверждён проверками
  при провале или по просьбе пользователя: rollback_task(task_id, snapshot_id)
```

- Задача Jarvis хранит `ai_dev_task_id`; история и аудит Jarvis ссылаются на запись AI-Dev-System.
- `checkpoint_task` и `complete_task` проверяют отчёт на «рационализации» («должно работать»,
  «pre-existing issue»). Это совпадает с принципом Verifier Jarvis: итоговые тексты строятся из
  доказательств, а не из уверенности модели.
- Вызовы жизненного цикла — побочные эффекты внутри репозитория (записи `.ai-dev/`, refs снапшотов),
  поэтому они идут через политику как MEDIUM и включаются по проекту настройкой «использовать
  жизненный цикл AI-Dev-System» (вопрос Q5).
- `bootstrap_project` и `prepare_project` создают файлы в репозитории (AGENTS.md, `.ai-dev/`) — только
  с явного согласия пользователя.

Для крупных задач («подготовь репозиторий к production») естественно использовать plan gate и эпики
AI-Dev-System (`plan_task`, `decompose_task`): дочерние задачи со своими критериями и порядком.

## 9. Безопасность

- Импортированные навыки (Membrane, ECC и другие источники) — **недоверенный текст**: в контекст
  попадают как данные с меткой происхождения, не добавляют инструментов и прав.
- Собственные навыки пользователя — доверие выше, но это всё равно руководство, а не разрешение.
- Пакет контекста содержит выдержки из исходников репозитория — это тоже недоверенный контент
  (комментарий в коде может быть инъекцией), и задача получает taint.
- Правила `.ai-dev/policy.json` репозитория Jarvis может подключать как **дополнительный источник
  политики** для команд в этом репозитории, но только в сторону ужесточения: добавить запрет или
  подтверждение можно, ослабить политику Jarvis — нельзя.

## 10. Контракт и версионирование

- **Закреплённая версия** AI-Dev-System в конфиге Jarvis; при старте сверяется с `serverInfo`.
- **Контрактные тесты** в CI Jarvis: поднять сервер закреплённой версии на фикстурном репозитории и
  проверить форму ответов тех ~15 инструментов, которыми пользуется Jarvis.
- **Feature detection** во время работы: если инструмента нет — соответствующая функция
  отключается, а не падает.
- Только MCP API — никаких чтений vault, SQLite или JSON-файлов AI-Dev-System в обход протокола.

## 11. Что можно добавить в AI-Dev-System ради Jarvis (по желанию)

Это предложения в бэклог AI-Dev-System, а не требования для старта Jarvis.

1. **Документированное стабильное подмножество API для внешних клиентов** (не coding-агентов) с
   версионированием.
2. **Структурированная секция «процедура» в навыках** (шаги с командами и проверками) — чтобы Jarvis
   надёжнее превращал руководства в черновики рецептов.
3. **Группа навыков для администрирования рабочего места:** Windows, PowerShell, WSL, Docker Desktop —
   тогда `recommend_skills` станет полезен и вне dev-задач.
4. **Импорт локальной папки навыков** (сейчас `import_skill_repo` работает с репозиторием по URL) —
   для публикации SKILL.md-граней рецептов Jarvis.
5. **Идентификация клиента** в учёте использования и в статистике исходов навыков.
