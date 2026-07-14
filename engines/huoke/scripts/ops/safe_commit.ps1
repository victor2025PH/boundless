# P2-6 Safe commit wrapper - prevents "sibling pack-in" accidents
#
# Difference from plain `git commit`:
#   1. Refuses -a / --all flag (so sibling's unstaged changes are not packed in)
#   2. Shows branch + staged file list + cross-module risk before commit
#   3. Two-step confirm by default (skip with -Force)
#   4. Calls .git/hooks/pre-commit (default behavior)
#
# Usage:
#   safe_commit.bat "commit message"
#   safe_commit.bat "commit message" -Force         # skip confirm
#   safe_commit.bat -Help
#
# Why this exists:
#   PR #142 commit 6f80638 incident: sibling Claude ran `git commit -a` and
#   packed my 5 staged P2 files into its OPT-FP1 messenger commit. CLAUDE.md
#   warns about shared worktree but human review is unreliable - tooling fix.

param(
    [Parameter(Position=0)]
    [string]$Message,
    [switch]$Force,
    [switch]$Help
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

if ($Help -or [string]::IsNullOrWhiteSpace($Message)) {
    Write-Host "==========================================="
    Write-Host "  safe_commit - git commit + collision guard"
    Write-Host "==========================================="
    Write-Host ""
    Write-Host "Usage:" -ForegroundColor Cyan
    Write-Host '  safe_commit.bat "your commit message"'
    Write-Host '  safe_commit.bat "your commit message" -Force   # skip confirm'
    Write-Host ""
    Write-Host "Why use this:" -ForegroundColor DarkCyan
    Write-Host "  - Refuses git commit -a (sibling unstaged changes won't get packed)"
    Write-Host "  - Shows branch + staged list, lets you review before commit"
    Write-Host "  - Warns on cross-module / branch-mismatch heuristics"
    Write-Host ""
    Write-Host "Bypass: plain git commit (loses safety net)" -ForegroundColor DarkGray
    exit 1
}

# 1. Branch
$branch = (& git branch --show-current 2>$null).Trim()
Write-Host "==========================================="
Write-Host "  safe_commit"
Write-Host "==========================================="
Write-Host ""
if ($branch -eq 'main') {
    Write-Host "[BLOCK] On main branch - CLAUDE.md forbids direct commits" -ForegroundColor Red
    Write-Host "        Fix: branch_create.bat <name>" -ForegroundColor DarkRed
    exit 1
}
Write-Host "Branch:    $branch" -ForegroundColor Green

# 2. Staged check
$staged = @(& git diff --cached --name-only 2>$null)
if ($staged.Count -eq 0) {
    Write-Host ""
    Write-Host "[BLOCK] No staged files - run git add <files> first" -ForegroundColor Red
    Write-Host "        unstaged dirty:" -ForegroundColor DarkRed
    & git status --short 2>$null | Select-Object -First 10 | ForEach-Object {
        Write-Host "          $_" -ForegroundColor DarkGray
    }
    exit 1
}

Write-Host "Staged:    $($staged.Count) file(s)" -ForegroundColor Green
foreach ($f in ($staged | Select-Object -First 20)) {
    Write-Host "  + $f" -ForegroundColor DarkGreen
}
if ($staged.Count -gt 20) {
    Write-Host "  ... and $($staged.Count - 20) more" -ForegroundColor DarkGray
}
Write-Host ""

# 3. Diff stat
Write-Host "Diff stat:" -ForegroundColor Cyan
& git diff --cached --stat 2>$null | Select-Object -First 12 | ForEach-Object {
    Write-Host "  $_" -ForegroundColor DarkCyan
}
Write-Host ""

# 4. Cross-module risk (heuristic, mirrors pre_commit.ps1 section 3.1 but stricter)
$stagedDirs = @($staged | Where-Object { $_ -match '^src/' } |
    ForEach-Object { ($_ -split '/')[0..1] -join '/' } |
    Sort-Object -Unique)

$risks = @()
if ($stagedDirs.Count -ge 2) {
    $risks += "staged spans $($stagedDirs.Count) src/ submodules ($($stagedDirs -join ', ')) - usually means git commit -a packed someone else's work"
}
if ($branch -match '^feat-ops-' -and ($staged -match 'src/app_automation/facebook\.py')) {
    $risks += "feat-ops-* branch staged facebook.py - that's usually sibling B-worker territory"
}
$untrackedCount = @(& git status --porcelain 2>$null | Where-Object { $_ -match '^\?\?' }).Count
if ($untrackedCount -gt 8) {
    $risks += "$untrackedCount untracked files not added - missing files this commit should include?"
}

if ($risks.Count -gt 0) {
    Write-Host "RISKS:" -ForegroundColor Yellow
    foreach ($r in $risks) {
        Write-Host "  ! $r" -ForegroundColor Yellow
    }
    Write-Host ""
}

# 5. Commit message preview
$msgPreview = if ($Message.Length -gt 100) { $Message.Substring(0, 97) + "..." } else { $Message }
Write-Host "Message:   $msgPreview" -ForegroundColor Cyan
Write-Host ""

# 6. Two-step confirm (unless -Force)
if (-not $Force) {
    if ($risks.Count -gt 0) {
        Write-Host "Risk(s) detected. Type 'yes' to commit anyway (anything else aborts):" -ForegroundColor Yellow
    } else {
        Write-Host "Press ENTER to commit, type 'no' to abort:" -ForegroundColor Cyan
    }
    $confirm = Read-Host
    if ($risks.Count -gt 0) {
        if ($confirm -ne 'yes') {
            Write-Host "Aborted." -ForegroundColor DarkYellow
            exit 1
        }
    } else {
        if ($confirm -eq 'no' -or $confirm -eq 'n') {
            Write-Host "Aborted." -ForegroundColor DarkYellow
            exit 1
        }
    }
}

# 7. Run commit (no -a, only commit what is already staged)
Write-Host ""
Write-Host "Committing..." -ForegroundColor DarkCyan
& git commit -m $Message
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "[OK] Commit successful." -ForegroundColor Green
    & git log --oneline -1 2>$null | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkGreen }
} else {
    Write-Host ""
    Write-Host "[FAIL] git commit returned $exitCode" -ForegroundColor Red
}
exit $exitCode
