[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if ($env:VIRTUAL_ENV) {
    $python = Join-Path $env:VIRTUAL_ENV "Scripts/python.exe"
} elseif (Test-Path -LiteralPath (Join-Path $projectRoot ".venv/Scripts/python.exe") -PathType Leaf) {
    $python = Join-Path $projectRoot ".venv/Scripts/python.exe"
} else {
    $python = "python"
}

$soundcardOutput = & $python -c "import os, soundcard; print(os.path.dirname(soundcard.__file__))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not locate the installed soundcard package. Install requirements.txt first."
}
$soundcardDirectory = ($soundcardOutput -join "`n").Trim()
if ([string]::IsNullOrWhiteSpace($soundcardDirectory)) {
    throw "Could not locate the installed soundcard package. Install requirements.txt first."
}

$soundcardHeader = Join-Path $soundcardDirectory "mediafoundation.py.h"
if (-not (Test-Path -LiteralPath $soundcardHeader -PathType Leaf)) {
    throw "soundcard header is missing: $soundcardHeader"
}

$nuitkaArguments = @(
    "--include-data-files=$soundcardHeader=soundcard/mediafoundation.py.h"
)
if (Test-Path -LiteralPath (Join-Path $projectRoot "vendor") -PathType Container) {
    $nuitkaArguments += "--include-data-dir=vendor=vendor"
}
$nuitkaArguments += "main.py"

& $python -m nuitka @nuitkaArguments
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka build failed."
}

$artifact = Join-Path $projectRoot "dist/nuitka/VisualAudioOverlay.exe"
if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "Nuitka completed without producing the expected artifact: $artifact"
}
