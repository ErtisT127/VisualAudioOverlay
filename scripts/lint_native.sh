#!/usr/bin/env bash
# Run clang-tidy on the native overlay. Mirrors lint_native.ps1.
# Usage: lint_native.sh [--ci] [--build-dir <dir>]
#   default: full .clang-tidy set against the MinGW db in native/.../build/
#   --ci:    critical subset against build-lint/, emits GitHub ::error lines

set -euo pipefail

ci=0
build_dir=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ci) ci=1 ;;
        --build-dir)
            build_dir="${2:-}"
            shift
            ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2 && exit 1 ;;
    esac
    shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
native_source="$project_root/native/overlay_native"

if [[ -n "$build_dir" && "$build_dir" != /* && "$build_dir" != [A-Za-z]:/* ]]; then
    build_dir="$project_root/$build_dir"
fi
if [[ -z "$build_dir" ]]; then
    if [[ "$ci" -eq 1 ]]; then
        build_dir="$native_source/build-lint"
    else
        build_dir="$native_source/build"
    fi
fi

compile_db="$build_dir/compile_commands.json"
if [[ ! -f "$compile_db" ]]; then
    printf '%s\n' "No compile database at $compile_db. Configure the native project first." >&2
    exit 1
fi

clang_tidy="$(command -v clang-tidy || true)"
if [[ -z "$clang_tidy" ]]; then
    vswhere="/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"
    if [[ -x "$vswhere" ]]; then
        clang_tidy="$("$vswhere" -latest -products '*' -find 'VC\Tools\Llvm\**\bin\clang-tidy.exe' 2>/dev/null | head -1)"
    fi
fi
if [[ -z "$clang_tidy" && -x "/c/Program Files/LLVM/bin/clang-tidy.exe" ]]; then
    clang_tidy="/c/Program Files/LLVM/bin/clang-tidy.exe"
fi
if [[ -z "$clang_tidy" ]]; then
    printf '%s\n' "clang-tidy not found. Install LLVM or the Visual Studio LLVM toolset." >&2
    exit 1
fi

args=(-p "$build_dir")
if [[ "$ci" -eq 1 ]]; then
    args+=("-checks=-*,clang-analyzer-*,bugprone-*,-bugprone-easily-swappable-parameters")
    args+=("--warnings-as-errors=*")
else
    args+=("--extra-arg-before=--target=x86_64-w64-windows-gnu")
    args+=("--extra-arg-before=-Wno-unused-command-line-argument")
    mingw_root="${VAO_MINGW_DIR:-}"
    if [[ -n "$mingw_root" && -d "$mingw_root/x86_64-w64-mingw32" ]]; then
        args+=("--extra-arg=--gcc-toolchain=$mingw_root")
    elif [[ -d "/d/DevToolkit/MinGW/x86_64-w64-mingw32" ]]; then
        args+=("--extra-arg=--gcc-toolchain=/d/DevToolkit/MinGW")
    fi
fi

source_file="$native_source/src/overlay_native.cpp"
output="$("$clang_tidy" "${args[@]}" "$source_file" 2>&1)" || true
found=0
while IFS= read -r line; do
    printf '%s\n' "$line"
    if [[ "$line" =~ ^(.+):([0-9]+):([0-9]+):\ (warning|error):\ (.*)$ ]]; then
        found=$((found + 1))
        if [[ "$ci" -eq 1 ]]; then
            file="${BASH_REMATCH[1]#"$project_root"/}"
            check="clang-tidy"
            if [[ "$line" =~ \[([a-z0-9.-]+)\][[:space:]]*$ ]]; then
                check="${BASH_REMATCH[1]}"
            fi
            printf '::error file=%s,line=%s,col=%s,title=%s::%s\n' \
                "$file" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "$check" "${BASH_REMATCH[5]}"
        fi
    fi
done <<< "$output"

if [[ "$found" -gt 0 ]]; then
    printf 'clang-tidy found %s issue(s) in %s\n' "$found" "$source_file" >&2
    exit 1
fi
