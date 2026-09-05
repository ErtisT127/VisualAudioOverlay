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

$nativeSource = Join-Path $projectRoot "native/overlay_native"
$nativeBuild = Join-Path $nativeSource "build"
$cmakeGenerator = if (Get-Command mingw32-make.exe -ErrorAction SilentlyContinue) {
    "MinGW Makefiles"
} else {
    "Visual Studio 17 2022"
}
& cmake -S $nativeSource -B $nativeBuild -G $cmakeGenerator
if ($LASTEXITCODE -ne 0) {
    throw "Native overlay CMake configuration failed."
}
& cmake --build $nativeBuild --config Release --parallel
if ($LASTEXITCODE -ne 0) {
    throw "Native overlay CMake build failed."
}
$nativeDll = Get-ChildItem -LiteralPath $nativeBuild -Recurse -Filter "overlay_native.dll" |
    Select-Object -First 1
if ($null -eq $nativeDll) {
    $nativeDll = Get-ChildItem -LiteralPath $nativeBuild -Recurse -Filter "*overlay_native.dll" |
        Where-Object { $_.Name -notlike "*.dll.a" } |
        Select-Object -First 1
}
if ($null -eq $nativeDll) {
    throw "Native overlay build produced no overlay_native.dll."
}

$qtWebEngineLocale = & $python -c "import os, PyQt6; print(os.path.join(os.path.dirname(PyQt6.__file__), 'Qt6', 'translations', 'qtwebengine_locales', 'en-US.pak'))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not locate the PyQt6 QtWebEngine locale package. Install requirements.txt first."
}
$qtWebEngineLocale = ($qtWebEngineLocale -join "`n").Trim()
if ([string]::IsNullOrWhiteSpace($qtWebEngineLocale) -or -not (Test-Path -LiteralPath $qtWebEngineLocale -PathType Leaf)) {
    throw "QtWebEngine locale package is missing: $qtWebEngineLocale"
}

$nuitkaArguments = @(
    "--include-data-files=$soundcardHeader=soundcard/mediafoundation.py.h"
    "--include-data-files=$qtWebEngineLocale=qtwebengine_locales/en-US.pak"
    "--include-data-files=$($nativeDll.FullName)=native/overlay_native.dll"
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
