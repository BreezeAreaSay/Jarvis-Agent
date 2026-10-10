# Jarvis

Персональный помощник для Windows 11. По хоткею появляется строка ввода, команда выполняется сразу.
Работает на трёх уровнях:

| Уровень | Что делает | Бюджет |
|---|---|---|
| 0. Грамматика | ~30 частых команд разбираются правилами, без модели («громкость 30», «открой телегу», «сверни всё») | ≤0,3 с |
| 1. Руки | локальная модель (Qwen3-4B на llama-server) выбирает один инструмент ПК | p95 ≤1,2 с |
| 2. Мозг | GPT через Codex app-server: вопросы, тексты, многошаговые задачи; ответ потоком | p95 ≤3 с до первых слов |

Префиксы: `локально:` — запрос без облака, `gpt:` — сразу мозг, `думай:` — мозг на основной модели.
Мозг действует на ПК только через инструменты pc (MCP), под политикой риска: опасное — только с подтверждением
в окне Jarvis, произвольный shell и удаление мимо корзины запрещены. Подробности — в [AGENTS.md](AGENTS.md).

## Установка

1. Скачай `Jarvis-Setup-<версия>.exe` и запусти. Права администратора не нужны: программа ставится
   в `%LOCALAPPDATA%\Programs\Jarvis`. Флажок «Запускать при входе в Windows» — автозапуск.
2. Каталог данных (журнал, кэш приложений, вход GPT) — `%LOCALAPPDATA%\Jarvis` или `JARVIS_DATA_DIR`.
   Если в пути профиля есть кириллица, задай `JARVIS_DATA_DIR=C:\JarvisData` (Codex с таким путём не работает).
   Удаление программы каталог данных не трогает.

## Первый запуск

- Значок Jarvis появится в трее. Хоткей — **Ctrl+Alt+Space** (занят — Ctrl+Alt+Shift+Space, затем Ctrl+Alt+J;
  окно открывается и из меню трея).
- `jarvis-cli doctor` — проверка всего по пунктам с подсказками
  (`& "$env:LOCALAPPDATA\Programs\Jarvis\jarvis-cli.exe" doctor`).
- Вход мозга (один раз), если doctor пишет «Not logged in» — команда в docs/manual-checks.md.
- Руки: llama.cpp (`C:\llama\llama-server.exe`) и модель Qwen3-4B; сервер поднимается сам при первой команде
  (`scripts\start_hands.cmd 4b`).
- Everything (поиск файлов) и `es.exe` (путь — `[pc] es_path` в конфиге).

## Окно

Enter — отправить; Esc — отменить запрос, второй Esc — скрыть; ↑/↓ — история; цифра+Enter — открыть найденное.
Подтверждение опасного действия: Enter и Esc — «нет», **Ctrl+Enter** — «да» (первые 700 мс нажатия
не принимаются).

## Трей

Локальный режим (без облака), новый разговор, перезапустить/выгрузить руки (освободить VRAM для игр), журнал,
автозапуск, выход.

## Конфиг

`C:\Jarvis\jarvis.toml` (или `JARVIS_CONFIG`; у установленного exe без папки C:\Jarvis — `<data>\jarvis.toml`).
Пример со всеми ключами — [jarvis.example.toml](jarvis.example.toml).

## Командная строка

```
jarvis-cli doctor [--brain]        проверка компонентов
jarvis-cli ask "громкость 30"      выполнить запрос (--dry — без действия)
jarvis-cli route "почему …"        какой уровень ответит
jarvis-cli apps [--refresh] [имя]  инвентарь приложений
jarvis-cli bench [--live]          прогон корпуса фраз
jarvis-cli stats [--days 7]        тайминги по журналу
jarvis-cli autostart on|off
jarvis-cli selftest                проверка собранного бандла
```
Из исходников — то же через `uv run jarvis …`.

## Сборка из исходников

```
uv sync                           # Python 3.12, зависимости из uv.lock
uv run ruff check . ; uv run ruff format --check . ; uv run pytest -q -m "not live"
pwsh -File scripts\build.ps1      # PyInstaller (onedir) + selftest + Inno Setup → Jarvis-Setup-<версия>.exe
```
На Linux тот же установщик собирается под Wine: `scripts/wine_build.sh all` (см. docs/wine-build.md).

Документы: [AGENTS.md](AGENTS.md) — спецификация и состояние; [docs/architecture.md](docs/architecture.md) —
контракты модулей; [docs/manual-checks.md](docs/manual-checks.md) — что проверить руками; [docs/notes.md](docs/notes.md)
— заметки.
