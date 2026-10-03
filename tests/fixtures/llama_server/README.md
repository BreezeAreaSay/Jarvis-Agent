# Записанные ответы llama.cpp server

Настоящие ответы `llama-server` (llama.cpp, коммит `b92761a`, сборка CPU), записанные `curl` в Session 4
для контрактных тестов адаптера `OpenAICompatibleBackend` (`tests/contract/test_model_backend.py`).

Модель — крошечная qwen2 со случайными весами и настоящим токенизатором Qwen2 (из
`models/ggml-vocab-qwen2.gguf` репозитория llama.cpp): смысла в ответах нет, но формат API, грамматика
по JSON Schema, `usage`, `timings` и ошибки — настоящие. `props_trimmed.json` — только поля, которые
читает адаптер.

| Файл | Запрос |
| --- | --- |
| `chat_json_schema.json` | `POST /v1/chat/completions` с `response_format` (JSON Schema, enum) |
| `chat_plain.json` | `POST /v1/chat/completions` без схемы |
| `error_context.json` | промпт длиннее окна контекста (HTTP 400) |
| `models.json` | `GET /v1/models` |
| `props_trimmed.json` | `GET /props` |

## Логи `llama-server` (бенчмарк, `tests/unit/evals/test_bench_server.py`)

| Файл | Что это |
| --- | --- |
| `server_log_cpu.txt` | настоящий лог загрузки с `-lv 4` (сборка CPU, крошечная модель), строки, которые разбирает бенчмарк |
| `server_log_full_vulkan.txt` | **синтетический**: те же строки в формате Vulkan-сборки — RX 7600, все слои на GPU |
| `server_log_moe_vulkan.txt` | **синтетический**: MoE с `--cpu-moe` — эксперты в RAM |
| `server_log_partial_vulkan.txt` | **синтетический**: на GPU 20 из 33 слоёв |

Синтетические логи нужны, потому что GPU в контейнере разработки нет; настоящий лог Vulkan появится с
первым прогоном бенчмарка на ПК (`benchmarks/results/*/server.log`).
