param(
    [switch] $Editable,
    [switch] $Force,
    [switch] $NoScripts,
    [Alias("h")]
    [switch] $Help,
    [string] $Python
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$venvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $projectRoot ".venv" }

function Show-Usage {
    @"
Usage: .\install.ps1 [-Editable] [-Force] [-NoScripts] [-Python <python-bin>]

Options:
  -Editable        Install in editable mode for local development
  -Force           Recreate the local .venv before installing
  -NoScripts       Install the package but don't keep aik / cst /
                   codex-session-toolkit / cc-clean console scripts in
                   <venv>\Scripts (use ``python -m ai_cli_kit`` instead).
                   Useful if you don't want extra commands on PATH.
  -Python <bin>    Use a specific Python executable
"@
}

function Test-PythonCommand {
    param([string[]] $CommandParts)

    $exe = $CommandParts[0]
    $preArgs = @()
    if ($CommandParts.Length -gt 1) {
        $preArgs = $CommandParts[1..($CommandParts.Length - 1)]
    }

    & $exe @preArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-PythonCommand {
    param([string] $PreferredPython)

    $candidates = @()
    if ($PreferredPython) {
        $candidates += ,@($PreferredPython)
    } else {
        foreach ($cmd in Get-Command "python" -All -ErrorAction SilentlyContinue) {
            $candidates += ,@($cmd.Source)
        }
        if (Get-Command "py" -ErrorAction SilentlyContinue) {
            $candidates += ,@("py", "-3.12")
            $candidates += ,@("py", "-3")
        }
        foreach ($cmd in Get-Command "python3" -All -ErrorAction SilentlyContinue) {
            $candidates += ,@($cmd.Source)
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-PythonCommand -CommandParts $candidate) {
            return ,$candidate
        }
    }
    return $null
}

function Assert-LastExitCode {
    param([string] $StepName)

    if ($LASTEXITCODE -ne 0) {
        throw "$StepName failed with exit code $LASTEXITCODE."
    }
}

if ($Help) {
    Show-Usage
    exit 0
}

$pyCmd = Resolve-PythonCommand -PreferredPython $Python
if (-not $pyCmd) {
    Write-Host "Error: Python 3.12+ not found. Install Python 3.12 or newer first." -ForegroundColor Red
    Write-Host "Tip: disable the Windows Store python alias or put your real Python before WindowsApps on PATH." -ForegroundColor Yellow
    exit 127
}

if ($Force -and (Test-Path $venvDir)) {
    Remove-Item -Recurse -Force $venvDir
}

$pythonExe = $pyCmd[0]
$pythonPreArgs = @()
if ($pyCmd.Length -gt 1) {
    $pythonPreArgs = $pyCmd[1..($pyCmd.Length - 1)]
}

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host " AI CLI Kit - Installer (Windows)" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "Project:   $projectRoot"
Write-Host "Python:    $pythonExe $($pythonPreArgs -join ' ')"
Write-Host "Venv:      $venvDir"
if ($Editable) {
    Write-Host "Mode:      editable"
} else {
    Write-Host "Mode:      standard"
}

# Drop --system-site-packages: it lets a stale system setuptools<61 leak in
# and silently build an empty UNKNOWN-0.0.0 wheel because the PEP 621 [project]
# table goes unread. Our package has zero runtime deps so an isolated venv
# is strictly safer.
& $pythonExe @pythonPreArgs -m venv $venvDir
Assert-LastExitCode "python -m venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Error: failed to create local venv at $venvDir" -ForegroundColor Red
    exit 1
}

# Force-upgrade pip / setuptools / wheel inside the venv so PEP 621 metadata
# is read correctly and console scripts (aik / cst / cc-clean) actually land.
Write-Host "Upgrading pip / setuptools / wheel in the local venv..."
& $venvPython -m pip install --quiet --upgrade pip setuptools wheel
Assert-LastExitCode "pip install --upgrade pip setuptools wheel"

if ($Editable) {
    & $venvPython -m pip install --no-deps -e $projectRoot
} else {
    & $venvPython -m pip install --no-deps $projectRoot
}
Assert-LastExitCode "pip install"

# -NoScripts: caller wants a clean install with no PATH-visible commands.
# pip can't be told to skip individual entry points cleanly, so we install
# normally then remove the four console scripts from venv\Scripts. The
# package is still importable via ``python -m ai_cli_kit``.
if ($NoScripts) {
    foreach ($name in @("aik", "cst", "codex-session-toolkit", "cc-clean")) {
        foreach ($ext in @(".exe", ".cmd", "-script.py", "")) {
            $candidate = Join-Path $venvDir "Scripts\$name$ext"
            if (Test-Path $candidate) {
                Remove-Item -Force $candidate
            }
        }
    }
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host " Install complete." -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
if ($NoScripts) {
    Write-Host "已按 -NoScripts 模式安装，未在 venv\Scripts 注册任何命令。"
    Write-Host "推荐运行方式（任选一种，都不会污染系统 PATH）："
    Write-Host "  .\aik.cmd                              # 项目内 launcher"
    Write-Host "  $venvPython -m ai_cli_kit              # python -m 直跑"
} else {
    Write-Host "推荐：在项目目录里直接运行 launcher"
    Write-Host "  .\aik.cmd                              # 顶层菜单（进 Codex / Claude）"
    Write-Host "  .\codex-session-toolkit.cmd"
    Write-Host "  .\cc-clean.cmd"
    Write-Host ""
    Write-Host "也可以直接用 python，无需注册 PATH 命令："
    Write-Host "  $venvPython -m ai_cli_kit              # 等价于 .\aik.cmd"
    Write-Host "  $venvPython -m ai_cli_kit.codex"
    Write-Host "  $venvPython -m ai_cli_kit.claude"
    Write-Host ""
    Write-Host "若想全局裸命令 'aik'，把 venv Scripts 加入 PATH："
    Write-Host "  `$env:Path = `"$venvDir\Scripts;`" + `$env:Path"
}
Write-Host ""
Write-Host "查看版本：.\aik.cmd --version"
