Прочитай AGENTS.md. Сессия 7: измеримость и доводка скорости.

1. journal.py: одна JSON-строка на запрос в <data>\journal\YYYY-MM-DD.jsonl: ts, text (если store_text), mode,
   level, reason, tool, args, ok, error, тайминги (hotkey_to_window, route, grammar; hands: cache_n, prompt_n,
   prompt_ms, predicted_ms, predicted_per_second, total_ms; exec; brain: first_token, total, new_thread, model, usage;
   total). Запись — в фоновом потоке, не на горячем пути. Тайминги, которые S3–S6 писали в лог, перенеси сюда.
2. `jarvis stats [--days 7]`: по уровням — count, p50/p95 total, доля ошибок, доля «hands→ask_gpt»;
   для рук — средний prompt_n (вырос — кэш префикса сломался), средние т/с (упали — VRAM переполнена) и p95
   total_ms; для мозга — p95 first_token отдельно для нового треда и повторных; нарушения бюджетов AGENTS.md выделить.
3. `jarvis bench [--live] [--level grammar|hands|router|all] [-n N]`: прогон bench/phrases.ru.jsonl (с ctx из корпуса
   и инвентарём из tests/fixtures/apps.json) — точность уровня, инструмента и ключевых аргументов; p50/p95 по уровням;
   действия НЕ выполняются (dry); отчёт в консоль и bench/results/<дата>.json; список ошибок
   «фраза → ожидалось → получено».
4. `jarvis doctor [--brain]`: ✓/✗ и подсказка, что сделать, по пунктам:
   - llama-server: /health; /props → model_path и default_generation_settings.n_ctx (верхнего n_ctx там нет);
     владелец порта 8081 — C:\llama\llama-server.exe; тёплый запрос: cache_n ≥ префикс + 3 (префикс — через
     /v1/chat/completions/input_tokens), predicted_per_second ≥ hands.min_tokens_per_s; Hands.status из S3;
     процессы Ollama (`Get-Process ollama*`-эквивалент через psutil; ollama CLI не вызывать — он сам запускает Ollama)
     — если запущена, предупредить про VRAM;
   - Everything (логика — как в scripts/es_check.py): es.exe ≥ 1.1.0.37, Everything запущен (код выхода 8 — нет); `es -get-result-count -path C:\Windows`
     > 0 → ✓ «весь C: в индексе»; = 0, но `-get-result-count -path <профиль пользователя>` > 0 → ✓ с пометкой
     «режим папок: поиск только в проиндексированных папках»; оба 0 → ✗ (служба Everything или индексы папок);
     поиск файла с кириллицей в имени возвращает точный путь;
   - кэш приложений (сколько, возраст), автозапуск, режим, хоткей зарегистрирован;
   - мозг (только если режим не local — в local codex не запускать вообще, `--brain` → отказ с пояснением):
     `codex login status` с CODEX_HOME мозга — бинарь из SDK; результат он пишет в stderr: код 0 и «Logged in using
     ChatGPT» → ✓; код 1 — показать stderr: «Not logged in» → подсказать вход (команда из S5), «Error loading
     configuration» → чинить config.toml в CODEX_HOME мозга; с --brain — один тестовый ход с t_first_token
     (без флага квоту не тратим).
5. Доводка: я пришлю вывод `jarvis bench --live` с моего ПК в эту сессию. Меняй только SYSTEM, примеры
   и описания инструментов рук, пороги грамматики, эвристики роутера; каждое изменение — с замером до/после,
   отдельным коммитом `fix(…)`.

Готово, когда: проверки зелёные; stats и bench проверены на синтетическом журнале и фейковом сервере.
Проверка руками (для меня): 1) `uv run jarvis doctor`; 2) запущенный сервер рук → `uv run jarvis bench --live` —
пришлю вывод; 3) через день использования — `uv run jarvis stats`.
