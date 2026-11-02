# Сколько процессов Chrome/Edge с профилем внутри C:\jarvis-check (браузеры проверки), с командной строкой.
$found = Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -in @('chrome.exe', 'msedge.exe')) -and ($_.CommandLine -like '*jarvis-check*') }
"Процессов браузера с профилем в C:\jarvis-check: $(@($found).Count)"
$found | Select-Object ProcessId, ParentProcessId, Name | Format-Table -AutoSize
