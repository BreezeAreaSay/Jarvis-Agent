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
