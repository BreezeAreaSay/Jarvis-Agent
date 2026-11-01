# Проверка этапа 1 на Windows — промт для Codex

Этап 1 плана ([IMPROVEMENT_PLAN.md](../IMPROVEMENT_PLAN.md)): распознавание текста в окнах (1.1), одна система
координат (1.2), Chrome браузера агента закрывается вместе с ядром (1.4). На Linux это проверено тестами и
имитатором; ниже — то, что можно проверить только на настоящей Windows. Скрипты — в `docs/checks/stage1/`.

Скопируйте промт целиком в Codex на ПК владельца.

---

Ты проверяешь ветку `stage1-screen-agent` проекта Jarvis (голосовой ассистент для Windows,
https://github.com/BreezeAreaSay/Jarvis-Agent) на этом ПК. Это Windows 11, масштаб 100 %, один монитор, имя
пользователя — кириллицей.

## Правила

- Работай только в `C:\jarvis-check`: клон, данные, журналы, отчёт. Вне неё ничего не создавай и не меняй.
- Код проекта не меняй, ничего не коммить и не отправляй. Свои вспомогательные файлы клади в
  `C:\jarvis-check\work`.
- Ничего не делай от администратора. Ни в какие аккаунты не входи и пароли не вводи.
- **Перед каждым шагом с мышью, окнами или параметрами Windows спрашивай владельца** и жди ответа «да». Пока идёт
  такой шаг, проси владельца не трогать мышь и клавиатуру.
- Личное (чаты, имена, сообщения) в отчёт не переписывай: только числа, время и да/нет. Строки текста выводи
  (`--show`) только с согласия владельца.
- Итог — отчёт `C:\jarvis-check\report-stage1.md` с фактами: команды, вывод (JSON скриптов целиком, кроме
  личного), что прошло, что нет и с каким текстом ошибки. Не исправляй найденное — опиши.

## 0. Подготовка

```powershell
New-Item -ItemType Directory -Force C:\jarvis-check, C:\jarvis-check\work, C:\jarvis-check\data | Out-Null
cd C:\jarvis-check
git clone --branch stage1-screen-agent https://github.com/BreezeAreaSay/Jarvis-Agent.git
cd C:\jarvis-check\Jarvis-Agent\core
uv sync
$env:PYTHONUTF8 = "1"
$env:JARVIS_DATA_DIR = "C:\jarvis-check\data"
```

Если `git clone` просит войти в GitHub — остановись и спроси владельца (сам не входи). Запиши версии:
`uv run python --version`, `git log -1 --oneline`, сборку Windows (`winver` не нужен:
`[Environment]::OSVersion.Version`).

## 1. Тесты ядра (без окон и мыши)

```powershell
uv run --with pytest-timeout pytest -q --timeout 120 tests/ai/test_agent_ocr.py tests/ai/test_agent_coords.py tests/platform/test_win_jobs.py tests/background/test_agent_browser.py
uv run --with pytest-timeout pytest -q --timeout 300
uv run python -m jarvis --selftest
```

Важно: `tests/platform/test_win_jobs.py::test_real_process_runs_inside_the_core_job` идёт только на Windows —
настоящий процесс в Job Object. В самопроверке должны импортироваться `winrt.windows.media.ocr`,
`winrt.windows.graphics.imaging`, `winrt.windows.globalization`. Падения — в отчёт с текстом.

## 2. Распознавание текста на картинке (без окон и мыши)

```powershell
uv run python ..\docs\checks\stage1\ocr_text.py
```

Запиши язык распознавания, время на картинку, сколько строк совпало для 12 и 9 пт и чем отличается прочитанное
(например, «O» и «0»).

## 3. Распознавание в настоящем окне (спроси владельца: окна выйдут вперёд)

Попроси владельца открыть Telegram Desktop (если его нет — любую программу на Qt: OBS, VLC, qBittorrent; или
другую, где UI Automation видит мало) и Блокнот.

```powershell
uv run python ..\docs\checks\stage1\ocr_window.py --exe Telegram.exe --focus --expect Поиск
uv run python ..\docs\checks\stage1\ocr_window.py --exe notepad.exe --focus
```

Для Telegram ожидается: `агент_распознаёт: true`, `ocr_ошибка` пустая, `ocr_секунд` — сотые доли секунды, строк —
десятки, слово «Поиск» (или «Search») в `найдено_распознано` — true (в `найдено_в_снимке` оно может быть false:
если поле поиска видит UI Automation, его подсказка в снимок строкой не идёт). Для Блокнота —
`агент_распознаёт: false` (UI Automation видит его хорошо). Запиши JSON обоих запусков без личного; если
`ocr_ошибка` не пустая — и строку WARNING из вывода.

## 4. Щелчок агента по координатам картинки и по распознанной строке (спроси владельца: мышь двигается)

Агент Jarvis с моделью по сценарию (без сети и ключей) щёлкает элемент так, как модель прочла его в тексте
снимка, а скрипт проверяет, попал ли щелчок в элемент. После щелчка скрипт нажимает Esc.

```powershell
# 1.2: click_point по координатам картинки (меню «Файл» Блокнота; английская Windows — --name File)
uv run python ..\docs\checks\stage1\agent_click.py --exe notepad.exe --name Файл --vision
# 1.1: ui_click по распознанной строке t… (поле поиска Telegram: щелчок только ставит туда курсор)
uv run python ..\docs\checks\stage1\agent_click.py --exe Telegram.exe --name Поиск --only-text
```

Ожидается: `попал: true`; для Блокнота строка снимка — `e… MenuItem «Файл» […]`, в шапке — «Координаты [x,y,ш×в]
— точки картинки экрана W×H»; для Telegram — строка `t… Text «Поиск» […]`.

Если строки `t… «Поиск»` нет (поле поиска видно UI Automation — тогда его подсказка не дублируется строкой
`t…`): с согласия владельца посмотри строки (`ocr_window.py --exe Telegram.exe --show`), выберите вместе
безобидную надпись — папку чатов («Все чаты») или «Избранное» — и повтори с `--name <надпись> --only-text`.
Запиши `e_строк`, `t_строк` и итог.

### 4а. Масштаб 125 % и 150 % (спроси владельца)

Попроси владельца поставить масштаб 125 % (Параметры → Система → Дисплей → Масштаб), подожди, пока экран
перестроится, и повтори шаг 4 (оба запуска) и `ocr_window.py` для Telegram. Затем то же для 150 %. **После —
попроси вернуть 100 %** и убедись, что вернули. В отчёте — таблица: масштаб, `dpi`, `попал`, размер картинки
из «шапки координат», `ocr_секунд`.

### 4б. Второй монитор

Если второго монитора нет — так и запиши. Если есть: попроси перенести Блокнот на второй монитор и повтори
первый запуск шага 4.

## 5. Chrome закрывается вместе с ядром (окон нет: браузер без окна)

Каждый «скрипт-ядро» печатает `pid_ядра` и ждёт. Запускай его в отдельном окне PowerShell (или
`Start-Process`), не закрывай вручную — убивай.

```powershell
# 5.1 механизм: браузер в Job Object процесса Python
Start-Process -NoNewWindow uv -ArgumentList "run","python","..\docs\checks\stage1\chrome_job.py" -RedirectStandardOutput C:\jarvis-check\work\chrome_job.json
Start-Sleep 8; Get-Content C:\jarvis-check\work\chrome_job.json
powershell -File ..\docs\checks\stage1\browsers_left.ps1        # браузер есть
Stop-Process -Force -Id <pid_ядра из JSON>
Start-Sleep 10
powershell -File ..\docs\checks\stage1\browsers_left.ps1        # ожидается 0

# 5.2 как в жизни: браузер агента фоновых задач
Start-Process -NoNewWindow uv -ArgumentList "run","python","..\docs\checks\stage1\agent_browser_job.py" -RedirectStandardOutput C:\jarvis-check\work\agent_browser_job.json
Start-Sleep 15; Get-Content C:\jarvis-check\work\agent_browser_job.json
powershell -File ..\docs\checks\stage1\browsers_left.ps1        # браузер есть, «подключён»: true
Stop-Process -Force -Id <pid_ядра из JSON>
Start-Sleep 10
powershell -File ..\docs\checks\stage1\browsers_left.ps1        # ожидается 0
```

Внимание: `uv run` запускает Python дочерним процессом — `pid_ядра` в JSON и есть процесс Python, убивай его, а не
`uv`. Если после 10 с процессы остались — запиши их число, `ParentProcessId` и командные строки (без личного) и
убери их сам: `Stop-Process -Force -Id …` только для процессов с `jarvis-check` в командной строке.

## 6. Уборка

Закрой то, что открывал сам; верни масштаб 100 %, если меняли. Папку `C:\jarvis-check` не удаляй — в ней отчёт.

## Отчёт `C:\jarvis-check\report-stage1.md`

1. Версии (Windows, Python, коммит).
2. Тесты: числа «passed/failed/skipped», тексты падений.
3. Самопроверка: прошла ли, модули OCR.
4. OCR на картинке: язык, время, совпадения 12/9 пт, ошибки распознавания.
5. OCR в окнах: JSON Telegram (или другой программы) и Блокнота.
6. Щелчки агента: таблица «масштаб × проверка × попал × dpi × картинка».
7. Job Object: сколько процессов браузера до и через 10 с после убийства «ядра» — для 5.1 и 5.2.
8. Что не удалось проверить и почему.
