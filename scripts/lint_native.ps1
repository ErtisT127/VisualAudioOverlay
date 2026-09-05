[CmdletBinding()]
param(
    # CI mode: lint against the Ninja+clang-cl compile db in build-lint/ with the
    # narrow critical check set and emit GitHub ::error annotations. Local mode
    # (default): lint against the MinGW compile db in build/ with the full
    # .clang-tidy set.
    [switch]$Ci,
    # Override the compile_commands.json directory (CI passes build-lint).
    [string]$BuildDir = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$nativeSource = Join-Path $projectRoot "native/overlay_native"
if ([string]::IsNullOrWhiteSpace($BuildDir)) {
    $BuildDir = Join-Path $nativeSource $(if ($Ci) { "build-lint" } else { "build" })
} elseif (-not [System.IO.Path]::IsPathRooted($BuildDir)) {
    $BuildDir = Join-Path $projectRoot $BuildDir
}
$compileDb = Join-Path $BuildDir "compile_commands.json"
if (-not (Test-Path -LiteralPath $compileDb -PathType Leaf)) {
    $tool = if ($Ci) { "lint_native.ps1 -Ci" } else { "build_native.ps1" }
    throw "No compile database at $compileDb. Configure the native project first (scripts/$tool)."
}

# Resolve clang-tidy: PATH first (standalone LLVM), then the VS-bundled LLVM
# toolset, then the default install location.
$clangTidy = Get-Command clang-tidy.exe -ErrorAction SilentlyContinue
if (-not $clangTidy) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio/Installer/vswhere.exe"
    if (Test-Path -LiteralPath $vswhere -PathType Leaf) {
        $vsClangTidy = & $vswhere -latest -products * -find "VC\Tools\Llvm\**\bin\clang-tidy.exe" |
            Select-Object -First 1
        if ($vsClangTidy) {
            $clangTidy = Get-Item -LiteralPath $vsClangTidy
        }
    }
}
if (-not $clangTidy) {
    $fallback = "C:\Program Files\LLVM\bin\clang-tidy.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) {
        $clangTidy = Get-Item -LiteralPath $fallback
    }
}
if (-not $clangTidy) {
    throw "clang-tidy not found. Install LLVM (https://llvm.org) or the Visual Studio LLVM toolset."
}

$source = Join-Path $nativeSource "src/overlay_native.cpp"
$argsList = @("-p", $BuildDir)

if ($Ci) {
    # Critical subset only; --warnings-as-errors makes any finding fail.
    $argsList += @(
        "-checks=-*,clang-analyzer-*,bugprone-*,-bugprone-easily-swappable-parameters",
        "--warnings-as-errors=*"
    )
} else {
    # Full .clang-tidy set. The compile db was produced by MinGW, so clang needs
    # the matching GNU target, and must tolerate gcc-only flags such as
    # -municode injected by CMake.
    $argsList += @(
        "--extra-arg-before=--target=x86_64-w64-windows-gnu",
        "--extra-arg-before=-Wno-unused-command-line-argument"
    )
    # If clang cannot find the MinGW headers next to g++ (e.g. g++ is not on
    # PATH when linting), point it at the MinGW root explicitly.
    $mingwRoot = $env:VAO_MINGW_DIR
    if ($mingwRoot -and (Test-Path -LiteralPath (Join-Path $mingwRoot "x86_64-w64-mingw32") -PathType Container)) {
        $argsList += "--extra-arg=--gcc-toolchain=$mingwRoot"
    } elseif (Test-Path -LiteralPath "D:/DevToolkit/MinGW/x86_64-w64-mingw32" -PathType Container) {
        $argsList += "--extra-arg=--gcc-toolchain=D:/DevToolkit/MinGW"
    }
}

# clang-tidy reports findings on stdout; without --warnings-as-errors its exit
# code stays 0, so we count findings ourselves and fail the script either way.
# With $ErrorActionPreference=Stop, clang-tidy's stderr progress lines
# ("N warnings generated") would raise NativeCommandError - relax around the
# call and restore afterwards.
$previousEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$output = & $clangTidy.Source @argsList $source 2>&1
$ErrorActionPreference = $previousEap
$annotation = '^(.+?):(\d+):(\d+): (warning|error): (.*)$'
$found = 0
foreach ($line in $output) {
    $text = "$line"
    Write-Host $text
    if ($text -notmatch $annotation) {
        continue
    }
    $found++
    if ($Ci) {
        # Capture the annotation groups first: every successful -match resets
        # $Matches, and the check-name match below would wipe them.
        $file = $Matches[1].Replace("\", "/")
        $lineNo = $Matches[2]
        $colNo = $Matches[3]
        # clang-tidy appends " [check-name]" to the message; it goes in the
        # annotation title instead, so strip the trailing suffix.
        $message = $Matches[5] -replace '\s+\[[a-z0-9.\-]+\]\s*$', ''
        # GitHub annotations match repo-relative POSIX paths, so strip the
        # project root (compare in one separator space) and keep the rest.
        $rootPrefix = $projectRoot.Replace("\", "/")
        if ($file.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $file = $file.Substring($rootPrefix.Length).TrimStart('/')
        }
        $check = if ($text -match '\[([a-z0-9.\-]+)\]\s*$') { $Matches[1] } else { "clang-tidy" }
        Write-Output "::error file=$file,line=$lineNo,col=$colNo,title=$check::$message"
    }
}

if ($found -gt 0) {
    Write-Host "clang-tidy found $found issue(s) in $source." -ForegroundColor Red
    exit 1
}
