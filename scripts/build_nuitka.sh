#!/usr/bin/env bash
# Build the onefile VisualAudioOverlay.exe. Mirrors build_nuitka.ps1.
# Usage: build_nuitka.sh [native_toolchain] [c_compiler]
#   native_toolchain: auto|mingw|msvc|clang-cl   (default auto)
#   c_compiler:       auto|msvc|clang|zig        (default auto)

set -euo pipefail

native_toolchain="${1:-auto}"
c_compiler="${2:-auto}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

# --- Resolve the Python interpreter (uv layout first, then bare uv). --------
interp=()
if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/Scripts/python.exe" ]]; then
    interp=("$VIRTUAL_ENV/Scripts/python.exe")
elif [[ -x "$project_root/.venv/Scripts/python.exe" ]]; then
    interp=("$project_root/.venv/Scripts/python.exe")
elif command -v uv >/dev/null 2>&1; then
    interp=(uv run --no-sync python)
else
    printf '%s\n' "No Python found. Install uv and run 'uv sync --group build' first." >&2
    exit 1
fi

if ! soundcard_directory="$("${interp[@]}" -c 'import os, soundcard; print(os.path.dirname(soundcard.__file__))')"; then
    printf '%s\n' "Could not locate the installed soundcard package. Run 'uv sync --group build' first." >&2
    exit 1
fi
soundcard_directory="${soundcard_directory//$'\r'/}"
if [[ -z "$soundcard_directory" ]]; then
    printf '%s\n' "Could not locate the installed soundcard package. Run 'uv sync --group build' first." >&2
    exit 1
fi
soundcard_header="$soundcard_directory/mediafoundation.py.h"
if [[ ! -f "$soundcard_header" ]]; then
    printf '%s\n' "soundcard header is missing: $soundcard_header" >&2
    exit 1
fi

# --- Build the native overlay DLL (same script local dev and CI use). --------
native_dll="$(bash "$script_dir/build_native.sh" "$native_toolchain")"
if [[ -z "$native_dll" || ! -f "$native_dll" ]]; then
    printf '%s\n' "Native overlay build failed." >&2
    exit 1
fi

if ! qtwebengine_locale="$("${interp[@]}" -c 'import os, PyQt6; print(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "translations", "qtwebengine_locales", "en-US.pak"))')"; then
    printf '%s\n' "Could not locate the PyQt6 QtWebEngine locale package. Run 'uv sync --group build' first." >&2
    exit 1
fi
qtwebengine_locale="${qtwebengine_locale//$'\r'/}"
if [[ ! -f "$qtwebengine_locale" ]]; then
    printf '%s\n' "QtWebEngine locale package is missing: $qtwebengine_locale" >&2
    exit 1
fi

nuitka_arguments=(
    # Allow Nuitka to fetch the Dependency Walker tool onefile needs on Windows.
    "--assume-yes-for-downloads"
    "--include-data-files=$soundcard_header=soundcard/mediafoundation.py.h"
    "--include-data-files=$qtwebengine_locale=qtwebengine_locales/en-US.pak"
    "--include-data-files=$native_dll=native/overlay_native.dll"
)
# Nuitka >= 4.2 requires a version argument for --msvc ("latest" = the
# newest installed VS, which is also what auto-detection picks).
case "$c_compiler" in
    msvc) nuitka_arguments+=("--msvc=latest") ;;
    clang) nuitka_arguments+=("--clang") ;;
    zig) nuitka_arguments+=("--zig") ;;
esac
if [[ -d "$project_root/vendor" ]]; then
    nuitka_arguments+=("--include-data-dir=vendor=vendor")
fi

if ! "${interp[@]}" -m nuitka "${nuitka_arguments[@]}" src/main.py; then
    printf '%s\n' "Nuitka build failed." >&2
    exit 1
fi

artifact="$project_root/dist/nuitka/VisualAudioOverlay.exe"
if [[ ! -f "$artifact" ]]; then
    printf '%s\n' "Nuitka completed without producing the expected artifact: $artifact" >&2
    exit 1
fi
printf 'Build complete: %s\n' "$artifact"
