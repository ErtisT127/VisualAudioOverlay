#!/usr/bin/env bash
# Local full-quality gate - mirrors scripts/check.ps1. Run from anywhere.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
cd "$project_root"

stage() {
    printf '\n==> %s\n' "$1"
    shift
    "$@"
}

# --- Python: ruff (full rule set) + format gate + pytest ---------------------
stage "Python: ruff format --check" uv run ruff format --check .
stage "Python: ruff check (full set)" uv run ruff check .
stage "Python: pytest" uv run pytest tests -q

# --- Dashboard: prettier + eslint (full) + node tests -----------------------
if ! command -v pnpm >/dev/null 2>&1; then
    printf '%s\n' "pnpm not found. Install pnpm (https://pnpm.io/installation) to run dashboard checks." >&2
    exit 1
fi
stage "Dashboard: prettier --check" pnpm format:check
stage "Dashboard: eslint (full set)" pnpm lint
stage "Dashboard: node tests" pnpm test

# --- Native: clang-tidy against the MinGW compile db ------------------------
stage "Native: clang-tidy (full set)" bash "$script_dir/lint_native.sh"

printf '\nAll checks passed.\n'
