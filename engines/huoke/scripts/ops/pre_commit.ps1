# OpenClaw pre-commit hook
# Installed via install_hooks.bat -> .git/hooks/pre-commit (calls this).
# Blocks commit on common mistakes:
#   1. Committing to main directly (CLAUDE.md "no direct commits to main").
#   2. Local main is behind origin/main (sibling Claude likely merged a PR).
#   3. Trying to commit runtime config files (config/cluster_state.json etc).
#
# Bypass (rare, intentional): commit with --no-verify

$ErrorActionPreference = 'Continue'

# Repository root (this script lives at scripts/ops/pre_commit.ps1)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$err = 0
function Block { param([string]$msg) Write-Host "[BLOCK] $msg" -ForegroundColor Red; $script:err = 1 }
function Note  { param([string]$msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }

# ---- 1. Refuse direct commits to main ----
$branch = (& git branch --show-current 2>$null).Trim()
if ($branch -eq 'main') {
    Block "Committing to main is forbidden by CLAUDE.md."
    Write-Host "        Fix:  branch_create.bat   (one-shot helper)" -ForegroundColor DarkRed
    Write-Host "        Bypass (rare): git commit --no-verify" -ForegroundColor DarkGray
}

# ---- 2. Refuse staged runtime config files ----
# 区分: 真 runtime (heartbeat / 通知 history / queue DB / local override)
#   vs source-of-truth (设备注册 — 首次接入写入后稳定, 历史有 commit 记录).
# 2026-05-04 修: device_aliases/device_registry 从黑名单移到豁免, 让 ops
# 能正常持久化新设备注册 (4 台主控-05/07/08/09 扩容场景).
$staged = & git diff --cached --name-only 2>$null
$runtimeConfigs = @(
    'config/cluster_state.json',           # 心跳 + IP, 服务启动 rewrite
    'config/notify_config.json',           # 通知 history 自动追加 + 滚动
    'config/launch.env',                   # local override, not in git
    'config/central_push_queue.db',
    'config/central_push_queue.db-shm',
    'config/central_push_queue.db-wal'
)
foreach ($f in $staged) {
    if ($runtimeConfigs -contains $f.Trim()) {
        Block "Staged runtime file: $f (these are written by server.py at runtime)."
        Write-Host "        Unstage: git restore --staged $f" -ForegroundColor DarkRed
    }
}

# ---- 3. Warn (not block) if local main is behind origin/main ----
# Don't block: fetching every commit slows pre-commit too much.
# Soft warning so user knows to sync_with_main.bat -Rebase eventually.
$hasOriginMain = (& git rev-parse --verify --quiet "refs/remotes/origin/main") 2>$null
if ($hasOriginMain) {
    $behind = [int](& git rev-list --count "main..origin/main" 2>$null)
    if ($behind -gt 0) {
        Note "local main is $behind commit(s) behind origin/main (cached, not fetched)."
        Note "  consider: sync_with_main.bat -Rebase  (after this commit)"
    }
}

# ---- P2-⑥ 新增检查: sibling collision 防护 ----
# 加这些检查的根因是 PR #142 / #144 实施时被 sibling Claude 用 git commit -a
# 把 staged 文件打包进它的 commit 的事故 (commit 6f80638).
# 全部 warning 不 block, 让用户能立刻看到干预但不卡工作流.

function Warn { param([string]$msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }

# 3.1 跨多个独立 src/ 模块 staged: 提示"打包了多个 feature?"
$stagedDirs = @($staged | Where-Object { $_ -match '^src/' } |
    ForEach-Object { ($_ -split '/')[0..1] -join '/' } |
    Sort-Object -Unique)
if ($stagedDirs.Count -ge 2) {
    Warn "staged 跨 $($stagedDirs.Count) 个 src/ 子模块 — 是不是 git commit -a 误打包了别人的工作?"
    foreach ($d in $stagedDirs) { Write-Host "         $d/" -ForegroundColor DarkYellow }
    Write-Host "        若意外打包: git restore --staged <他人文件>" -ForegroundColor DarkGray
}

# 3.2 ops/* 分支但 staged 含 src/app_automation/facebook.py
if ($branch -match '^feat-ops-' -and ($staged -match 'src/app_automation/facebook\.py')) {
    Warn "feat-ops-* 分支 staged 了 facebook.py — 这通常是 sibling B-worker 的活."
    Write-Host "        若误打包: git restore --staged src/app_automation/facebook.py" -ForegroundColor DarkGray
}

# 3.3 stash 列表 > 5 警告
$stashCount = @(& git stash list 2>$null).Count
if ($stashCount -gt 5) {
    Warn "git stash list 有 $stashCount 条 — 累积太多, 易丢失上下文. 建议 git stash drop 旧的."
}

# 3.4 untracked 文件 > 8: 漏 git add 的提示
$untrackedCount = @(& git status --porcelain 2>$null | Where-Object { $_ -match '^\?\?' }).Count
if ($untrackedCount -gt 8) {
    Warn "$untrackedCount 个 untracked 文件未 add — 检查是否漏掉本次 commit 应包含的新文件."
}

# ---- 4. Print summary ----
if ($err -ne 0) {
    Write-Host ""
    Write-Host "[pre-commit] commit BLOCKED (use --no-verify to bypass if intentional)" -ForegroundColor Red
    exit 1
} else {
    Write-Host "[pre-commit] OK" -ForegroundColor DarkGreen
    exit 0
}
