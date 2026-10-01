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
| `jarvis eval [пути] [-s ID] [--report файл.json]` | прогнать сценарии из `evals/scenarios` |

Данные Jarvis лежат в `JARVIS_HOME` (по умолчанию `AppData\Local\Jarvis` в профиле пользователя
на Windows, `~/.local/share/jarvis` на Linux); конфиг — `JARVIS_HOME/config/config.toml` или путь из `JARVIS_CONFIG`.
Других переменных окружения Jarvis не читает. Пример конфига:

```toml
schema_version = 1

[budgets.agent]
max_steps = 15
```

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
`task.transition`). Стадии получают `BudgetMeter` и списывают расход до действия. В M1 стадии —
scripted (`jarvis.evals.scripted`): так проверяется механика ядра без модели и инструментов.
