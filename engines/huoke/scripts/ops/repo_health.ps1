# OpenClaw repository health check
# Pre-commit / pre-push sanity sweep:
#   - git status (which files dirty / untracked)
#   - branch vs main (commit ahead/behind)
#   - 4 runtime config files (intentionally not committed)
#   - config/launch.env (local override) presence
#   - large dirs that may need pruning (logs/_archive, debug, temp)
#   - vendor/ integrity (DLL count)
#
# Usage:
#   repo_health.bat
#   repo_health.bat -Verbose

param(
    [switch]$Verbose,
    [switch]$Fetch,
    [switch]$Json
)

# JSON-only mode: machine-readable output for CI / Prometheus / cron consumption.
if ($Json) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    Set-Location $ProjectRoot

    if ($Fetch) {
        & git fetch origin main 2>&1 | Out-Null
    }

    $r = [ordered]@{
        timestamp = (Get-Date).ToString('o')
        verdict = 0
        verdict_label = $null
        branch = $null
        ahead_local_main = 0
        behind_local_main = 0
        local_main_behind_origin = $null
        on_main_with_changes = $false
        modified = 0
        untracked = 0
        staged = 0
        runtime_configs = @{}
        launch_env_exists = $false
        launch_env_size = 0
        disk_hotspots_mb = @{}
        vendor_dll_count = 0
        vendor_has_scrcpy_server = $false
    }
    function _bump { param([int]$lvl) if ($lvl -gt $r.verdict) { $r.verdict = $lvl } }

    # Branch
    $r.branch = (& git branch --show-current 2>$null).Trim()
    if (-not $r.branch) { _bump 1 }

    if ($r.branch -and $r.branch -ne 'main') {
        $r.ahead_local_main  = [int](& git rev-list --count "main..HEAD" 2>$null)
        $r.behind_local_main = [int](& git rev-list --count "HEAD..main" 2>$null)
        if ($r.behind_local_main -gt 0) { _bump 1 }
    }
    $hasOriginMain = (& git rev-parse --verify --quiet "refs/remotes/origin/main") 2>$null
    if ($hasOriginMain) {
        $r.local_main_behind_origin = [int](& git rev-list --count "main..origin/main" 2>$null)
        if ($r.local_main_behind_origin -gt 0) { _bump 1 }
    }

    # Status
    $dirty = & git status --porcelain 2>$null
    $r.modified = ($dirty | Where-Object { $_ -match '^\s*M' }).Count
    $r.untracked = ($dirty | Where-Object { $_ -match '^\?\?' }).Count
    $r.staged = ($dirty | Where-Object { $_ -match '^[AMRD]' -and $_ -notmatch '^\?\?' }).Count

    if ($r.branch -eq 'main' -and ($r.modified -gt 0 -or $r.staged -gt 0)) {
        $r.on_main_with_changes = $true
        _bump 1
    }

    # Runtime configs
    $runtimeConfigs = @(
        'config/cluster_state.json',
        'config/device_aliases.json',
        'config/device_registry.json',
        'config/notify_config.json'
    )
    foreach ($cfg in $runtimeConfigs) {
        $isDirty = [bool]($dirty | Where-Object { $_ -match [regex]::Escape($cfg) })
        $r.runtime_configs[$cfg] = if ($isDirty) { 'dirty' } else { 'clean' }
    }

    # launch.env
    $envFile = Join-Path $ProjectRoot "config\launch.env"
    if (Test-Path $envFile) {
        $r.launch_env_exists = $true
        $r.launch_env_size = (Get-Item $envFile).Length
    } else {
        _bump 1
    }

    # Disk hotspots
    $hotspots = @('logs', 'logs\_archive', 'data', 'data\_archive', 'debug', 'temp', 'apk_repo')
    foreach ($h in $hotspots) {
        $p = Join-Path $ProjectRoot $h
        if (Test-Path $p) {
            $size = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                     Measure-Object -Property Length -Sum).Sum
            $r.disk_hotspots_mb[$h] = [Math]::Round(($size / 1MB), 1)
            if ($r.disk_hotspots_mb[$h] -gt 500) { _bump 1 }
        }
    }

    # Vendor
    $vendor = Join-Path $ProjectRoot "vendor"
    if (Test-Path $vendor) {
        $r.vendor_dll_count = (Get-ChildItem $vendor -Filter "*.dll").Count
        $r.vendor_has_scrcpy_server = Test-Path (Join-Path $vendor "scrcpy-server")
        if (-not $r.vendor_has_scrcpy_server) { _bump 1 }
    } else {
        _bump 1
    }

    # YYY: sibling协同压力指标 - count merged-PR commits on origin/main in last 24h
    # (subject contains "(#NNN)"). Doesn't need network if origin/main was fetched.
    $recentSquash = 0
    try {
        $recentLog = & git log origin/main --pretty=%s --since='24 hours ago' 2>$null
        $recentSquash = @($recentLog | Where-Object { $_ -match '\(#\d+\)' }).Count
    } catch { }
    $r.sibling_prs_24h = $recentSquash

    # YYY: also expose last fetch age in minutes (helps consumers know data freshness)
    $fetchHead = Join-Path $ProjectRoot ".git\FETCH_HEAD"
    if (Test-Path $fetchHead) {
        $r.last_fetch_age_min = [int]([Math]::Round(((Get-Date) - (Get-Item $fetchHead).LastWriteTime).TotalMinutes, 0))
    } else {
        $r.last_fetch_age_min = $null
    }

    # BBC: feat-* branches count (cheap). For obsolete count run cleanup_branches.bat -Json.
    # Why not compute obsolete here: git cherry on 50+ branches is slow (~1-2s each).
    $featBranches = & git for-each-ref --format='%(refname:short)' "refs/heads/feat-*" 2>$null
    $r.feat_branches_count = @($featBranches).Count
    $r.feat_branches_hint = "run 'cleanup_branches.bat -Json' for obsolete count + per-branch detail"

    # HHJ: pre-commit hook status (signal CI / monitor whether hook is installed)
    $hookFile = Join-Path $ProjectRoot ".git\hooks\pre-commit"
    if (Test-Path $hookFile) {
        $hookContent = Get-Content $hookFile -Raw -ErrorAction SilentlyContinue
        if ($hookContent -match 'OpenClaw|pre_commit\.ps1') {
            $r.pre_commit_hook_installed = $true
            $r.pre_commit_hook_kind = 'openclaw'
        } else {
            $r.pre_commit_hook_installed = $true
            $r.pre_commit_hook_kind = 'other'
        }
    } else {
        $r.pre_commit_hook_installed = $false
        $r.pre_commit_hook_kind = $null
    }

    $r.verdict_label = switch ($r.verdict) { 0 {'HEALTHY'} 1 {'NEEDS ATTENTION'} default {'UNKNOWN'} }
    $r | ConvertTo-Json -Depth 4
    exit $r.verdict
}

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$exitCode = 0
function Bump-Exit { param([int]$lvl) if ($lvl -gt $script:exitCode) { $script:exitCode = $lvl } }

Write-Host "==========================================="
Write-Host "  OpenClaw Repo Health"
Write-Host "==========================================="

# ---- [1/7] Branch + commits ahead/behind main / origin/main ----
Write-Host ""
Write-Host "[1/7] Branch + commits"

# -Fetch: pull latest origin refs first (5-10s network call, opt-in)
if ($Fetch) {
    Write-Host "   [INFO] git fetch origin main ..." -ForegroundColor DarkGray
    & git fetch origin main 2>&1 | Out-Null
}

$branch = (& git branch --show-current 2>$null).Trim()
if (-not $branch) {
    Write-Host "   [WARN] not on a branch (detached HEAD?)" -ForegroundColor Yellow
    Bump-Exit 1
} else {
    Write-Host ("   [OK]   branch: {0}" -f $branch) -ForegroundColor Green
    if ($branch -ne 'main') {
        $ahead  = ([int](& git rev-list --count "main..HEAD" 2>$null))
        $behind = ([int](& git rev-list --count "HEAD..main" 2>$null))
        if ($ahead -gt 0)  { Write-Host ("          {0} commit(s) ahead of main"  -f $ahead) -ForegroundColor Cyan }
        if ($behind -gt 0) {
            Write-Host ("          {0} commit(s) behind main (sync_with_main.bat -Rebase recommended)" -f $behind) -ForegroundColor Yellow
            Bump-Exit 1
        }
    }
    # Origin tracking: catch sibling-merged-PR cases the local main does not yet reflect
    $hasOriginMain = (& git rev-parse --verify --quiet "refs/remotes/origin/main") 2>$null
    if ($hasOriginMain) {
        $localBehindOrigin = ([int](& git rev-list --count "main..origin/main" 2>$null))
        if ($localBehindOrigin -gt 0) {
            $hint = if ($Fetch) { 'just fetched' } else { 'last fetch may be stale; rerun with -Fetch' }
            Write-Host ("          local main is {0} commit(s) behind origin/main ({1})" -f $localBehindOrigin, $hint) -ForegroundColor Yellow
            Write-Host "                 (sibling Claude / collaborator merged a PR — see RUNBOOK 4fbee97 incident)" -ForegroundColor DarkYellow
            Bump-Exit 1
        }

        # HHH: Detect commits on this branch whose patch-id is already in origin/main
        # (squash-merged). If all squashed -> branch is obsolete.
        if ($branch -ne 'main') {
            $cherryLines = & git cherry origin/main HEAD 2>$null
            $freshCnt = @($cherryLines | Where-Object { $_ -match '^\+\s+' }).Count
            $squashCnt = @($cherryLines | Where-Object { $_ -match '^\-\s+' }).Count
            if ($squashCnt -gt 0) {
                if ($freshCnt -eq 0) {
                    Write-Host ("          [INFO] all {0} commit(s) of this branch are already in origin/main (squash-merged)" -f $squashCnt) -ForegroundColor Cyan
                    Write-Host ("                 branch obsolete — safe to delete: git branch -D {0}" -f $branch) -ForegroundColor DarkCyan
                    Write-Host "                 or batch: cleanup_branches.bat -Apply" -ForegroundColor DarkCyan
                } else {
                    Write-Host ("          [INFO] {0} commit(s) on this branch already in origin/main (squashed); {1} fresh" -f $squashCnt, $freshCnt) -ForegroundColor Cyan
                    Write-Host "                 to resync onto origin/main: sync_with_main.bat -CherryPick" -ForegroundColor DarkCyan
                }
            }
        }
    }
}

# Branch sanity (added 2026-04-26 after 4fbee97 incident):
# CLAUDE.md says no direct commits to main. Detect "main + dirty/staged" early.
$_dirty_pre = & git status --porcelain 2>$null
$_mod_pre = ($_dirty_pre | Where-Object { $_ -match '^\s*M' }).Count
$_staged_pre = ($_dirty_pre | Where-Object { $_ -match '^[AMRD]' -and $_ -notmatch '^\?\?' }).Count
if ($branch -eq 'main' -and ($_mod_pre -gt 0 -or $_staged_pre -gt 0)) {
    Write-Host "   [WARN] on main with uncommitted changes — CLAUDE.md says no direct commits to main." -ForegroundColor Yellow
    Write-Host "          Fix: branch_create.bat   (one-shot helper)" -ForegroundColor DarkYellow
    Bump-Exit 1
}

# Branch sanity (added 2026-04-26 after 4fbee97 incident):
# CLAUDE.md says no direct commits to main. Detect "main + dirty/staged" early.
$_dirty_pre = & git status --porcelain 2>$null
$_mod_pre = ($_dirty_pre | Where-Object { $_ -match '^\s*M' }).Count
$_staged_pre = ($_dirty_pre | Where-Object { $_ -match '^[AMRD]' -and $_ -notmatch '^\?\?' }).Count

# ---- [2/7] Working tree dirty ----
Write-Host ""
Write-Host "[2/7] Working tree status"
$dirty = & git status --porcelain 2>$null
$modCount = ($dirty | Where-Object { $_ -match '^\s*M' }).Count
$untrackedCount = ($dirty | Where-Object { $_ -match '^\?\?' }).Count
$stagedCount = ($dirty | Where-Object { $_ -match '^[AMR]' -and $_ -notmatch '^\?\?' }).Count

if ($modCount -eq 0 -and $untrackedCount -eq 0 -and $stagedCount -eq 0) {
    Write-Host "   [OK]   clean tree" -ForegroundColor Green
} else {
    Write-Host ("   [INFO] modified={0} untracked={1} staged={2}" -f $modCount, $untrackedCount, $stagedCount) -ForegroundColor Cyan
    if ($Verbose) {
        $dirty | Select-Object -First 20 | ForEach-Object { Write-Host ("       " + $_) -ForegroundColor DarkCyan }
    }
}

# ---- [3/7] 4 runtime config files (expected dirty after server start) ----
Write-Host ""
Write-Host "[3/7] Runtime config files (expected dirty after server start)"
$runtimeConfigs = @(
    'config/cluster_state.json',
    'config/device_aliases.json',
    'config/device_registry.json',
    'config/notify_config.json'
)
foreach ($cfg in $runtimeConfigs) {
    $isDirty = ($dirty | Where-Object { $_ -match [regex]::Escape($cfg) })
    if ($isDirty) {
        Write-Host ("   [INFO] dirty: $cfg") -ForegroundColor Cyan
    } else {
        Write-Host ("   [OK]   clean: $cfg") -ForegroundColor DarkGray
    }
}
Write-Host "       (these are runtime state; do NOT commit unless intentional)" -ForegroundColor DarkGray

# ---- [4/7] launch.env ----
Write-Host ""
Write-Host "[4/7] config/launch.env"
$envFile = Join-Path $ProjectRoot "config\launch.env"
$envExample = Join-Path $ProjectRoot "config\launch.env.example"
if (Test-Path $envFile) {
    $envSize = (Get-Item $envFile).Length
    Write-Host ("   [OK]   exists ({0} bytes)" -f $envSize) -ForegroundColor Green
} else {
    if (Test-Path $envExample) {
        Write-Host "   [WARN] not found. Run: cp config/launch.env.example config/launch.env" -ForegroundColor Yellow
        Bump-Exit 1
    } else {
        Write-Host "   [WARN] launch.env missing AND launch.env.example missing" -ForegroundColor Yellow
        Bump-Exit 1
    }
}

# ---- [5/7] Disk usage hotspots ----
Write-Host ""
Write-Host "[5/7] Disk usage hotspots"
$hotspots = @(
    'logs',
    'logs\_archive',
    'data',
    'data\_archive',
    'debug',
    'temp',
    'apk_repo'
)
foreach ($h in $hotspots) {
    $p = Join-Path $ProjectRoot $h
    if (Test-Path $p) {
        $size = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object -Property Length -Sum).Sum
        $sizeMB = [Math]::Round(($size / 1MB), 1)
        $color = if ($sizeMB -gt 500) { 'Yellow' } elseif ($sizeMB -gt 100) { 'Cyan' } else { 'Green' }
        $tag = if ($sizeMB -gt 500) { '[WARN]' } elseif ($sizeMB -gt 100) { '[INFO]' } else { '[OK]  ' }
        Write-Host ("   {0} {1,8} MB   {2}" -f $tag, $sizeMB, $h) -ForegroundColor $color
        if ($sizeMB -gt 500) {
            Bump-Exit 1
            Write-Host "          consider: cleanup_logs.bat -Days 30" -ForegroundColor DarkYellow
        }
    }
}

# ---- [6/7] vendor/ integrity ----
Write-Host ""
Write-Host "[6/7] vendor/ integrity"
$vendor = Join-Path $ProjectRoot "vendor"
if (Test-Path $vendor) {
    $dllCount = (Get-ChildItem $vendor -Filter "*.dll").Count
    $hasScrcpy = Test-Path (Join-Path $vendor "scrcpy-server")
    Write-Host ("   [OK]   {0} DLL(s)  scrcpy-server: {1}" -f $dllCount, $(if ($hasScrcpy) { 'yes' } else { 'NO' })) -ForegroundColor Green
    if (-not $hasScrcpy) {
        Write-Host "   [WARN] scrcpy-server missing under vendor/" -ForegroundColor Yellow
        Bump-Exit 1
    }
} else {
    Write-Host "   [WARN] vendor/ directory missing" -ForegroundColor Yellow
    Bump-Exit 1
}

# ---- [7/7] P2-⑥ Sibling collision risk ----
# 同机多 Claude session 共享 worktree 的实际事故信号. 不阻塞健康分但显眼提示.
# 加这一节的根因: PR #142/#144 实施过程中两次被 sibling 切分支或 git commit -a
# 把我 staged 文件卷入它的 commit (6f80638 OPT-FP1 messenger 误打包事故).
Write-Host ""
Write-Host "[7/7] Sibling collision risk (P2-⑥)"
$collisionRisks = 0

# 7.1 stash 列表条数 > 3 = 工作流压力信号
$stashCount = @(& git stash list 2>$null).Count
if ($stashCount -gt 3) {
    Write-Host ("   [WARN] git stash list 有 {0} 条 — 累积太多, 易丢失上下文" -f $stashCount) -ForegroundColor Yellow
    Write-Host "          先 git stash list 看清, 不需要的 git stash drop" -ForegroundColor DarkYellow
    $collisionRisks++
} elseif ($stashCount -gt 0) {
    Write-Host ("   [INFO] {0} stash entr(y/ies) — OK" -f $stashCount) -ForegroundColor DarkGray
} else {
    Write-Host "   [OK]   no stash entries" -ForegroundColor DarkGreen
}

# 7.2 跨 src/ 模块 dirty 警告 (modified + staged + untracked 合并)
$allDirty = & git status --porcelain 2>$null
$dirtySrcDirs = @($allDirty | Where-Object { $_ -match 'src/' } |
    ForEach-Object {
        $f = ($_ -replace '^.{3}', '')  # strip status prefix
        if ($f -match '^src/[^/]+/') { $matches[0].TrimEnd('/') } else { $null }
    } |
    Where-Object { $_ } |
    Sort-Object -Unique)
if ($dirtySrcDirs.Count -ge 2) {
    Write-Host ("   [WARN] dirty 跨 {0} 个 src/ 子模块 — sibling 可能也在动这个 worktree" -f $dirtySrcDirs.Count) -ForegroundColor Yellow
    foreach ($d in $dirtySrcDirs) {
        Write-Host ("          {0}/" -f $d) -ForegroundColor DarkYellow
    }
    Write-Host "          commit 前先 git status --short 核对所有权" -ForegroundColor DarkYellow
    $collisionRisks++
}

# 7.3 最近 1h 内 sibling commit (signal 多 session 同时活跃)
$recent1hCount = @(& git log --since='1 hour ago' --oneline 2>$null).Count
if ($recent1hCount -gt 3) {
    Write-Host ("   [WARN] 最近 1h 有 {0} 个 commit — sibling 活跃, 切分支/commit 谨慎" -f $recent1hCount) -ForegroundColor Yellow
    & git log --since='1 hour ago' --oneline -5 2>$null | ForEach-Object {
        Write-Host ("          {0}" -f $_) -ForegroundColor DarkYellow
    }
    $collisionRisks++
} else {
    Write-Host ("   [OK]   {0} commit(s) in last 1h" -f $recent1hCount) -ForegroundColor DarkGreen
}

# 7.4 branch 命名 vs dirty 文件位置启发式
if ($branch -match '^feat-ops-' -and ($dirtySrcDirs -contains 'src/app_automation')) {
    Write-Host "   [WARN] branch=$branch 但 src/app_automation 有 dirty — 这分支期望只动 src/host/" -ForegroundColor Yellow
    Write-Host "          建议: git stash 那部分给 sibling, 或先切到对方分支再 commit" -ForegroundColor DarkYellow
    $collisionRisks++
}

if ($collisionRisks -gt 0) {
    Write-Host ("       collision risk score: {0} — 建议跑 safe_commit.bat 替代 git commit -a" -f $collisionRisks) -ForegroundColor DarkYellow
    Bump-Exit 1
} else {
    Write-Host "       collision risk: low OK" -ForegroundColor DarkGreen
}

# ---- Summary ----
Write-Host ""
Write-Host "==========================================="
$verdict = switch ($exitCode) { 0 {'HEALTHY'} 1 {'NEEDS ATTENTION'} default {'UNKNOWN'} }
$color   = switch ($exitCode) { 0 {'Green'  } 1 {'Yellow'         } default {'White'  } }
Write-Host ("  >> Repo verdict: [{0}] {1}" -f $exitCode, $verdict) -ForegroundColor $color
Write-Host "==========================================="

exit $exitCode
