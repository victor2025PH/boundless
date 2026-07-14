# OpenClaw start script
# Usage: start.bat  (or directly: powershell -File start.ps1)
# Detailed runbook: docs/SYSTEM_RUNBOOK.md
#
# Args:
#   -NoBranchCheck   Skip the branch sanity / dirty-config / fetch-age info
#                    (useful for CI / scripted starts where they're noise)

param([switch]$NoBranchCheck)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot
. "$PSScriptRoot\_lib.ps1"

Write-Host "==========================================="
Write-Host "  OpenClaw - starting"
Write-Host "==========================================="
Write-Host ""

# ---- 0. Load config/launch.env (KEY=VAL) into process env ----
$envFile = Join-Path $ProjectRoot "config\launch.env"
if (Test-Path $envFile) {
    Write-Host "Loading config/launch.env..."
    $loaded = @()
    Get-Content $envFile | ForEach-Object {
        $line = $_
        if ($line -match '^\s*#' -or $line -match '^\s*$') { return }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
            $loaded += "$key=$val"
        }
    }
    if ($loaded.Count -gt 0) {
        Write-Host ("   [OK] {0} var(s): {1}" -f $loaded.Count, ($loaded -join ', ')) -ForegroundColor DarkGray
    }
} else {
    Write-Host "[INFO] config/launch.env not found (defaults: port=18080, host=0.0.0.0)" -ForegroundColor DarkGray
    $exampleFile = Join-Path $ProjectRoot "config\launch.env.example"
    if (Test-Path $exampleFile) {
        Write-Host "       template available: cp config/launch.env.example config/launch.env" -ForegroundColor DarkGray
    }
}

# ---- 0b. Branch sanity + dirty config notice + last-fetch age ----
# XXX: warn if last 'git fetch' is stale (sibling Claude likely merged PRs).
# DDE: -NoBranchCheck skips this entire block (CI / scripted use)
if (-not $NoBranchCheck) {
try {
    $fetchHead = Join-Path $ProjectRoot ".git\FETCH_HEAD"
    if (Test-Path $fetchHead) {
        $lastFetch = (Get-Item $fetchHead).LastWriteTime
        $ageMin = [Math]::Round(((Get-Date) - $lastFetch).TotalMinutes, 0)
        if ($ageMin -gt 10) {
            Write-Host ""
            Write-Host ("[INFO] last 'git fetch' was {0} min ago." -f $ageMin) -ForegroundColor Cyan
            Write-Host "       sibling Claude may have merged PRs since. Run: sync_with_main.bat" -ForegroundColor DarkCyan
        }
    }
} catch { }

# Branch sanity (防 4fbee97-style 事故): warn if on main with dirty/staged.
try {
    $branch = (& git branch --show-current 2>$null).Trim()
    if ($branch) {
        if ($branch -eq 'main') {
            $dirtyAll = & git status --porcelain 2>$null
            $hasUncommitted = $dirtyAll | Where-Object { $_ -match '^[\sMARD?][MARD?]' -and $_ -notmatch 'config/(cluster_state|device_aliases|device_registry|notify_config|central_push)' }
            if ($hasUncommitted) {
                Write-Host ""
                Write-Host "[WARN] on main with uncommitted changes!" -ForegroundColor Yellow
                Write-Host "       CLAUDE.md says no direct commits to main." -ForegroundColor DarkYellow
                Write-Host "       Consider: git checkout -b feat-ops-`$(Get-Date -Format 'yyyy-MM-dd')" -ForegroundColor DarkYellow
                Write-Host ""
            }
            # P3 L2: sibling 协同护栏 — 检测未合并的 feat-* 分支
            try {
                $unmergedFeats = & git for-each-ref --format='%(refname:short)' 'refs/heads/feat-*' 2>$null | ForEach-Object {
                    $b = $_.Trim()
                    if ($b) {
                        $ah = (& git rev-list --count "main..$b" 2>$null)
                        if ([int]$ah -gt 0) {
                            [PSCustomObject]@{ Branch = $b; Ahead = [int]$ah }
                        }
                    }
                }
                if ($unmergedFeats) {
                    $count = @($unmergedFeats).Count
                    Write-Host ""
                    Write-Host "[WARN] Starting on 'main' but unmerged feature branch(es) exist:" -ForegroundColor Yellow
                    @($unmergedFeats) | Select-Object -First 5 | ForEach-Object {
                        Write-Host ("   - {0}  ({1} commits ahead of main)" -f $_.Branch, $_.Ahead) -ForegroundColor DarkYellow
                    }
                    if ($count -gt 5) {
                        Write-Host ("   ... and {0} more" -f ($count - 5)) -ForegroundColor DarkYellow
                    }
                    Write-Host "       sibling Claude / coworker may lose visibility of those work." -ForegroundColor DarkYellow
                    Write-Host "       Consider 'git checkout <feature-branch>' before restart, or merge first." -ForegroundColor DarkYellow
                    Write-Host ""
                }
            } catch { }
        } else {
            # P3 L1: 突出显示当前分支 + ahead-of-main 信息
            try {
                $aheadOfMain = (& git rev-list --count "main..HEAD" 2>$null)
                $aheadInt = [int]$aheadOfMain
                $aheadInfo = if ($aheadInt -gt 0) { "  ($aheadInt commits ahead of main)" } else { "" }
                Write-Host ""
                Write-Host "[BRANCH] Service running on: $branch$aheadInfo" -ForegroundColor Cyan
                Write-Host ""
            } catch {
                Write-Host "[BRANCH] $branch" -ForegroundColor Cyan
            }
        }
    }
    # Notice for dirty config files (will be loaded as-is)
    $dirtyConfig = & git status --porcelain config/ 2>$null | Where-Object { $_ -match '^\s*M\s' }
    if ($dirtyConfig) {
        Write-Host ""
        Write-Host "[INFO] Uncommitted changes in config/ (will be loaded as-is):" -ForegroundColor Cyan
        $dirtyConfig | Select-Object -First 5 | ForEach-Object {
            Write-Host ("   $_") -ForegroundColor DarkCyan
        }
        if ($dirtyConfig.Count -gt 5) {
            Write-Host ("   ... and {0} more" -f ($dirtyConfig.Count - 5)) -ForegroundColor DarkCyan
        }
        Write-Host "       (commit / stash if you want a clean baseline)" -ForegroundColor DarkCyan
    }
} catch {
    # git not available or not a repo - ignore
}
}  # end -NoBranchCheck guard

# ---- 1. Check existing processes ----
$existing = Get-OpenClawProcesses

if ($existing) {
    Write-Host ""
    Write-Host "[WARN] OpenClaw is already running:" -ForegroundColor Yellow
    foreach ($p in $existing) {
        $tag = Get-OpenClawLaunchTag $p.CommandLine
        Write-Host ("   PID={0}  {1}" -f $p.ProcessId, $tag)
    }
    Write-Host ""
    Write-Host "Run stop.bat first, or status.bat to check."
    Write-Host "Press Ctrl+C to cancel, Enter to continue (may cause port conflict)..." -ForegroundColor Yellow
    [void](Read-Host)
}

# ---- 2. Clean stale sentinel ----
$sentinel = Join-Path $ProjectRoot ".restart-required"
if (Test-Path $sentinel) {
    Write-Host "[INFO] Removed stale .restart-required sentinel"
    Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
}

# ---- 3. Launch service_wrapper ----
Write-Host ""
Write-Host "Launching service_wrapper.py..."
$wrapperScript = Join-Path $ProjectRoot "service_wrapper.py"
if (-not (Test-Path $wrapperScript)) {
    Write-Host "[ERROR] $wrapperScript not found" -ForegroundColor Red
    exit 1
}

# Show effective env that wrapper will inherit
$effPort = if ($env:OPENCLAW_PORT) { $env:OPENCLAW_PORT } else { '18080 (default)' }
$effHost = if ($env:OPENCLAW_HOST) { $env:OPENCLAW_HOST } else { '0.0.0.0 (default)' }
Write-Host ("   port=$effPort  host=$effHost") -ForegroundColor DarkGray

Start-Process -WindowStyle Minimized -WorkingDirectory $ProjectRoot `
    -FilePath "python" -ArgumentList "service_wrapper.py"

Write-Host ""
Write-Host "Waiting 8 seconds for server.py to come up..."
Start-Sleep -Seconds 8

# ---- 4. Auto-run status check (use -NoExit so we don't kill our caller) ----
Write-Host ""
& (Join-Path $PSScriptRoot "status.ps1") -NoExit
