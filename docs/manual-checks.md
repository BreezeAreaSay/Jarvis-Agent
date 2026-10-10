# Проверка руками и «Мне на потом»

Всё, что сессия не могла проверить без настоящего Windows, рабочего стола, GPU, Everything, сети и входа в GPT.
Запускает только владелец на своём ПК. В репозиторий из вывода переносить только числа (см. «Публичный
репозиторий» в AGENTS.md).

Обозначения: `jarvis-cli` — установленный exe (`& "$env:LOCALAPPDATA\Programs\Jarvis\jarvis-cli.exe" …`);
`uv run jarvis …` — то же из исходников в C:\Jarvis. Каталог данных — C:\JarvisData (`JARVIS_DATA_DIR`).

## Порядок для владельца

<!-- ORDER -->

## Разделы по этапам

### S2b — канал подтверждений и MCP-сервер pc
1. `$env:JARVIS_DATA_DIR='C:\Jarvis\scratch\data'; uv run python scripts\mcp_smoke.py`
   → «Инструментов: 18», твои окна, пути из find_files("отчёт") с кириллицей без кракозябр.
   Прислать: число инструментов, «готов за X с», время вызовов (без заголовков окон и путей).
2. Канал между двумя процессами (оба окна PowerShell с `$env:JARVIS_DATA_DIR='C:\Jarvis\scratch\data'`):
   - окно 1: `uv run python -c "import time; from pc import confirm_client as c; s=c.ConfirmServer(lambda a,b,d: input(f'{a} / {b} / {d} y? ')=='y'); s.start(); print(s.address); time.sleep(300)"`
   - окно 2: `$env:JARVIS_CONFIRM_PIPE='<адрес из окна 1>'; uv run python -c "from pc import confirm_client as c; print(c.confirm('Тест?','детали','brain'))"`
   → в окне 1 вопрос с «brain»; «y» → True, другое → False; без ответа 60 с → False. Прислать: результаты и время.
