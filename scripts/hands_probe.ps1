# Замер рук: точность выбора инструмента, задержка и скорость генерации на 16 фразах, два круга.
# Запуск (сервер рук уже запущен):
#   pwsh -NoProfile -ExecutionPolicy Bypass -File C:\Jarvis\scripts\hands_probe.ps1 -Label 4b
param(
  [string]$Label = "hands",
  [string]$Url = "http://127.0.0.1:8081",
  [string]$Template = (Join-Path $PSScriptRoot "hands_probe.json")
)
$ErrorActionPreference = "Stop"
$tpl = Get-Content $Template -Raw -Encoding utf8
$cases = @(
  @("открой телеграм", "open"),
  @("закрой это окно", "close"),
  @("сделай погромче", "vol"),
  @("почему небо голубое", "ask_gpt"),
  @("звук на 40", "vol"),
  @("открой загрузки", "open"),
  @("покажи дискорд", "focus"),
  @("поставь на паузу", "media"),
  @("найди презентацию про бюджет", "find"),
  @("разверни окно", "win"),
  @("напиши письмо начальнику что я заболел", "ask_gpt"),
  @("привет как дела", "reply"),
  @("закрой процесс стима", "kill"),
  @("ну сделай там это", "clarify"),
  @("переведи на английский я опоздаю", "ask_gpt"),
  @("предыдущая песня", "media")
)

function Invoke-Hands([string]$Text) {
  $body = [Text.Encoding]::UTF8.GetBytes($tpl.Replace("__CMD__", $Text))
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    $r = Invoke-RestMethod "$Url/v1/chat/completions" -Method Post `
      -ContentType "application/json; charset=utf-8" -Body $body
  } catch {
    $msg = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
    return [pscustomobject]@{ ms = $sw.ElapsedMilliseconds; err = $true; cache_n = 0; prompt_n = 0; gen_n = 0
                              tg = 0; calls = 0; finish = "error"; tool = "ERROR"; args = $msg }
  }
  $calls = @($r.choices[0].message.tool_calls | Where-Object { $_ })
  [pscustomobject]@{
    ms = $sw.ElapsedMilliseconds; err = $false; cache_n = $r.timings.cache_n; prompt_n = $r.timings.prompt_n
    gen_n = $r.timings.predicted_n; tg = [int][math]::Round([double]$r.timings.predicted_per_second)
    calls = $calls.Count; finish = $r.choices[0].finish_reason
    tool = if ($calls.Count -gt 0) { $calls[0].function.name } else { "" }
    args = if ($calls.Count -gt 0) { $calls[0].function.arguments } else { [string]$r.choices[0].message.content }
  }
}

$null = Invoke-Hands "привет"   # прогрев: системный текст и инструменты попадают в кэш

$rows = foreach ($round in 1..2) {
  foreach ($c in $cases) {
    $res = Invoke-Hands $c[0]
    [pscustomobject]@{
      round = $round; ms = $res.ms; err = $res.err; cache_n = $res.cache_n; prompt_n = $res.prompt_n
      gen_n = $res.gen_n; tg = $res.tg; finish = $res.finish
      ok = ((-not $res.err) -and $res.finish -eq "tool_calls" -and $res.calls -eq 1 -and $res.tool -eq $c[1])
      phrase = $c[0]; expected = $c[1]; tool = $res.tool; args = $res.args
    }
  }
}

$rows | Format-Table round, ms, cache_n, prompt_n, gen_n, tg, finish, ok, phrase, expected, tool, args -AutoSize |
  Out-String -Width 300

$rows | Where-Object err | ForEach-Object { "ошибка сервера: {0} → {1}" -f $_.phrase, ($_.args -replace "\s+", " ") }
$good = @($rows | Where-Object { -not $_.err })
$errors = @($rows | Where-Object { $_.err }).Count
$first = @($rows | Where-Object round -eq 1)
if ($good.Count -eq 0) { "[{0}] все запросы с ошибкой — сервер не отвечает или отвечает ошибкой (см. args)" -f $Label; return }
$ms = @($good.ms | Sort-Object)
function Get-Pct([double]$Q) { $ms[[math]::Min($ms.Count - 1, [int][math]::Floor($ms.Count * $Q))] }
$avg = { param($xs) [math]::Round(($xs | Measure-Object -Average).Average) }
"[{0}] точность {1}/{2} | ошибок {3} | p50 {4} мс | p95 {5} мс | max {6} мс | cache_n ~{7} | prompt_n ~{8} | gen_n ~{9} | tg ~{10} т/с" -f `
  $Label, @($first | Where-Object ok).Count, $first.Count, $errors, (Get-Pct 0.5), (Get-Pct 0.95), $ms[-1],
  (& $avg $good.cache_n), (& $avg $good.prompt_n), (& $avg $good.gen_n), (& $avg $good.tg)
