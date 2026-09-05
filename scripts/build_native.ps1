[CmdletBinding()]
param(
    # Which toolchain builds the native overlay DLL.
    #   auto    - MinGW Makefiles if mingw32-make is on PATH, else MSVC via
    #             VsDevCmd (the newest Visual Studio vswhere finds)
    #   mingw   - MinGW Makefiles (local default; no MSVC headers needed for
    #             the DLL's pure C ABI)
    #   msvc    - MSVC cl.exe under the VsDevCmd environment (CI; /MT static
    #             runtime). Version-agnostic: the "Visual Studio <year>"
    #             CMake generators hardcode a VS version that CMake releases
    #             can lag behind, so we go single-config through Ninja (or
    #             NMake, which ships with every VS, when ninja is missing)
    #   clang-cl- Ninja + clang-cl (needs a vcvars-initialized shell)
    [ValidateSet("auto", "mingw", "msvc", "clang-cl")]
    [string]$Toolchain = "auto"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$nativeSource = Join-Path $projectRoot "native/overlay_native"
$nativeBuild = Join-Path $nativeSource "build"

function Write-HostOut {
    # Send non-result output to the host only, keeping the success stream for
    # the final DLL path (build_nuitka.ps1 captures it).
    Write-Host @args
}

function Enable-MsvcEnv {
    # Locate Visual Studio (any edition, any year) with the C++ x64 tools and
    # import its developer environment (INCLUDE/LIB/PATH, ...) into this
    # process so cl.exe and the Windows SDK are visible to CMake.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio/Installer/vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "No supported native toolchain found. Install MinGW-w64 (DLL builds) or Visual Studio Build Tools."
    }
    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $vsPath) {
        throw "vswhere found no Visual Studio with C++ x64 tools. Install the 'Desktop development with C++' workload."
    }
    $devCmd = Join-Path $vsPath "Common7/Tools/VsDevCmd.bat"
    if (-not (Test-Path -LiteralPath $devCmd -PathType Leaf)) {
        throw "Visual Studio at $vsPath has no Common7/Tools/VsDevCmd.bat."
    }
    # Ask cmd to print the dev environment in one go; /S strips the outer
    # quotes, and cmd's hidden "=C:=..." variables are skipped by the parse.
    # VsDevCmd can print a non-fatal vswhere error to stderr, which would
    # terminate under $ErrorActionPreference=Stop; relax and check the exit code.
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $envLines = cmd.exe /S /C """$devCmd"" -arch=x64 -host_arch=x64 && set" 2>&1
    $cmdExit = $LASTEXITCODE
    $ErrorActionPreference = $previousEap
    if ($cmdExit -ne 0) {
        throw "VsDevCmd.bat exited with code $cmdExit."
    }
    foreach ($line in $envLines) {
        # stderr from cmd arrives as ErrorRecord objects (2>&1 merge).
        $text = "$line"
        $eq = $text.IndexOf("=")
        if ($eq -le 0) { continue }
        [Environment]::SetEnvironmentVariable($text.Substring(0, $eq), $text.Substring($eq + 1), "Process")
    }
    if (-not $env:INCLUDE) {
        throw "VsDevCmd.bat did not export the MSVC environment (INCLUDE is empty)."
    }
}

$msvcToolchain = $false
$generator = $null
$compilerArgs = @()
switch ($Toolchain) {
    "mingw" {
        $generator = "MinGW Makefiles"
    }
    "msvc" {
        $msvcToolchain = $true
    }
    "clang-cl" {
        $generator = "Ninja"
        $compilerArgs = @("-DCMAKE_C_COMPILER=clang-cl", "-DCMAKE_CXX_COMPILER=clang-cl")
    }
    "auto" {
        if (Get-Command mingw32-make.exe -ErrorAction SilentlyContinue) {
            $generator = "MinGW Makefiles"
        } else {
            $msvcToolchain = $true
        }
    }
}

if ($msvcToolchain) {
    Enable-MsvcEnv
    $generator = if (Get-Command ninja.exe -ErrorAction SilentlyContinue) {
        "Ninja"
    } else {
        Write-HostOut "ninja not found on PATH - falling back to NMake Makefiles."
        "NMake Makefiles"
    }
    # MSVC_RUNTIME_LIBRARY keys on CONFIG (see CMakeLists.txt); without a
    # build type the DLL would link /MD and need the VC++ redistributable.
    $compilerArgs += "-DCMAKE_BUILD_TYPE=Release"
}

Write-HostOut "Configuring native overlay ($generator)..."
& cmake -S $nativeSource -B $nativeBuild -G $generator @compilerArgs "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
if ($LASTEXITCODE -ne 0) {
    throw "Native overlay CMake configuration failed."
}

Write-HostOut "Building native overlay (Release)..."
& cmake --build $nativeBuild --config Release --parallel
if ($LASTEXITCODE -ne 0) {
    throw "Native overlay CMake build failed."
}

# MSVC puts the DLL in a per-config subdirectory; single-config generators in
# the build root.
$nativeDll = Get-ChildItem -LiteralPath $nativeBuild -Recurse -Filter "overlay_native.dll" |
    Select-Object -First 1
if ($null -eq $nativeDll) {
    throw "Native overlay build produced no overlay_native.dll under $nativeBuild."
}

Write-HostOut "Native overlay DLL: $($nativeDll.FullName)"
Write-Output $nativeDll.FullName
