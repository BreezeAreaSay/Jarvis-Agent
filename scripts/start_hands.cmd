@echo off
rem Jarvis hands server (llama.cpp llama-server, Vulkan).
rem Usage: start_hands.cmd 4b  or  start_hands.cmd 8b   (default: 4b)
rem All console output (including Vulkan out-of-memory errors, which bypass --log-file) goes to logs\hands.log;
rem the file is overwritten on every start.
setlocal
set "MODEL=C:\models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
if /i "%~1"=="8b" set "MODEL=D:\Jarvis-model-cache\Qwen3-8B-Q4_K_M.gguf"
set "LOGDIR=C:\Jarvis\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
"C:\llama\llama-server.exe" -m "%MODEL%" ^
  --host 127.0.0.1 --port 8081 -a hands --cors-origins localhost ^
  -ngl 99 -fa on -c 8192 -ctk q8_0 -ctv q8_0 ^
  -np 1 --jinja --reasoning off ^
  --temp 0 --no-webui ^
  --log-timestamps --log-prefix --log-colors off ^
  > "%LOGDIR%\hands.log" 2>&1
