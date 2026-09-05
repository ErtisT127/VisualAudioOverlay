#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/Scripts/python.exe" ]]; then
    python="$VIRTUAL_ENV/Scripts/python.exe"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    python="$VIRTUAL_ENV/bin/python"
elif [[ -x "$project_root/.venv/bin/python" ]]; then
    python="$project_root/.venv/bin/python"
elif [[ -x "$project_root/.venv/Scripts/python.exe" ]]; then
    python="$project_root/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    python="python3"
elif command -v python >/dev/null 2>&1; then
    python="python"
else
    printf '%s\n' "Python was not found. Install Python and the project dependencies first." >&2
    exit 1
fi

if ! soundcard_directory="$("$python" -c 'import os, soundcard; print(os.path.dirname(soundcard.__file__))')"; then
    printf '%s\n' "Could not locate the installed soundcard package. Install requirements.txt first." >&2
    exit 1
fi
soundcard_directory="${soundcard_directory//$'\r'/}"
if [[ -z "$soundcard_directory" ]]; then
    printf '%s\n' "Could not locate the installed soundcard package. Install requirements.txt first." >&2
    exit 1
fi

soundcard_header="$soundcard_directory/mediafoundation.py.h"
if [[ ! -f "$soundcard_header" ]]; then
    printf '%s\n' "soundcard header is missing: $soundcard_header" >&2
    exit 1
fi

if ! qtwebengine_locale="$($python -c 'import os, PyQt6; print(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "translations", "qtwebengine_locales", "en-US.pak"))')"; then
    printf '%s\n' "Could not locate the PyQt6 QtWebEngine locale package. Install requirements.txt first." >&2
    exit 1
fi
qtwebengine_locale="${qtwebengine_locale//$'\r'/}"
if [[ ! -f "$qtwebengine_locale" ]]; then
    printf '%s\n' "QtWebEngine locale package is missing: $qtwebengine_locale" >&2
    exit 1
fi

nuitka_arguments=(
    "--include-data-files=$soundcard_header=soundcard/mediafoundation.py.h"
    "--include-data-files=$qtwebengine_locale=qtwebengine_locales/en-US.pak"
)
if [[ -d "$project_root/vendor" ]]; then
    nuitka_arguments+=("--include-data-dir=vendor=vendor")
fi

if ! "$python" -m nuitka "${nuitka_arguments[@]}" main.py; then
    printf '%s\n' "Nuitka build failed." >&2
    exit 1
fi

artifact="$project_root/dist/nuitka/VisualAudioOverlay.exe"
if [[ ! -f "$artifact" ]]; then
    printf '%s\n' "Nuitka completed without producing the expected artifact: $artifact" >&2
    exit 1
fi
