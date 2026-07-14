# OpenClaw git hooks installer
# Wires .git/hooks/pre-commit -> scripts/ops/pre_commit.ps1
# Idempotent: re-running is safe.
#
# Usage:
#   install_hooks.bat              # install
#   install_hooks.bat -Uninstall   # remove

param([switch]$Uninstall, [switch]$Force)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$hookDir = Join-Path $ProjectRoot ".git\hooks"
$hookFile = Join-Path $hookDir "pre-commit"

if (-not (Test-Path $hookDir)) {
    Write-Host "[ERROR] $hookDir not found (not a git repo?)" -ForegroundColor Red
    exit 1
}

Write-Host "==========================================="
Write-Host "  OpenClaw git hooks"
Write-Host "==========================================="
Write-Host ""

if ($Uninstall) {
    if (Test-Path $hookFile) {
        Remove-Item $hookFile -Force
        Write-Host "[OK] Removed $hookFile" -ForegroundColor Green
    } else {
        Write-Host "[INFO] No pre-commit hook installed." -ForegroundColor DarkGray
    }
    exit 0
}

# Install path
if ((Test-Path $hookFile) -and (-not $Force)) {
    Write-Host "[INFO] pre-commit hook already exists at $hookFile" -ForegroundColor Cyan
    Write-Host "       Use -Force to overwrite, or -Uninstall to remove." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Existing content (first 5 lines):" -ForegroundColor DarkGray
    Get-Content $hookFile -TotalCount 5 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
    exit 0
}

# Write a small bash shim that invokes our PowerShell script.
# Git on Windows runs hooks via Git Bash, so we need a bash file calling powershell.
$hookContent = @"
#!/bin/sh
# OpenClaw pre-commit hook (installed by scripts/ops/install_hooks.ps1)
# Runs scripts/ops/pre_commit.ps1 via PowerShell.
# Bypass once: git commit --no-verify

set -e
PROJECT_ROOT="`$(git rev-parse --show-toplevel)"
PS_SCRIPT="`$PROJECT_ROOT/scripts/ops/pre_commit.ps1"

if [ ! -f "`$PS_SCRIPT" ]; then
    echo "[pre-commit] PowerShell script missing: `$PS_SCRIPT"
    exit 0   # don't block if script gone (e.g. branch without it)
fi

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "`$PS_SCRIPT"
exit `$?
"@

$hookContent | Set-Content -Path $hookFile -Encoding ASCII -NoNewline

Write-Host ("[OK] Installed pre-commit hook at: {0}" -f $hookFile) -ForegroundColor Green
Write-Host ""
Write-Host "What it does:" -ForegroundColor DarkCyan
Write-Host "  1. Block commits on main (branch_create.bat to fix)"
Write-Host "  2. Block commits of runtime config files (cluster_state.json etc)"
Write-Host "  3. Warn if local main is behind origin/main"
Write-Host ""
Write-Host "Bypass (one commit only): git commit --no-verify" -ForegroundColor DarkGray
Write-Host "Uninstall: install_hooks.bat -Uninstall" -ForegroundColor DarkGray
