#!/usr/bin/env bash
# Build the native overlay DLL. Mirrors build_native.ps1.
# Usage: build_native.sh [auto|mingw|msvc|clang-cl]   (default: auto)

set -euo pipefail

toolchain="${1:-auto}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
native_source="$project_root/native/overlay_native"
native_build="$native_source/build"

log() { printf '==> %s\n' "$*" >&2; }

# The msvc toolchain must run inside the Visual Studio developer environment
# (INCLUDE/LIB/PATH for cl.exe), which VsDevCmd.bat sets up - importing that
# environment is far more reliable from PowerShell than from bash. Delegate
# to the ps1 script; it prints the DLL path as its last stdout line, which we
# keep and re-emit.
run_msvc() {
    local dll
    dll="$(powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script_dir/build_native.ps1" -Toolchain msvc 2>&1 | tail -n 1)"
    if [[ -z "$dll" || ! -f "$dll" ]]; then
        printf '%s\n' "MSVC native overlay build failed (see the PowerShell output above)." >&2
        exit 1
    fi
    printf '%s\n' "$dll"
    exit 0
}

compiler_args=()
case "$toolchain" in
    mingw) generator="MinGW Makefiles" ;;
    msvc) run_msvc ;;
    clang-cl)
        generator="Ninja"
        compiler_args=(-DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl)
        ;;
    auto)
        if command -v mingw32-make >/dev/null 2>&1; then
            generator="MinGW Makefiles"
        else
            run_msvc
        fi
        ;;
    *)
        printf 'Unknown toolchain: %s (auto|mingw|msvc|clang-cl)\n' "$toolchain" >&2
        exit 1
        ;;
esac

log "Configuring native overlay ($generator)..."
cmake -S "$native_source" -B "$native_build" -G "$generator" "${compiler_args[@]}" \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
log "Building native overlay (Release)..."
cmake --build "$native_build" --config Release --parallel

native_dll="$(find "$native_build" -type f -iname 'overlay_native.dll' -print -quit)"
if [[ -z "$native_dll" ]]; then
    printf '%s\n' "Native overlay build produced no overlay_native.dll under $native_build." >&2
    exit 1
fi

log "Native overlay DLL: $native_dll"
printf '%s\n' "$native_dll"
