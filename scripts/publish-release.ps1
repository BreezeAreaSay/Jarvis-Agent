#Requires -Version 5.1
<#
.SYNOPSIS
    Публикация версии Jarvis для обновления из приложения: релиз GitHub vX.Y.Z с установщиком.

.DESCRIPTION
    Берёт из desktop\release файлы версии из desktop\package.json — Jarvis-Setup-X.Y.Z.exe, его .blockmap
    и latest.yml — и создаёт релиз vX.Y.Z. Установленный Jarvis находит его при запуске и каждые 6 часов
    («Настройки → Система → Обновления → Проверить сейчас» — сразу), качает и ставит поверх.
    Нужен GitHub CLI: winget install GitHub.cli, затем gh auth login.
    Без него: https://github.com/stonebridgeway/Jarvis-Agent/releases/new — тег vX.Y.Z и те же три файла.

.EXAMPLE
    pwsh -File scripts\publish-release.ps1 -Notes "Исправлен вылет ядра; обновления из приложения"
#>
[CmdletBinding()]
param(
    [string]$Notes = '',
    [string]$NotesFile = '',
    [string]$Repo = 'stonebridgeway/Jarvis-Agent',
    [switch]$Replace,
    [switch]$Draft
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 при $ErrorActionPreference = 'Stop' превращает stderr программы (2>$null) в исключение
function Quiet([scriptblock]$Block) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block } finally { $ErrorActionPreference = $prev }
}

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root 'desktop\release'
$Version = (Get-Content (Join-Path $Root 'desktop\package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$Tag = "v$Version"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "Не найден GitHub CLI (gh): winget install GitHub.cli, затем gh auth login. Или создайте релиз $Tag вручную: https://github.com/$Repo/releases/new"
}

$Exe = Join-Path $Out "Jarvis-Setup-$Version.exe"
$Files = @($Exe, "$Exe.blockmap", (Join-Path $Out 'latest.yml'))
foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f)) { throw "Нет файла $f — сначала соберите установщик: pwsh -File scripts\build-win.ps1" }
}
# latest.yml — от этой сборки: в нём версия и sha512 установщика, по которым Jarvis проверяет скачанное
$Latest = Get-Content -LiteralPath $Files[2] -Raw -Encoding UTF8
if ($Latest -notmatch "(?m)^version:\s*'?$([regex]::Escape($Version))'?\s*$") {
    throw "latest.yml не от версии $Version — пересоберите установщик"
}

Quiet { gh release view $Tag --repo $Repo *> $null }
if ($LASTEXITCODE -eq 0) {
    if (-not $Replace) { throw "Релиз $Tag уже есть. Заменить его файлы: добавьте -Replace" }
    Write-Host "==> Заменяю файлы релиза $Tag"
    gh release upload $Tag @Files --repo $Repo --clobber
    if ($LASTEXITCODE -ne 0) { throw 'gh release upload не удался' }
} else {
    Write-Host "==> Создаю релиз $Tag в $Repo"
    $GhArgs = @('release', 'create', $Tag) + $Files + @('--repo', $Repo, '--title', "Jarvis $Version")
    if ($NotesFile) { $GhArgs += @('--notes-file', $NotesFile) }
    elseif ($Notes) { $GhArgs += @('--notes', $Notes) }
    else { $GhArgs += @('--notes', "Jarvis $Version") }
    # тег — на текущий коммит, если он уже на GitHub; иначе gh поставит его на основную ветку
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $Sha = Quiet { git -C $Root rev-parse HEAD 2>$null }
        if ($LASTEXITCODE -eq 0 -and $Sha -and (Quiet { git -C $Root branch -r --contains $Sha 2>$null })) {
            $GhArgs += @('--target', "$Sha".Trim())
        }
    }
    if ($Draft) { $GhArgs += '--draft' }
    gh @GhArgs
    if ($LASTEXITCODE -ne 0) { throw 'gh release create не удался' }
}

if ($Draft) {
    Write-Host "Черновик $Tag создан. Jarvis увидит версию, когда вы опубликуете черновик на GitHub."
} else {
    Write-Host "Готово: https://github.com/$Repo/releases/tag/$Tag — Jarvis предложит обновиться в течение 6 часов."
}
