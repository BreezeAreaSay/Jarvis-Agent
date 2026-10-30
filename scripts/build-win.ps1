#Requires -Version 5.1
<#
.SYNOPSIS
    Сборка установщика Jarvis на Windows: Jarvis-Setup-<версия>.exe в desktop\release.

.DESCRIPTION
    1. Python 3.12 для встраивания (пакет NuGet «python») → build\core-win\python
    2. Зависимости ядра (uv pip install --target), пакет jarvis и словарь ударений голоса
       (scripts\ru_dict\build_ru_dict.py; -SkipStressDict — без него) → build\core-win
    3. Интерфейс (npm run build) и установщик (electron-builder, NSIS)
    Нужны: uv (https://docs.astral.sh/uv/) и Node.js 22+.

    Подпись: electron-builder подписывает Jarvis.exe, установщик и деинсталлятор — SmartScreen и антивирусы
    не пугают. Сертификат на токене (так их теперь и выдают) — -CertificateSubject «имя из сертификата»:
    signtool найдёт его в хранилище Windows; файл .pfx — -Certificate (пароль — -CertificatePassword или
    спросит) или переменные CSC_LINK и CSC_KEY_PASSWORD. Без сертификата — как раньше, без подписи.

.EXAMPLE
    pwsh -File scripts\build-win.ps1 -CertificateSubject "Ivan Ivanov"
.EXAMPLE
    pwsh -File scripts\build-win.ps1 -Certificate C:\keys\jarvis.pfx
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12.10',
    [switch]$SkipCore,
    [switch]$SkipStressDict,
    [string]$Certificate = '',
    [string]$CertificatePassword = '',
    [string]$CertificateSubject = ''
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

# подпись: сертификат из параметра — electron-builder берёт его из CSC_LINK / CSC_KEY_PASSWORD
if ($Certificate) {
    if (-not (Test-Path -LiteralPath $Certificate)) { throw "Нет файла сертификата $Certificate" }
    $env:CSC_LINK = (Resolve-Path -LiteralPath $Certificate).Path
    if ($CertificatePassword) {
        $env:CSC_KEY_PASSWORD = $CertificatePassword
    } elseif (-not $env:CSC_KEY_PASSWORD) {
        $secure = Read-Host 'Пароль сертификата' -AsSecureString
        $env:CSC_KEY_PASSWORD = (New-Object System.Net.NetworkCredential('', $secure)).Password
    }
}
$SignArgs = @()
if ($CertificateSubject) {
    # сертификат на токене или в хранилище Windows — по имени; signtool спросит PIN токена сам
    $SignArgs = @('--', "--config.win.signtoolOptions.certificateSubjectName=$CertificateSubject")
    Write-Host "==> Подпись: сертификат «$CertificateSubject» из хранилища Windows"
} elseif ($env:CSC_LINK -or $env:WIN_CSC_LINK) {
    Write-Host '==> Подпись: установщик и Jarvis.exe будут подписаны сертификатом'
} else {
    Write-Host '==> Подпись: без сертификата — SmartScreen предупредит при установке (см. README, «Подпись установщика»)'
}

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
    foreach ($junk in 'bin', 'Scripts') {
        $p = Join-Path $Out "python\Lib\site-packages\$junk"
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }

    Write-Host '==> Пакет jarvis'
    New-Item -ItemType Directory -Force (Join-Path $Out 'app') | Out-Null
    robocopy (Join-Path $Root 'core\jarvis') (Join-Path $Out 'app\jarvis') /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
    & (Join-Path $Out 'python\python.exe') -m compileall -q (Join-Path $Out 'app\jarvis') | Out-Null

    # словарь ударений и «ё» голоса Jarvis (scripts\ru_dict, свои лицензии — LICENSE.md там же): собирается из
    # закреплённых по SHA-256 исходников; ядро ставит его голосу piper само. Путь — латиницей (espeak-ng)
    if (-not $SkipStressDict) {
        Write-Host '==> Словарь ударений голоса'
        $voiceOut = Join-Path $Root 'build\voice'
        & (Join-Path $Out 'python\python.exe') (Join-Path $Root 'scripts\ru_dict\build_ru_dict.py') --out $voiceOut
        if ($LASTEXITCODE -ne 0) { throw 'Словарь ударений не собрался (или запустите с -SkipStressDict)' }
        $res = Join-Path $Out 'app\jarvis\resources\voice'
        New-Item -ItemType Directory -Force $res | Out-Null
        foreach ($f in 'ru_dict', 'ru_dict.json', 'LICENSE.md') { Copy-Item (Join-Path $voiceOut $f) $res }
    }
}

Write-Host '==> Интерфейс и установщик'
Push-Location (Join-Path $Root 'desktop')
npm ci
npm run dist @SignArgs
Pop-Location
Get-ChildItem (Join-Path $Root 'desktop\release') -Filter 'Jarvis-Setup-*.exe' | ForEach-Object { Write-Host "Готово: $($_.FullName)" }
