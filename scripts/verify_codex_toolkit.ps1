param(
    [string]$Provider = "openai",
    [switch]$SkipRealDryRuns
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Command
}

Invoke-Step "Unit tests: tests/test_core_workflows.py" {
    python -m unittest discover -s tests -p "test_core_workflows.py"
}

Invoke-Step "Compile core Codex modules" {
    python -m py_compile `
        src\ai_cli_kit\codex\services\provider.py `
        src\ai_cli_kit\codex\services\clone.py `
        src\ai_cli_kit\codex\services\repair.py `
        src\ai_cli_kit\codex\services\dedupe.py `
        src\ai_cli_kit\codex\services\promote.py `
        src\ai_cli_kit\codex\services\switching.py `
        src\ai_cli_kit\codex\stores\session_files.py `
        src\ai_cli_kit\codex\stores\desktop_state.py `
        src\ai_cli_kit\codex\commands.py `
        src\ai_cli_kit\codex\cli.py
}

if (-not $SkipRealDryRuns) {
    Invoke-Step "Real data dry-run: dedupe-clones $Provider" {
        $env:PYTHONPATH = "src"
        python -m ai_cli_kit codex dedupe-clones $Provider --dry-run
    }

    Invoke-Step "Real data dry-run: repair-desktop $Provider" {
        $env:PYTHONPATH = "src"
        python -m ai_cli_kit codex repair-desktop $Provider --dry-run
    }
}

Write-Host ""
Write-Host "Verification completed." -ForegroundColor Green

