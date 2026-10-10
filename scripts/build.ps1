<#
.SYNOPSIS
    Local build of Jarvis on the owner's PC: tests -> icon -> PyInstaller (onedir) -> selftest -> Inno Setup.

.DESCRIPTION
    Same steps as .github/workflows/build.yml. Run from any folder:
        pwsh -File C:\Jarvis\scripts\build.ps1 [-SkipTests]
    Result: build\dist\Jarvis\ (Jarvis.exe, jarvis-cli.exe, _internal\) and
    build\installer\Jarvis-Setup-<version>.exe. Needs uv and Inno Setup 6 (no admin rights required:
    winget install JRSoftware.InnoSetup --scope user).

.PARAMETER SkipTests
    Do not run pytest before the build.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "build\dist\Jarvis"
$OutDir = Join-Path $Root "build\installer"
$Icon = Join-Path $Root "build\jarvis.ico"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Assert-ExitCode([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE)"
    }
}

function Find-Iscc {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    # winget --scope user installs into %LOCALAPPDATA%\Programs, the regular installer into Program Files (x86)
    $bases = @("$env:LOCALAPPDATA\Programs", ${env:ProgramFiles(x86)}, $env:ProgramFiles) | Where-Object { $_ }
    foreach ($base in $bases) {
        $path = Join-Path $base "Inno Setup 6\ISCC.exe"
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    return $null
}

Push-Location $Root
try {
    Write-Step "uv sync --locked --group build"
    uv sync --locked --group build
    Assert-ExitCode "uv sync"

    if ($SkipTests) {
        Write-Step "tests skipped (-SkipTests)"
    }
    else {
        Write-Step "pytest -m 'not live'"
        uv run --no-sync pytest -q -m "not live"
        Assert-ExitCode "pytest"
    }

    Write-Step "icon -> $Icon"
    uv run --no-sync python packaging/make_icon.py --out $Icon
    Assert-ExitCode "make_icon.py"

    Write-Step "PyInstaller -> $Dist"
    uv run --no-sync pyinstaller packaging/jarvis.spec --noconfirm --distpath build/dist --workpath build/work
    Assert-ExitCode "PyInstaller"

    Write-Step "selftest"
    & (Join-Path $Dist "jarvis-cli.exe") selftest
    Assert-ExitCode "jarvis-cli.exe selftest"

    $files = Get-ChildItem -Recurse -File -LiteralPath $Dist
    $mb = [math]::Round(($files | Measure-Object -Sum Length).Sum / 1MB, 1)
    Write-Host "onedir: $mb MB, $($files.Count) files"

    Write-Step "Inno Setup"
    $version = uv run --no-sync python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
    Assert-ExitCode "reading version from pyproject.toml"
    $version = "$version".Trim()

    $iscc = Find-Iscc
    if (-not $iscc) {
        Write-Host "Inno Setup 6 (ISCC.exe) not found." -ForegroundColor Yellow
        Write-Host "Install it without admin rights:  winget install JRSoftware.InnoSetup --scope user"
        Write-Host "or with Chocolatey:                choco install innosetup -y"
        Write-Host "The onedir build is ready anyway: $Dist"
        exit 1
    }
    & $iscc "/DVersion=$version" "/DSourceDir=$Dist" "/DOutputDir=$OutDir" "/DIconFile=$Icon" (Join-Path $Root "packaging\jarvis.iss")
    Assert-ExitCode "iscc"

    $setup = Join-Path $OutDir "Jarvis-Setup-$version.exe"
    if (-not (Test-Path -LiteralPath $setup)) {
        throw "installer not found: $setup"
    }
    $setupMb = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB, 1)
    Write-Step "done"
    Write-Host "Installer: $setup ($setupMb MB)" -ForegroundColor Green
}
finally {
    Pop-Location
}
