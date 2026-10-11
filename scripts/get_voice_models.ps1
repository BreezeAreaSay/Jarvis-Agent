#Requires -Version 7.0
<#
.SYNOPSIS
    Download Jarvis voice models (speech recognition, VAD, text-to-speech) into <data>\models.

.DESCRIPTION
    <data> is $env:JARVIS_DATA_DIR, otherwise $env:LOCALAPPDATA\Jarvis. Everything runs locally; the models
    come from the GitHub releases of k2-fsa/sherpa-onnx (tags asr-models and tts-models). GigaAM v3 can
    also be fetched file by file from Hugging Face (-Source hf). Every file is checked by size and SHA-256,
    archives are unpacked with tar.exe (Windows 10+). Folders created:
        models\gigaam-v3 | gigaam-v3-punct | gigaam-v2 | vosk-small   speech recognition ([voice] model)
        models\vad\silero_vad.onnx                                     silence trimming ([voice] vad)
        models\tts-denis | tts-dmitri | tts-ruslan | tts-irina         answers by voice ([voice] tts_voice)
    Licenses: GigaAM - MIT; Vosk small - Apache-2.0; Silero VAD - MIT; piper voices denis and dmitri -
    dataset CC0, ruslan - CC BY-NC-SA 4.0 (non-commercial).

.PARAMETER Asr
    Speech recognition model: gigaam-v3 (default), gigaam-v3-punct, gigaam-v2, vosk-small or none.

.PARAMETER Voice
    Voice for spoken answers: ruslan (default, male), denis (male), dmitri, irina (female) or none.

.PARAMETER NoVad
    Do not download Silero VAD.

.PARAMETER Source
    Where GigaAM v3 comes from: github (default, one archive) or hf (Hugging Face, file by file).

.PARAMETER Mirror
    Base URL instead of https://github.com/k2-fsa/sherpa-onnx/releases/download (same <tag>/<file> layout).

.PARAMETER Force
    Download again even if the model is already in place.

.PARAMETER SkipHash
    Keep a file whose SHA-256 differs from the known one (the release was re-uploaded).

.EXAMPLE
    pwsh -File scripts\get_voice_models.ps1
.EXAMPLE
    pwsh -File scripts\get_voice_models.ps1 -Asr vosk-small -Voice ruslan
#>
[CmdletBinding()]
param(
    [ValidateSet('gigaam-v3', 'gigaam-v3-punct', 'gigaam-v2', 'vosk-small', 'none')]
    [string]$Asr = 'gigaam-v3',
    [ValidateSet('denis', 'dmitri', 'ruslan', 'irina', 'none')]
    [string]$Voice = 'ruslan',
    [switch]$NoVad,
    [ValidateSet('github', 'hf')]
    [string]$Source = 'github',
    [string]$Mirror = 'https://github.com/k2-fsa/sherpa-onnx/releases/download',
    [switch]$Force,
    [switch]$SkipHash
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # the progress bar slows Invoke-WebRequest down a lot

# --- what to download (sizes and SHA-256 checked on 2026-10-11) ---------------------------------------
$AsrModels = @{
    'gigaam-v3'       = @{
        Tag = 'asr-models'; Name = 'sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16'
        Size = 167388020; Sha = '20a439491904b839f35eb8efaa3d99cbfaaad0c6dcba22d06ff218bb0056772d'
    }
    'gigaam-v3-punct' = @{
        Tag = 'asr-models'; Name = 'sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16'
        Size = 170197019; Sha = 'f9620a0099019c6afcee26525ef9ed3297fa50dd5691c1902af0c948fc1a470b'
    }
    'gigaam-v2'       = @{
        Tag = 'asr-models'; Name = 'sherpa-onnx-nemo-transducer-giga-am-v2-russian-2025-04-19'
        Size = 172422327; Sha = '6c4bec4c4a70961fc4ec583bf5506089480a9a649c1816ef96b6e4fb8338cd71'
    }
    'vosk-small'      = @{
        Tag = 'asr-models'; Name = 'sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16'
        Size = 24110855; Sha = '6ba68a01ff3c5445aaf2d61e9b97b026f1149dcc9049d11af3f44f55176341d8'
    }
}
# GigaAM v3 file by file from Hugging Face (-Source hf)
$HfRepo = 'https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/resolve/main'
$HfFiles = @(
    @{ File = 'encoder.int8.onnx'; Size = 224570814; Sha = '0ec54e5f130a91c0f228d21795ede10b31c992a1f73e45dcd827e843a71a9b50' }
    @{ File = 'decoder.onnx'; Size = 3331651; Sha = '5d1cfef155d8e07f213bd2f3229b5f6c5c29355c7fa5c1b9eb62cb1925725254' }
    @{ File = 'joiner.onnx'; Size = 1440448; Sha = 'fd1d02f45c2ad3d6b67cc149811ad794ab4b020ed49a0a9e2790a8619d1cddd8' }
    @{ File = 'tokens.txt'; Size = 196; Sha = '17cc514451bcceac9c280068c71502f8448f99e9fb1456b8d0761651fd0392f2' }
)
$Vad = @{
    Tag = 'asr-models'; File = 'silero_vad.onnx'
    Size = 643854; Sha = '9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6'
}
$Voices = @{
    'denis'  = @{ Size = 67190991; Sha = 'efa4c18e0b5e32b81d1b6df36b9d312831e5d545200e27848ef926a4cd930300' }
    'dmitri' = @{ Size = 67188551; Sha = 'c86d0803737de13d441923ff3b3f309482fab8d7af3ec85949942809eb9a3660' }
    'ruslan' = @{ Size = 67210684; Sha = '0690b1cad01f86e8db9ba988af24898bdc1af774e23cb2e46b9c730269b6fd83' }
    'irina'  = @{ Size = 67153308; Sha = '' }  # size from the release page; hash not checked yet
}

# --- helpers ------------------------------------------------------------------------------------------
function Get-DataDir {
    if ($env:JARVIS_DATA_DIR) { return $env:JARVIS_DATA_DIR }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA 'Jarvis') }
    throw 'Neither JARVIS_DATA_DIR nor LOCALAPPDATA is set'
}

function Get-Tar {
    if ($IsWindows) {
        # bsdtar from Windows 10+, not GNU tar from Git (it reads "C:" as a remote host)
        $system = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (Test-Path $system) { return $system }
    }
    $found = Get-Command tar -ErrorAction SilentlyContinue
    if (-not $found) { throw 'tar not found (Windows 10 1803+ has C:\Windows\System32\tar.exe)' }
    return $found.Source
}

function Save-File([string]$Url, [string]$Path, [long]$Size, [string]$Sha) {
    $part = "$Path.part"
    Write-Host "  download $Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $part
    } catch {
        Remove-Item -Force $part -ErrorAction SilentlyContinue
        throw "download failed: $Url ($($_.Exception.Message))"
    }
    $got = (Get-Item $part).Length
    if ($Size -gt 0 -and $got -ne $Size) {
        Remove-Item -Force $part
        throw "size mismatch for ${Url}: $got bytes, expected $Size"
    }
    if ($Sha) {
        $hash = (Get-FileHash -Algorithm SHA256 $part).Hash.ToLowerInvariant()
        if ($hash -ne $Sha) {
            if (-not $SkipHash) {
                Remove-Item -Force $part
                throw "SHA-256 mismatch for ${Url}`n  got      $hash`n  expected $Sha`n  (release re-uploaded? run again with -SkipHash)"
            }
            Write-Warning "SHA-256 differs for $Url ($hash) - kept because of -SkipHash"
        }
    }
    Move-Item -Force $part $Path
    '  ok: {0:N1} MB' -f ($got / 1MB) | Write-Host
}

function Expand-Model([string]$Archive, [string]$TopDir, [string]$Target) {
    $work = Join-Path $script:Models (".unpack-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $work | Out-Null
    try {
        Write-Host "  unpack $([IO.Path]::GetFileName($Archive))"
        & $script:Tar -xf $Archive -C $work
        if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE on $Archive" }
        $src = Join-Path $work $TopDir
        if (-not (Test-Path -LiteralPath $src -PathType Container)) { throw "archive has no folder $TopDir" }
        if (Test-Path -LiteralPath $Target) { Remove-Item -Recurse -Force -LiteralPath $Target }
        Move-Item -LiteralPath $src -Destination $Target
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
    }
}

function Test-AsrFolder([string]$Folder) {
    if (-not (Test-Path -LiteralPath (Join-Path $Folder 'tokens.txt'))) { return $false }
    foreach ($part in 'encoder', 'decoder', 'joiner') {
        if (-not (Get-ChildItem -LiteralPath $Folder -Filter "$part*.onnx" -ErrorAction SilentlyContinue)) {
            return $false
        }
    }
    return $true
}

function Test-VoiceFolder([string]$Folder) {
    return (Test-Path -LiteralPath (Join-Path $Folder 'tokens.txt')) -and
        (Test-Path -LiteralPath (Join-Path $Folder 'espeak-ng-data') -PathType Container) -and
        [bool](Get-ChildItem -LiteralPath $Folder -Filter '*.onnx' -ErrorAction SilentlyContinue)
}

function Get-Archive([string]$Tag, [string]$Name, [long]$Size, [string]$Sha, [string]$Target) {
    $archive = Join-Path $script:Models "$Name.tar.bz2"
    try {
        Save-File "$($Mirror.TrimEnd('/'))/$Tag/$Name.tar.bz2" $archive $Size $Sha
        Expand-Model $archive $Name $Target
    } finally {
        Remove-Item -Force -LiteralPath $archive -ErrorAction SilentlyContinue
    }
}

# --- main ---------------------------------------------------------------------------------------------
$Data = Get-DataDir
$script:Models = Join-Path $Data 'models'
New-Item -ItemType Directory -Force $script:Models | Out-Null
$script:Tar = Get-Tar
Write-Host "Jarvis voice models -> $script:Models"

if ($Asr -ne 'none') {
    $target = Join-Path $script:Models $Asr
    if ((Test-AsrFolder $target) -and -not $Force) {
        Write-Host "[asr] $Asr is already in place ($target)"
    } elseif ($Asr -eq 'gigaam-v3' -and $Source -eq 'hf') {
        Write-Host "[asr] $Asr from Hugging Face"
        $tmp = "$target.download"
        New-Item -ItemType Directory -Force $tmp | Out-Null
        foreach ($f in $HfFiles) {
            Save-File "$HfRepo/$($f.File)" (Join-Path $tmp $f.File) $f.Size $f.Sha
        }
        if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
        Move-Item -LiteralPath $tmp -Destination $target
    } else {
        $m = $AsrModels[$Asr]
        Write-Host "[asr] $Asr ($('{0:N0}' -f ($m.Size / 1MB)) MB)"
        Get-Archive $m.Tag $m.Name $m.Size $m.Sha $target
    }
    if (-not (Test-AsrFolder $target)) { throw "[asr] $target is incomplete after download" }
}

if (-not $NoVad) {
    $folder = Join-Path $script:Models 'vad'
    $file = Join-Path $folder $Vad.File
    if ((Test-Path -LiteralPath $file) -and (Get-Item -LiteralPath $file).Length -eq $Vad.Size -and -not $Force) {
        Write-Host "[vad] silero_vad.onnx is already in place"
    } else {
        Write-Host '[vad] Silero VAD'
        New-Item -ItemType Directory -Force $folder | Out-Null
        Save-File "$($Mirror.TrimEnd('/'))/$($Vad.Tag)/$($Vad.File)" $file $Vad.Size $Vad.Sha
    }
}

if ($Voice -ne 'none') {
    $target = Join-Path $script:Models "tts-$Voice"
    if ((Test-VoiceFolder $target) -and -not $Force) {
        Write-Host "[tts] $Voice is already in place ($target)"
    } else {
        $v = $Voices[$Voice]
        Write-Host "[tts] voice $Voice ($('{0:N0}' -f ($v.Size / 1MB)) MB)"
        Get-Archive 'tts-models' "vits-piper-ru_RU-$Voice-medium" $v.Size $v.Sha $target
    }
    if (-not (Test-VoiceFolder $target)) { throw "[tts] $target is incomplete after download" }
}

Write-Host ''
Write-Host 'Done. Next: uv run jarvis doctor (or jarvis-cli.exe doctor) - the voice checks must be OK;'
Write-Host "then set [voice] model = `"$Asr`" and tts_voice = `"$Voice`" in jarvis.toml if they differ from defaults."
