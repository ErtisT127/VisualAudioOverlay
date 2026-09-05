[CmdletBinding()]
param()

# Local full-quality gate - mirrors what CI enforces with its critical-only
# subsets. Run from anywhere; executes every stage fail-fast.
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw "Stage failed: $Name"
    }
}

# --- Python: ruff (full rule set) + format gate + pytest ---------------------
Invoke-Stage "Python: ruff format --check" { uv run ruff format --check . }
Invoke-Stage "Python: ruff check (full set)" { uv run ruff check . }
Invoke-Stage "Python: pytest" { uv run pytest tests -q }

# --- Dashboard: prettier + eslint (full) + node tests -----------------------
if (-not (Get-Command pnpm.cmd -ErrorAction SilentlyContinue) -and
        -not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "pnpm not found. Install pnpm (https://pnpm.io/installation) to run dashboard checks."
}
Invoke-Stage "Dashboard: prettier --check" { pnpm format:check }
Invoke-Stage "Dashboard: eslint (full set)" { pnpm lint }
Invoke-Stage "Dashboard: node tests" { pnpm test }

# --- Native: clang-tidy against the MinGW compile db ------------------------
Invoke-Stage "Native: clang-tidy (full set)" { & (Join-Path $PSScriptRoot "lint_native.ps1") }

Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
