#Requires -Version 5.1
<#
.SYNOPSIS
    Сборка установщика Jarvis на Windows: Jarvis-Setup-<версия>.exe в desktop\release.

.DESCRIPTION
    1. Python 3.12 для встраивания (пакет NuGet «python») → build\core-win\python
    2. Зависимости ядра (uv pip install --target) и пакет jarvis → build\core-win
    3. Интерфейс (npm run build) и установщик (electron-builder, NSIS)
    Нужны: uv (https://docs.astral.sh/uv/) и Node.js 22+.
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12.10',
    [switch]$SkipCore
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root 'build\core-win'
$Cache = Join-Path $Root 'build\cache'

function Need([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Не найден $Name. $Hint" }
}
Need 'uv' 'Установите: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
Need 'npm' 'Установите Node.js 22+ с https://nodejs.org'

if (-not $SkipCore) {
    New-Item -ItemType Directory -Force $Cache | Out-Null
    if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
    New-Item -ItemType Directory -Force $Out | Out-Null

    Write-Host "==> Python $PythonVersion (NuGet)"
    $nupkg = Join-Path $Cache "python.$PythonVersion.nupkg"
    if (-not (Test-Path $nupkg)) {
        Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/python/$PythonVersion/python.$PythonVersion.nupkg" -OutFile "$nupkg.part"
        Move-Item "$nupkg.part" $nupkg
    }
    $tmp = Join-Path $env:TEMP ("jarvis-py-" + [guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $nupkg -DestinationPath $tmp -Force
    Move-Item (Join-Path $tmp 'tools') (Join-Path $Out 'python')
    Remove-Item -Recurse -Force $tmp
    foreach ($junk in 'Lib\test', 'Lib\idlelib', 'Lib\tkinter', 'Lib\turtledemo', 'Lib\ensurepip', 'tcl', 'include', 'libs') {
        $p = Join-Path $Out "python\$junk"
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }

    Write-Host '==> Зависимости ядра'
    $req = Join-Path $env:TEMP 'jarvis-requirements.txt'
    Push-Location (Join-Path $Root 'core')
    uv export --frozen --no-dev --no-hashes --no-emit-project --no-header -o $req | Out-Null
    Pop-Location
    uv pip install --python (Join-Path $Out 'python\python.exe') --target (Join-Path $Out 'python\Lib\site-packages') -r $req
    $sys32 = Join-Path $Out 'python\Lib\site-packages\pywin32_system32'
    if (Test-Path $sys32) { Copy-Item "$sys32\*.dll" (Join-Path $Out 'python') }
    $scripts = Join-Path $Out 'python\Lib\site-packages\bin'
    if (Test-Path $scripts) { Remove-Item -Recurse -Force $scripts }

    Write-Host '==> Пакет jarvis'
    New-Item -ItemType Directory -Force (Join-Path $Out 'app') | Out-Null
    robocopy (Join-Path $Root 'core\jarvis') (Join-Path $Out 'app\jarvis') /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
    & (Join-Path $Out 'python\python.exe') -m compileall -q (Join-Path $Out 'app\jarvis') | Out-Null
}

Write-Host '==> Интерфейс и установщик'
Push-Location (Join-Path $Root 'desktop')
npm ci
npm run dist
Pop-Location
Get-ChildItem (Join-Path $Root 'desktop\release') -Filter 'Jarvis-Setup-*.exe' | ForEach-Object { Write-Host "Готово: $($_.FullName)" }
