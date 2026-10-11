# Windows-линия через Wine (запасная, без CI)

Когда GitHub Actions недоступен, `scripts/wine_build.sh` в Linux-контейнере делает то же, что CI на
`windows-latest` и `.github/workflows/build.yml`: гоняет тесты под **настоящим Windows-Python 3.12** (64 бит, из
NuGet) и собирает **настоящий установщик** `Jarvis-Setup-<версия>.exe` — PyInstaller и Inno Setup работают под Wine.

```
scripts/wine_build.sh setup        # один раз (~1 мин при скачанных пакетах): Wine, префикс, Python, колёса, Inno Setup
scripts/wine_build.sh test         # pytest -m "not live" под Windows-Python (~6 мин); аргументы → pytest
scripts/wine_build.sh build        # иконка → PyInstaller onedir (~5 мин) → jarvis-cli.exe selftest → zip
scripts/wine_build.sh installer    # ISCC (~6 мин) → тихая установка, selftest, переустановка, удаление
scripts/wine_build.sh all          # всё подряд; падения тестов не останавливают сборку, код выхода — от pytest
scripts/wine_build.sh py -c "..."  # Windows-Python с тем же окружением (отладка)
```

Скрипт работает из любого каталога (переходит в корень репозитория), идемпотентен: пакеты скачиваются один раз,
колёса переставляются только при изменении `uv.lock`. Одновременно — один запуск на префикс (в начале и в конце
он завершает процессы Wine префикса: `wineserver -k`).

## Что получается

| Файл | Что это |
|---|---|
| `build/win/dist/Jarvis/` | папка onedir: `Jarvis.exe`, `jarvis-cli.exe`, `_internal\` |
| `build/win/Jarvis-onedir-<версия>.zip` | zip этой папки |
| `build/win/Output/Jarvis-Setup-<версия>.exe` | установщик Inno Setup |
| `build/win/pytest.log`, `pyinstaller.log`, `selftest.log`, `selftest-installed.log` | журналы |

`build/` в `.gitignore`. Иконка — `build/jarvis.ico` (там её ждёт `packaging/jarvis.spec`).

## Как устроено

Всё окружение — вне репозитория, в `/opt/winbuild` (`WINBUILD`):

- `prefix/` — Wine-префикс (win64). Внутри `C:\winbuild\python` — Python 3.12.10 из NuGet (`python`),
  `C:\winbuild\innosetup` — Inno Setup 6.7.3 из NuGet (`Tools.InnoSetup`: `ISCC.exe`, `Languages\Russian.isl`),
  `C:\winbuild\git` — MinGit 2.56.0 (`Git-Windows-Minimal`);
- `dl/` — скачанные `.nupkg`; `work/` — requirements, исходник шима ICU.

Колёса Windows ставятся из Linux без Wine: `uv export --frozen --all-groups` (без ruff) →
`uv pip install --python-platform x86_64-pc-windows-msvc --target <site-packages>` — маркеры `sys_platform ==
'win32'` выбирают pywin32, pycaw, comtypes. Проект подключён как editable-установка `uv sync`: `.pth` на
`Z:\…\src` и `jarvis-<версия>.dist-info` (версия из `pyproject.toml` для `importlib.metadata`).

Окружение запуска:

- `LANG=ru_RU.UTF-8` (`WINE_LANG`): Windows видит русскую локаль — ANSI 1251, OEM 866, как на ПК владельца.
  `WINE_LANG=C.UTF-8` — английская (1252/437), как на `windows-latest`. Без UTF-8-локали Linux Wine не создаёт
  файлы с кириллицей в имени;
- `PYTHONUTF8` не задан (как у собранного exe и в `ci.yml`); stdout/stderr запускаемого Python переключает на UTF-8
  обёртка `C:\winbuild\runner\utf8run.py` — только у самого процесса, кодировка локали и дочерние процессы как в
  Windows. Вывод Windows-процессов идёт только в pipe (`| cat`): с обычным файлом в stdout Python под Wine падает
  при старте («Invalid handle»);
- пользователь Windows — `me` (`WINE_USER`), профиль `C:\users\me`. Кириллическое имя (`WINE_USER=Пользователь`)
  проверяет пути профиля с кириллицей, как у владельца;
- Xvfb на время запуска: `Setup.exe` Inno Setup создаёт окно даже с `/VERYSILENT` и без X-сервера падает
  (ошибка 1400). Иконка и selftest идут с `QT_QPA_PLATFORM=offscreen`, как в `build.yml` (Wine не пропускает
  переменные `QT_*` — скрипт передаёт `WINEQT_QPA_PLATFORM`).

Добавки в префикс — только для Wine, в бандл и установщик они не попадают (`build` проверяет ICU):

- **ICU.** Qt 6.12 (`Qt6Core.dll`) импортирует системную `icuuc.dll` Windows 10 1903+; в Wine её нет. В
  `system32` префикса кладётся `icuuc.dll`, пересылающая имена `ucnv_*` в `icuuc72.dll` из NuGet
  `Microsoft.ICU.ICU4C.Runtime.win-x64` (собирается mingw-w64). PyInstaller DLL из `%WINDIR%` в бандл не берёт;
- **git для Windows.** Тесты гигиены зовут `git` (`ls-files`, `init` во временном репозитории); Linux-git из
  Wine с захватом вывода не запустить. В PATH — MinGit 2.56.0 из NuGet `Git-Windows-Minimal`
  (`C:\winbuild\git\cmd`). Мутирующие git-команды из Wine в дереве репозитория не запускай: git для Windows
  не видит бит исполнения и запишет в индекс режим 100644.

## Ограничения: это не Windows

Не проверяются: настоящий рабочий стол и окна других программ, хоткей, трей, DWM (скругления, backdrop), звук
(pycaw/WASAPI), Everything, llama-server/GPU, вход в ChatGPT, Restart Manager, SmartScreen, Defender. Окно Qt
рисуется офскрин. Скорость под Wine другая: замеры времени — только на ПК владельца.

Известные отличия Wine 9.0 — падение такого теста не баг кода:

- junction и symlink: `mklink /J` возвращает 0, но ничего не создаёт; `os.symlink` «успешен», а ссылки нет;
- `IsProcessInJob` с правом `PROCESS_QUERY_LIMITED_INFORMATION` — «Доступ запрещён» (Windows его принимает);
- psutil `Process.exe()/name()` — `NtQuerySystemInformation` не реализован (WinError 87);
- тесты с порогом времени: файловые вызовы под Wine медленные (`os.stat` ≈1,5 мс), импорт `mcp.server.fastmcp`
  ≈6 с против ≈0,7 с в Linux;
- `mscms.WcsCreateIccProfile` не реализован — Qt с платформой `windows` пишет об этом в лог, работу не прерывает;
- установленная копия видит `sys._MEIPASS` как `Z:\opt\winbuild\prefix\dosdevices\c:\…` (так Wine раскрывает
  путь) — на Windows это `C:\…`.

Падение, которого нет в этом списке, сначала проверь по коду: под `sys.platform == "win32"` работают ветки,
которых Linux-тесты не видят (`os.path.isabs("C:\\…")`, `ntpath`, реестр, Win32).

## Требования

Ubuntu 24.04, root (apt: `wine64`, `wine32:i386` для 32-битного Inno Setup, `gcc-mingw-w64-x86-64`, `xvfb`,
`locales`), `uv`, сеть к PyPI и `api.nuget.org`. Место: ~2,5 ГБ в `/opt/winbuild`, ~1,3 ГБ в `build/win`.
