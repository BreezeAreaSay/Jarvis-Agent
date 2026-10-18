#Requires -Version 5.1
<#
.SYNOPSIS
    Запуск Jarvis из исходников на Windows (режим разработки): ядро на Python + интерфейс Electron с горячей
    перезагрузкой. Первый запуск ставит зависимости (uv sync, npm install).
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Ставлю uv…'
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'Нужен Node.js 22+: https://nodejs.org' }

Push-Location (Join-Path $Root 'core')
uv sync
Pop-Location

Push-Location (Join-Path $Root 'desktop')
if (-not (Test-Path node_modules)) { npm install }
npm run dev
Pop-Location
