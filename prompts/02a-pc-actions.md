Прочитай AGENTS.md. Сессия 2a: действия src/pc (без MCP и без канала подтверждений — это 2b).
Все внешние процессы — через pc.subproc из S1. Подтверждения в этой сессии: confirm() пока вызывает обработчик,
заданный set_confirm_handler(), а без обработчика — отказ (False); канал добавит 2b.

1. windows.py: list_windows() (видимые top-level с заголовком: hwnd, title, pid, exe; без окон Jarvis,
   без cloaked и tool-окон); foreground(); find_window(target) по exe/заголовку/имени приложения
   (apps.resolve + rapidfuzz); focus(hwnd) — лестница, каждая ступень проверяется GetForegroundWindow():
   (1) ShowWindow(SW_RESTORE), если свёрнуто, затем SetForegroundWindow; (2) SendInput одного отпускания Alt
   (VK_MENU, KEYEVENTF_KEYUP) и повтор; (3) AttachThreadInput к потоку текущего переднего окна +
   SetForegroundWindow/BringWindowToTop, отсоединение в finally; (4) не вышло — FlashWindowEx и честный
   Result(ok=False, "Не удалось переключиться на …"). Окна с повышенными правами (администратор) не трогать:
   UIPI всё равно не даст — сразу понятное сообщение. close(hwnd) = PostMessage WM_CLOSE;
   minimize/maximize/restore/minimize_all. Окна и процессы самого Jarvis (свой PID и его дерево: codex.exe,
   pc.mcp, llama-server, запущенный Jarvis) — close/kill отказ: «Это сам Jarvis — выйти можно из трея».
2. audio.py: громкость get/set 0–100, delta, mute/unmute. pycaw ≥ 20251023: `AudioUtilities.GetSpeakers()`
   возвращает AudioDevice — старый приём `.Activate(IAudioEndpointVolume._iid_, …)` падает с AttributeError;
   используй `dev.EndpointVolume` (GetMute/SetMute, SetMasterVolumeLevelScalar). COM: вся работа со звуком —
   в одном выделенном потоке, который вызывает comtypes.CoInitialize() при старте и CoUninitialize() в конце;
   COM-объекты между потоками не передавать; после смены устройства вывода — заново GetSpeakers().
   `pycaw.magic` не импортировать. media.py: play_pause/next/prev через SendInput медиаклавиш.
3. procs.py: find_processes(name) (psutil, группировка по exe, число процессов; для brain — без командных строк);
   kill(name) — через policy и confirm; terminate → ждать 3 с → kill.
4. files.py: find(query, kind, limit=20) через es.exe (ES ≥ 1.1.0.37). Вызов — ровно так (проверено по исходникам ES):
   `[es, "-argv", "-cp", "65001", "-timeout", "3000", "-n", str(limit), "-sort", "date-modified-descending",
     "/a-d" | "/ad" (по kind; для any — без флага), "-dm", "-date-format", "1", "-csv", "-no-header",
     "-no-folder-append-path-separator", "-search", query]`, таймаут процесса 5 с;
   stdout декодировать как UTF-8 (без `-cp 65001` es.exe пишет в кодовой странице консоли — кириллица
   превращается в кракозябры), разбирать модулем csv. Пользовательский текст — только после `-search`
   (иначе «-отчёт» станет ключом). Коды выхода: 0 — ок; 8 — Everything не запущен (понятное сообщение);
   7 — один повтор; 4/6 — ошибка в аргументах (это баг — в лог, stdout не разбирать: там справка).
   Для brain: функции Everything content:, utf8content:, regex: в запросе — отказ; результаты — через privacy.
   known_folder(name) для «загрузки, документы, рабочий стол, изображения, видео, музыка» (SHGetKnownFolderPath);
   open_target(target, kind, caller) — единственная точка, где вызывается os.startfile (см. «Пути, цели…» в AGENTS.md:
   приложение — только AppsFolder\AppID из инвентаря; файл/папка — канонический путь после paths.check, исполняемые
   типы — confirm; URL — urllib.parse, только http/https; всё прочее — отказ); trash(path) (send2trash, policy);
   read_text(path, max_bytes=65536) (только brain, policy).
5. system.py: lock(), sleep/shutdown/restart (policy), clipboard_get/clipboard_set, type_text(text, target):
   target — hwnd из list_windows, название окна или "@cur" (окно на момент хоткея), не «активное окно».
   Подтверждение показывает заголовок и exe окна и текст целиком (переводы строк — видимым ⏎; длиннее 500 символов —
   отказ). После «да» pc фокусирует target лестницей focus() и перед каждой порцией SendInput проверяет
   GetForegroundWindow() == hwnd; иначе — стоп и Result(ok=False). Для brain отказ: консоли и терминалы (cmd,
   powershell, pwsh, WindowsTerminal, wsl, conhost), окно «Выполнить», окна Jarvis, окна с повышенными правами.
6. privacy.py: redact(...) — одна функция фильтра для всего, что уходит мозгу: заголовки окон ≤80 символов;
   пути внутри закрытых зон и private_paths — скрыты; заголовок, содержащий имя файла/папки из private_paths, — скрыт;
   тексты ошибок проходят тот же фильтр. Используется в files/windows/procs для caller="brain" и в S5 для контекста.
7. Тесты: unit с моками win32/psutil/es.exe/send2trash/COM — на каждое правило open_target (cmd, powershell,
   shell:, file:, search-ms:, .exe/.lnk/.url → confirm, http/https, зоны brain), type_text (цель, консоль, длина,
   смена переднего окна), privacy, защита своих процессов; тест: каждое действие pc отображается на строку таблицы
   policy. Live-тесты: Блокнот — открыть, найти именно новое окно (снимок окон notepad.exe до и после запуска;
   в Windows 11 PID из Popen не совпадает с PID окна), фокус, свернуть, закрыть только это окно; файл `Тест_ёЁ.txt`
   находится es.exe с точным путём; громкость меняется и возвращается.

Готово, когда: проверки зелёные.
Проверка руками (для меня): `uv run pytest -q -s -m live tests\test_pc_live.py` — пришлю вывод.
