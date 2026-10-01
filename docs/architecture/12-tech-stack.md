# 12. Технологический стек

Принцип выбора: маленькое собственное ядро (агентный цикл, политика, Tool Runtime — это главный
актив проекта) и проверенные библиотеки «на листьях» (протоколы, драйверы, движки моделей).

## 1. Решения

| Область | Выбор | Альтернативы | Почему |
| --- | --- | --- | --- |
| Язык рантайма | Python 3.12+ (точная версия — по наличию колёс onnxruntime и pywin32 на момент старта) | Rust, Go, TypeScript | Экосистема AI и MCP, скорость разработки; тяжёлые вычисления всё равно в нативных рантаймах |
| Конкурентность | asyncio + anyio (task groups, cancel scopes) | trio, потоки | Структурированная отмена; MCP SDK построен на anyio |
| Типы и схемы | pydantic v2, pydantic-settings | dataclasses, attrs | JSON Schema для инструментов, LLM и API из одних моделей |
| Сборка и качество | uv (workspace, lock-файл), ruff, pyright, pytest, import-linter | poetry, mypy | Скорость; правила слоёв проверяются автоматически |
| Runtime API | JSON-RPC 2.0 поверх WebSocket: Starlette + uvicorn | gRPC, ZeroMQ, FastAPI | Просто, дружит с Tauri и браузерными технологиями |
| Хранилище | SQLite (WAL), FTS5, sqlite-vec, blobs на диске по sha256 | Postgres, Qdrant, Chroma, LanceDB | Ноль администрирования, один файл, достаточно для личного ассистента |
| Конфиг и секреты | TOML + pydantic-settings, platformdirs, keyring (Windows Credential Manager) | YAML, JSON | Читаемо и типизировано |
| Рантайм LLM | Любой OpenAI-совместимый: llama.cpp `llama-server` (Vulkan) — основной; LM Studio — удобная альтернатива; Ollama — если поддерживает RX 7600 в вашей версии | vLLM (Linux и NVIDIA) | AMD + Windows; контроль над ограниченным декодированием и KV-кэшем |
| Переключение моделей | Встроенные механизмы рантайма (JIT LM Studio, keep_alive Ollama) или llama-swap | Собственный менеджер процессов (позже) | В v1 не писать то, что уже есть |
| Клиент LLM | Тонкий клиент на httpx (или официальный SDK OpenAI с `base_url`) + профили моделей | LiteLLM, LangChain | Узкая задача; полный контроль над особенностями рантаймов и кассетами |
| Эмбеддинги и реранкер | ONNX Runtime на CPU, мультиязычные модели класса BGE-M3 / multilingual-e5 | PyTorch, sentence-transformers | Без CUDA; AI-Dev-System уже работает с BGE-M3 в ONNX |
| MCP | Официальный Python SDK `mcp` | Свой протокол | Стандарт; клиент для AI-Dev-System и внешних серверов |
| Русский язык | pymorphy3 (лемматизация словоформ: «гофру», «в телеграме»), rapidfuzz (нечёткое сопоставление), dateparser + своя грамматика частых выражений времени, свой разбор числительных | spaCy | Лёгкие, без GPU; русская морфология критична для грамматик L0 и Entity Resolver |
| Windows: GUI и система | UIA-библиотеки (pywinauto или uiautomation), pywin32 (Win32, Job Objects), comtypes, pycaw (Core Audio), PyWinRT (`winrt-*`: медиа SMTC, уведомления), mss или DXGI-захват | Только pyautogui | UIA-first; нативные API надёжнее эмуляции ввода |
| Процессы и файлы | psutil, watchfiles, Everything (es.exe / SDK, если установлен), Windows Search | Свой индексатор всего диска | Использовать индексы, которые уже есть в системе |
| Браузер | Playwright (Python) или MCP-сервер Playwright, отдельный профиль | Selenium | DOM и дерево доступности надёжнее пикселей |
| Голос | sounddevice (WASAPI), openWakeWord (ONNX), Silero VAD, STT: GigaAM / faster-whisper int8 / Vosk, TTS: Piper / Silero | Porcupine; STT на GPU | Всё на CPU, GPU отдан LLM; русский — основной язык |
| CLI | Typer + Rich, prompt_toolkit для REPL | Click | Быстро и удобно |
| Desktop UI (Stage 4) | Tauri v2 + React + TypeScript | Electron, PySide6, веб-UI из демона | Небольшой размер; WebView2 есть в Windows 11 |
| Поставка | PyInstaller (onedir) → установщик Inno Setup (per-user) → подписанный манифест в GitHub Releases | Nuitka, embeddable CPython, MSI (WiX) | Быстрый старт; выбор скрыт за сборочным скриптом ([10-platform.md](10-platform.md#6-дистрибуция-и-обновления)) |
| Подпись обновлений | Ed25519 (библиотека `cryptography` или PyNaCl) | Только HTTPS | Защита от подмены релиза |
| CI | GitHub Actions: Linux-раннер для ядра на фейках, Windows-раннер для адаптеров, сборки и установщика | — | Ядро тестируется без Windows; платформа — на Windows |
| Наблюдаемость | structlog (JSON), собственное хранилище трасс, опциональный экспорт OTLP | Langfuse, облачные сервисы | Локально и без инфраструктуры |
| Планировщик | Свой маленький на SQLite + croniter | APScheduler | Мало кода, сохранность задач под контролем |

## 2. Что сознательно не берём

| Не берём | Почему |
| --- | --- |
| LangChain, LangGraph, CrewAI, AutoGen как фундамент | Агентный цикл — главный актив; чужие абстракции, их изменения и протечки станут нашими проблемами. Как источники идей — пожалуйста |
| LiteLLM | Нужен один OpenAI-совместимый протокол плюс профили моделей; лишний слой с собственными ошибками |
| Redis, Kafka, NATS, ZeroMQ | Один процесс, один пользователь |
| Postgres и серверные векторные БД | SQLite + sqlite-vec покрывают объёмы личного ассистента |
| Electron | Тяжелее Tauri при том же веб-интерфейсе |
| PyTorch и CUDA как зависимости рантайма | AMD + Windows; работаем через llama.cpp (Vulkan) и ONNX Runtime |
| Vision-first управление компьютером | Медленно и ненадёжно на 8 ГБ VRAM |
| Служба Windows для рантайма | Сессия 0 не видит рабочий стол пользователя |
| Собственный формат плагинов до MCP | MCP уже даёт изоляцию и экосистему |

Какие идеи и куски берём из существующих open-source проектов — в [14-prior-art.md](14-prior-art.md).

## 3. Модели: как выбирать

Конкретные модели устаревают быстрее документа, поэтому фиксируется процедура:

1. Кандидаты по классу и размеру под профиль железа ([02-overview.md](02-overview.md#1-целевая-платформа-и-железо)).
2. Обязательные требования: хорошее качество на **русском языке**, устойчивый structured output,
   рабочий контекст ≥ 12k на 8 ГБ VRAM.
3. Прогон eval-наборов роли (роутер, executor, verifier; для VLM — grounding) → таблица результатов.
4. Решение фиксируется в конфиге профиля железа и в ADR с датой.
