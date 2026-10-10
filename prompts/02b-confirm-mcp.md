Прочитай AGENTS.md. Сессия 2b: канал подтверждений и MCP-сервер pc для мозга.

1. confirm_client.py: confirm(summary, details, caller) по контракту AGENTS.md:
   в процессе Jarvis — через обработчик set_confirm_handler(); иначе — клиент named pipe по адресу из
   JARVIS_CONFIRM_PIPE (нет переменной — сразу False); нет сервера/таймаут 60 с → False.
   ConfirmServer(callback): адрес `\\.\pipe\jarvis-confirm-<имя пользователя>-<PID процесса>`, свойство address
   (S5 передаст его в env MCP-процесса). Проверенные особенности multiprocessing.connection на Windows:
   - ключ <data>\pipe.key: 32 байта os.urandom, создаётся один раз, если файла нет (tmp + os.replace), дальше его
     только читают и сервер, и клиенты — никогда не перезаписывать; рукопожатие HMAC взаимное, поэтому чужой
     «сервер» без ключа не сможет одобрить действие; с authkey=None проверки нет вовсе;
   - рукопожатие внутри Listener.accept() идёт без таймаута — поэтому Listener создавать без authkey и каждое
     принятое соединение сразу отдавать в свой daemon-поток, где вызвать
     `multiprocessing.connection.deliver_challenge(conn, key)`, затем `answer_challenge(conn, key)` (тот же порядок,
     что в Listener.accept), затем recv_bytes(maxlength); висящий клиент держит только свой поток;
   - только send_bytes/recv_bytes с JSON (send/recv распаковывают pickle);
   - listener.close() из другого потока НЕ будит accept(): остановка — флаг + одно подключение к самому себе через
     Client(address, family="AF_PIPE", authkey=key), затем close(); поток слушателя — daemon;
   - Client() тоже без таймаута — на стороне pc ждать ответа в отдельном потоке с ограничением 60 с;
   - DACL канала по умолчанию даёт чтение группе Everyone, удалённые клиенты не отклоняются — принятый риск для
     домашнего ПК (без ключа ни подключиться на запись, ни подделать ответ); запиши его в «Заметки».
2. mcp.py — FastMCP stdio, `python -m pc.mcp`, caller="brain" для всех вызовов. Инструменты (описания по-русски,
   коротко; точные схемы): list_windows, list_apps(query), find_files(query, kind), open(target, kind),
   focus(target), close(target), window(action, target), volume(set|delta|mute), media(action), processes(name),
   kill_process(name), clipboard_get, clipboard_set(text), read_text_file(path), move_to_trash(path),
   type_text(text, target), lock(), power(action). Аннотации: readOnlyHint у читающих, destructiveHint у
   kill/trash/power. Политика, подтверждения и privacy — внутри pc, не в MCP-слое; MCP-элицитацию не использовать.
   Ошибки — понятный текст, без traceback. stdout — только протокол; логи — в stderr и <data>\logs\pc-mcp.log
   (не открылся — только stderr). Импорт pc.mcp не тянет PySide6 и openai_codex; старт ≤1 с.
3. scripts/mcp_smoke.py: клиент mcp по stdio запускает сервер, печатает список инструментов и результат list_windows
   и find_files("отчёт"). Клиент mcp по stdio передаёт серверу только системные переменные плюс
   StdioServerParameters.env — поэтому env задаётся явно: JARVIS_DATA_DIR и JARVIS_CONFIG (если заданы в окружении),
   PYTHONUTF8="1", PYTHONIOENCODING="utf-8"; command = sys.executable, args = ["-m", "pc.mcp"], cwd = корень репозитория.
4. Тесты: ConfirmServer + клиент в одном процессе: да/нет/таймаут/неверный ключ/нет переменной/висящий клиент не
   блокирует следующего/второй сервер в том же процессе не ломает первый; каждый инструмент MCP отображается на
   строку таблицы policy; mcp.py под фейковыми действиями.

Готово, когда: проверки зелёные; в сессии `$env:JARVIS_DATA_DIR='C:\Jarvis\scratch\data'; uv run python
scripts\mcp_smoke.py` печатает 18 инструментов и не падает (пустой list_windows или «Everything не запущен» из
песочницы допустимы).
Проверка руками (для меня): `uv run python scripts\mcp_smoke.py` — 18 инструментов, мои окна, пути с кириллицей
без кракозябр.
