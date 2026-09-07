[CmdletBinding()]
param(
    # Toolchain for the native overlay DLL (forwarded to build_native.ps1).
    # CI passes "msvc"; local defaults to MinGW when present.
    [ValidateSet("auto", "mingw", "msvc", "clang-cl")]
    [string]$NativeToolchain = "auto",
    # C compiler Nuitka uses for the Python-to-C compilation on Windows.
    #   auto - Nuitka auto-detection (MSVC when installed - the supported path
    #          for Python 3.14, where the MinGW64 backend was removed)
    #   msvc - force Visual Studio (--msvc)
    #   clang - force clang-cl (--clang; still needs a VS installation)
    #   zig  - experimental Nuitka zig cc backend (--zig), fallback when no MSVC
    [ValidateSet("auto", "msvc", "clang", "zig")]
    [string]$CCompiler = "auto"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

# --- Resolve the Python interpreter (uv layout first, then bare uv). --------
if ($env:VIRTUAL_ENV) {
    $python = Join-Path $env:VIRTUAL_ENV "Scripts/python.exe"
} elseif (Test-Path -LiteralPath (Join-Path $projectRoot ".venv/Scripts/python.exe") -PathType Leaf) {
    $python = Join-Path $projectRoot ".venv/Scripts/python.exe"
} elseif (Get-Command uv.exe -ErrorAction SilentlyContinue) {
    # No synced venv yet - run inside uv's ephemeral environment. --no-sync
    # keeps this fast when the caller already ran `uv sync --group build`.
    $python = $null
    $uvPrefix = @("uv", "run", "--no-sync", "python")
} else {
    throw "No Python found. Install uv and run 'uv sync --group build' first."
}

if ($python) {
    $interp = @($python)
} else {
    $interp = $uvPrefix
}

$soundcardOutput = & $interp -c "import os, soundcard; print(os.path.dirname(soundcard.__file__))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not locate the installed soundcard package. Run 'uv sync --group build' first."
}
$soundcardDirectory = ($soundcardOutput -join "`n").Trim()
if ([string]::IsNullOrWhiteSpace($soundcardDirectory)) {
    throw "Could not locate the installed soundcard package. Run 'uv sync --group build' first."
}

$soundcardHeader = Join-Path $soundcardDirectory "mediafoundation.py.h"
if (-not (Test-Path -LiteralPath $soundcardHeader -PathType Leaf)) {
    throw "soundcard header is missing: $soundcardHeader"
}

# --- Build the native overlay DLL (same script local dev and CI use). --------
$nativeDllPath = & (Join-Path $PSScriptRoot "build_native.ps1") -Toolchain $NativeToolchain
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($nativeDllPath | Select-Object -Last 1))) {
    throw "Native overlay build failed."
}
$nativeDll = Get-Item -LiteralPath ($nativeDllPath | Select-Object -Last 1)

$qtWebEngineLocale = & $interp -c "import os, PyQt6; print(os.path.join(os.path.dirname(PyQt6.__file__), 'Qt6', 'translations', 'qtwebengine_locales', 'en-US.pak'))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not locate the PyQt6 QtWebEngine locale package. Run 'uv sync --group build' first."
}
$qtWebEngineLocale = ($qtWebEngineLocale -join "`n").Trim()
if ([string]::IsNullOrWhiteSpace($qtWebEngineLocale) -or -not (Test-Path -LiteralPath $qtWebEngineLocale -PathType Leaf)) {
    throw "QtWebEngine locale package is missing: $qtWebEngineLocale"
}

$nuitkaArguments = @(
    # Allow Nuitka to fetch the Dependency Walker tool onefile needs on Windows.
    "--assume-yes-for-downloads"
    "--include-data-files=$soundcardHeader=soundcard/mediafoundation.py.h"
    "--include-data-files=$qtWebEngineLocale=qtwebengine_locales/en-US.pak"
    "--include-data-files=$($nativeDll.FullName)=native/overlay_native.dll"
)
switch ($CCompiler) {
    # Nuitka >= 4.2 requires a version argument for --msvc ("latest" = the
    # newest installed VS, which is also what auto-detection picks).
    "msvc" { $nuitkaArguments += "--msvc=latest" }
    "clang" { $nuitkaArguments += "--clang" }
    "zig" { $nuitkaArguments += "--zig" }
}
$nuitkaArguments += "src/main.py"

& $interp -m nuitka @nuitkaArguments
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka build failed."
}

$artifact = Join-Path $projectRoot "dist/nuitka/VisualAudioOverlay.exe"
if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "Nuitka completed without producing the expected artifact: $artifact"
}
Write-Host "Build complete: $artifact"
