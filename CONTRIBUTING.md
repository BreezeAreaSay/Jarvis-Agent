# Как устроен Jarvis и как в нём работать

Спасибо, что хотите помочь. Ниже — как запустить проект из исходников, по каким правилам писать код, как
проверять изменения и выпускать версии. Присылая изменения, вы соглашаетесь, что они распространяются по
лицензии проекта ([PolyForm Noncommercial 1.0.0](LICENSE.md)).

## Устройство

```
desktop/   Electron + React 19 + TypeScript + Vite: главное окно, оверлей поверх окон, аватар, трей, хоткеи;
           запускает ядро
core/      Python 3.12 + FastAPI: голос, команды, ИИ, агент, фоновые задачи, MCP, плагины, виджеты, пульт
           (HTTP + WebSocket на 127.0.0.1, каждый запрос с токеном)
docs/      CONTRACTS.md — API, события и правила ядра; UI.md — правила интерфейса; BACKGROUND_TASKS.md — фоновые
           задачи; EVALS.md — проверочные задачи агента; releases/ — что нового в каждой версии
scripts/   запуск, сборка установщика, выпуск
```

Данные — `%LOCALAPPDATA%\Jarvis` (или папка из `JARVIS_DATA_DIR`): настройки, база, модели, журналы; ключи API
зашифрованы DPAPI.

## Запуск из исходников

Нужны [uv](https://docs.astral.sh/uv/) и Node.js 22+.

```powershell
pwsh -File scripts\dev.ps1        # Windows: ядро + интерфейс с горячей перезагрузкой
pwsh -File scripts\build-win.ps1  # установщик: desktop\release\Jarvis-Setup-<версия>.exe
```

Без Windows — ядро с платформой-заглушкой (ничего не делает с системой, отдаёт правдоподобные данные) и
интерфейс в браузере:

```bash
cd core && uv sync && uv run python -m jarvis --platform fake --token devtoken --port 8799
cd desktop && npm install && npm run web
# http://127.0.0.1:5173/?core=http://127.0.0.1:8799&token=devtoken
```

На Linux ядро для Windows-установщика собирается без Wine: `scripts/build-core-win.sh`, затем
`cd desktop && npm run dist` (для значка exe нужен Wine).

## Правила

- **Контракт ядра** — [docs/CONTRACTS.md](docs/CONTRACTS.md): маршруты, события, настройки, как писать сервисы.
  Меняете маршрут, событие или настройку — правьте CONTRACTS.md в том же коммите.
- **Интерфейс** — [docs/UI.md](docs/UI.md): готовые компоненты из `@/ui`, цвета только через переменные темы,
  тексты по-русски, коротко и на «вы».
- **Без новых таблиц базы**: своё состояние — `kv` или JSON-файлы в `paths.sub_dir("<область>")`.
- **Ошибка для человека** — `UserFacingError("…")` с понятным текстом.
- **Тесты проходят на Linux** без микрофона, моделей и сети (сеть в тестах — `httpx.MockTransport`).
- **Агент и ИИ**: текст страниц, писем и результатов инструментов — данные, а не инструкции; всё, что
  отправляет, платит, публикует или удаляет, — только с разрешения человека.

## Проверка перед коммитом

```bash
cd core
uv run ruff check jarvis tests evals
uv run ruff format --check jarvis tests evals
uv run pytest -q

cd desktop
npm run typecheck
npm run build
```

То же запускает CI (`.github/workflows/ci.yml`) на Linux и Windows. Меняли агента — прогоните проверочные
задачи: `cd core && uv run python -m evals` (подробности и живой режим с настоящей моделью —
[docs/EVALS.md](docs/EVALS.md)).

Коммиты — по-русски, в духе истории проекта: `feat(фоновые задачи): …`, `fix(агент): …`, `docs: …`.

## Выпуск версии

1. Поднять версию в `desktop/package.json` (и `package-lock.json`), `core/pyproject.toml` (и `core/uv.lock`)
   и `core/jarvis/__init__.py`; что нового — в `docs/releases/v<версия>.md`.
2. Поставить тег `v<версия>` на коммит в `main` и отправить его на GitHub (`git tag v3.1.0 && git push origin
   v3.1.0`): CI проверит ядро и интерфейс, соберёт установщик на Windows и сам создаст релиз с заметками из
   `docs/releases` — установленный Jarvis увидит обновление.
3. Без CI — на своём ПК: `pwsh -File scripts\build-win.ps1` (в `desktop\release` появятся
   `Jarvis-Setup-<версия>.exe`, `.exe.blockmap` и `latest.yml`), затем
   `pwsh -File scripts\publish-release.ps1 -NotesFile docs\releases\v<версия>.md` (нужен
   [GitHub CLI](https://cli.github.com): `winget install GitHub.cli`, `gh auth login`) — или вручную: новый релиз
   на GitHub с тегом `v<версия>` и этими тремя файлами. Без `latest.yml` приложение версию не увидит.

Что проверить на Windows перед выпуском и что осталось до публичного релиза —
[docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md).

### Подпись установщика

Подписанный установщик не пугает SmartScreen и реже ловит ложные срабатывания антивирусов. Нужен сертификат
подписи кода (Code Signing, OV или EV) от центра сертификации — их теперь выдают на токене или в облаке, не
файлом. Бесплатная подпись [SignPath Foundation](https://signpath.org) — только для проектов под открытой
лицензией, одобренной OSI; PolyForm Noncommercial к ним не относится, так что Jarvis подписывают своим
сертификатом.

- Сертификат на токене: `pwsh -File scripts\build-win.ps1 -CertificateSubject "Имя из сертификата"` — signtool
  найдёт его в хранилище Windows (PIN токена спросит сам); electron-builder подпишет `Jarvis.exe`, установщик
  и деинсталлятор.
- Файл .pfx (если есть): `-Certificate C:\keys\jarvis.pfx` (пароль спросит); в CI — секреты репозитория
  `WIN_CSC_LINK` (файл .pfx в base64) и `WIN_CSC_KEY_PASSWORD` (Settings → Secrets and variables → Actions),
  сборка по тегу подпишет сама.
- Без сертификата — сборка без подписи, как раньше.

Подписанную версию выпустили — дальше подписывайте все: обновление проверяет издателя по подписи уже
установленной версии.

## История

Версия 2.0 — полный перезапуск проекта. Прежняя реализация осталась в истории git (ветки `main` и `jarvis-v1`).
