# OpenClaw sync-with-main helper
# sibling Claude 协同事故防呆: fetch + report ahead/behind, optionally rebase.
#
# Default behavior (safe):
#   - git fetch origin main
#   - report ahead/behind vs main and origin/main
#   - DO NOT modify branches (read-only)
#
# With -Rebase:
#   - additionally rebase current feat-* branch onto origin/main
#   - REFUSES to rebase main itself (use -Pull to update local main)
#   - if there are merge conflicts, leaves you in rebase state to resolve
#
# With -Pull (only on main):
#   - git pull --ff-only origin main (fast-forward update local main)
#
# Usage:
#   sync_with_main.bat              # report only (safe)
#   sync_with_main.bat -Rebase      # rebase current branch onto origin/main
#   sync_with_main.bat -Pull        # fast-forward local main from origin

param(
    [switch]$Rebase,
    [switch]$Pull,
    [switch]$AutoStash,   # BBB: pass --autostash to git rebase (auto stash+pop dirty)
    [switch]$CherryPick,  # FFF: cherry-pick fresh commits to new branch off origin/main
                          #      (use when main has squash-merged this branch's commits)
    [string]$BranchName = "",  # FFF: name for new cherry-pick branch (default: auto)
    [switch]$Auto,       # SSS: try -Rebase --AutoStash, on failure fallback to
                         #      -CherryPick --AutoStash. One-flag-fits-all.
    [switch]$DryRun,     # ZZZ: print what would happen, do not execute
    [switch]$Stats       # CCD: print sibling PR frequency on origin/main and exit
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

# Mutex: -Rebase / -Pull / -CherryPick / -Auto are exclusive (pick one apply mode)
$_modes = @($Rebase, $Pull, $CherryPick, $Auto) | Where-Object { $_ } | Measure-Object | Select-Object -ExpandProperty Count
if ($_modes -gt 1) {
    Write-Host "[ERROR] -Rebase / -Pull / -CherryPick / -Auto are mutually exclusive (pick one)." -ForegroundColor Red
    exit 2
}

# -Auto implies AutoStash (we want hands-off mode)
if ($Auto) { $AutoStash = $true }

# CCD: -Stats — print sibling PR frequency from origin/main commit log and exit.
# This is read-only, useful for sensing collaboration intensity before deciding
# whether to start a long task (high freq -> shorter commit cycles).
if ($Stats) {
    Write-Host "==========================================="
    Write-Host "  Sibling PR frequency on origin/main"
    Write-Host "==========================================="
    Write-Host ""

    # Need a recent fetch to get accurate origin/main; warn if stale
    $fetchHead = Join-Path $ProjectRoot ".git\FETCH_HEAD"
    if (Test-Path $fetchHead) {
        $ageMin = [int]([Math]::Round(((Get-Date) - (Get-Item $fetchHead).LastWriteTime).TotalMinutes, 0))
        if ($ageMin -gt 30) {
            Write-Host ("[WARN] last fetch was {0} min ago. Run sync_with_main.bat (no flag) first." -f $ageMin) -ForegroundColor Yellow
            Write-Host ""
        }
    }

    $windows = @(
        @{ label = 'last  1h '; since = '1 hour ago'  },
        @{ label = 'last  6h '; since = '6 hours ago' },
        @{ label = 'last 24h '; since = '24 hours ago'},
        @{ label = 'last 48h '; since = '48 hours ago'},
        @{ label = 'last  7d '; since = '7 days ago'  },
        @{ label = 'last 30d '; since = '30 days ago' }
    )
    foreach ($w in $windows) {
        # PS 5.1 native-exe arg quirk: --since=$x with hashtable expansion can split
        # on spaces. Build args array + splat so each element is one git argument.
        $gitArgs = @('log', 'origin/main', '--pretty=%s', "--since=$($w.since)")
        $log = & git @gitArgs 2>$null
        $count = @($log | Where-Object { $_ -match '\(#\d+\)' }).Count
        Write-Host ("   {0}  : {1,4} squash-merged PR(s)" -f $w.label, $count)
    }

    # GGH: ASCII bar chart of last 7 days, one bar per day (Mon..Sun pattern).
    Write-Host ""
    Write-Host "   Daily distribution (last 7 days):"
    $dailyData = @()
    for ($d = 6; $d -ge 0; $d--) {
        # day = $d days ago (covers that day 00:00 to next day 00:00)
        $thatDay = (Get-Date).AddDays(-$d).Date
        $nextDay = $thatDay.AddDays(1)
        $dayLabel = $thatDay.ToString('MM-dd ddd')
        # Use ISO 8601 with T separator to avoid PS native-exe space-quoting bugs
        $sinceArg = "--since=$($thatDay.ToString('yyyy-MM-ddTHH:mm:ss'))"
        $untilArg = "--until=$($nextDay.ToString('yyyy-MM-ddTHH:mm:ss'))"
        $gitArgs = @('log', 'origin/main', '--pretty=%s', $sinceArg, $untilArg)
        $log = & git @gitArgs 2>$null
        $count = @($log | Where-Object { $_ -match '\(#\d+\)' }).Count
        $dailyData += @{ label = $dayLabel; count = $count }
    }
    $maxCount = ($dailyData | ForEach-Object { $_.count } | Measure-Object -Maximum).Maximum
    if ($maxCount -lt 1) { $maxCount = 1 }   # avoid div by zero
    foreach ($d in $dailyData) {
        $barLen = [Math]::Min(40, [Math]::Round($d.count * 40 / $maxCount, 0))
        $bar = '#' * $barLen
        Write-Host ("      {0}  {1,-40}  {2}" -f $d.label, $bar, $d.count)
    }

    Write-Host ""
    Write-Host "Read: high freq (>=10/24h) -> sibling collaborator pressure high, run sync_with_main.bat before long task" -ForegroundColor DarkGray
    exit 0
}

Write-Host "==========================================="
Write-Host "  OpenClaw sync-with-main"
Write-Host "==========================================="
Write-Host ""

# ---- 1. fetch ----
Write-Host "[1/4] git fetch origin main"
$fetchOutput = & git fetch origin main 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "   [ERROR] fetch failed:" -ForegroundColor Red
    $fetchOutput | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkRed }
    exit 1
}
Write-Host "   [OK]   fetched origin/main"

# ---- 2. inspect state ----
$currentBranch = (& git branch --show-current 2>$null).Trim()
Write-Host ""
Write-Host "[2/4] State inspection"
Write-Host ("   current branch: {0}" -f $currentBranch)

$mainAhead = [int](& git rev-list --count "origin/main..main" 2>$null)
$mainBehind = [int](& git rev-list --count "main..origin/main" 2>$null)
Write-Host ("   local main vs origin/main: ahead={0} behind={1}" -f $mainAhead, $mainBehind)

if ($currentBranch -ne 'main') {
    $branchAhead = [int](& git rev-list --count "main..HEAD" 2>$null)
    $branchBehind = [int](& git rev-list --count "HEAD..main" 2>$null)
    $branchAheadOrigin = [int](& git rev-list --count "origin/main..HEAD" 2>$null)
    $branchBehindOrigin = [int](& git rev-list --count "HEAD..origin/main" 2>$null)
    Write-Host ("   {0} vs main:        ahead={1} behind={2}" -f $currentBranch, $branchAhead, $branchBehind)
    Write-Host ("   {0} vs origin/main: ahead={1} behind={2}" -f $currentBranch, $branchAheadOrigin, $branchBehindOrigin)
}

# ---- 3. recommendation ----
Write-Host ""
Write-Host "[3/4] Recommendation"

$needPull = $mainBehind -gt 0
$needSync = ($currentBranch -ne 'main') -and (([int](& git rev-list --count "HEAD..origin/main" 2>$null)) -gt 0)

# MMM: smart recommendation - if any of branch's commits are squash-merged
# (detected by patch-id via `git cherry`), suggest -CherryPick over -Rebase.
$squashSuspected = $false
if ($needSync -and $currentBranch -ne 'main') {
    $cherryHint = & git cherry origin/main HEAD 2>$null
    $hintFresh = @($cherryHint | Where-Object { $_ -match '^\+\s+' }).Count
    $hintSquash = @($cherryHint | Where-Object { $_ -match '^\-\s+' }).Count
    if ($hintSquash -gt 0) { $squashSuspected = $true }
}

if (-not $needPull -and -not $needSync) {
    Write-Host "   [OK]   already up to date with origin/main" -ForegroundColor Green
}
if ($needPull) {
    Write-Host ("   [SUGGEST] local main is behind origin/main by {0}; run sync_with_main.bat -Pull" -f $mainBehind) -ForegroundColor Cyan
}
if ($needSync) {
    # RRR: New canonical advice — git rebase has built-in patch-content
    # detection that auto-drops "patch contents already upstream" commits.
    # In practice this works for most squash-merge cases. Only fallback to
    # -CherryPick when -Rebase actually fails on a real conflict.
    Write-Host ("   [SUGGEST] {0} is behind origin/main." -f $currentBranch) -ForegroundColor Cyan
    Write-Host "             Recommended: sync_with_main.bat -Rebase -AutoStash" -ForegroundColor Cyan
    Write-Host "             One-flag mode: sync_with_main.bat -Auto  (rebase, fallback to cherry-pick)" -ForegroundColor Cyan
    if ($squashSuspected) {
        Write-Host ("   [INFO]    {0} commit(s) detected as already-in-main by patch-id." -f $hintSquash) -ForegroundColor DarkCyan
        Write-Host "             git rebase will auto-drop them via patch-content detection." -ForegroundColor DarkCyan
        Write-Host "             If rebase conflicts: fallback to sync_with_main.bat -CherryPick -AutoStash" -ForegroundColor DarkCyan
    }
}

# ---- 4. apply (if requested) ----
Write-Host ""
Write-Host "[4/4] Apply"

if ($Pull) {
    if ($currentBranch -ne 'main') {
        Write-Host "   [ERROR] -Pull requires being on main. Currently on $currentBranch." -ForegroundColor Red
        Write-Host "           git checkout main && sync_with_main.bat -Pull" -ForegroundColor DarkRed
        exit 1
    }
    Write-Host "   git pull --ff-only origin main"
    & git pull --ff-only origin main 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   [ERROR] pull failed (likely diverged); manual resolve needed" -ForegroundColor Red
        exit 1
    }
    Write-Host "   [OK]   main fast-forwarded to origin/main" -ForegroundColor Green
} elseif ($Rebase) {
    if ($currentBranch -eq 'main') {
        Write-Host "   [ERROR] -Rebase is for feat-* branches, not main. Use -Pull instead." -ForegroundColor Red
        exit 1
    }
    # Check for uncommitted changes
    $dirty = & git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\?' }
    if ($dirty -and -not $AutoStash) {
        Write-Host "   [ERROR] working tree has uncommitted changes; commit or stash first." -ForegroundColor Red
        Write-Host "           Or pass -AutoStash to auto stash+rebase+pop." -ForegroundColor DarkRed
        exit 1
    }
    if ($dirty -and $AutoStash) {
        Write-Host "   [INFO] working tree dirty; using --autostash" -ForegroundColor Cyan
    }
    $rebaseArgs = @('rebase')
    if ($AutoStash) { $rebaseArgs += '--autostash' }
    $rebaseArgs += 'origin/main'
    Write-Host ("   git {0}  ({1} -> origin/main)" -f ($rebaseArgs -join ' '), $currentBranch)
    & git @rebaseArgs 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "   [CONFLICT] rebase has conflicts on a real file change." -ForegroundColor Yellow
        # UUU: Updated guidance (2026-04-26) — git rebase has patch-content
        # detection that auto-drops "patch contents already upstream" commits,
        # so most squash-merge cases pass cleanly. A real conflict means
        # main and this branch both modified the same lines.
        Write-Host "   Primary fix (resolve the actual conflict):" -ForegroundColor DarkYellow
        Write-Host "      1) edit conflicted files" -ForegroundColor DarkYellow
        Write-Host "      2) git add <files>" -ForegroundColor DarkYellow
        Write-Host "      3) git rebase --continue" -ForegroundColor DarkYellow
        Write-Host "   Or abort and try a different strategy:" -ForegroundColor DarkGray
        Write-Host "      git rebase --abort" -ForegroundColor DarkGray
        Write-Host "      sync_with_main.bat -CherryPick -AutoStash    (skip squashed by patch-id)" -ForegroundColor DarkGray
        exit 1
    }
    Write-Host "   [OK]   rebased onto origin/main" -ForegroundColor Green
    $newAhead = [int](& git rev-list --count "origin/main..HEAD" 2>$null)
    Write-Host ("          now {0} commit(s) ahead of origin/main, ready to push" -f $newAhead) -ForegroundColor Cyan
} elseif ($CherryPick) {
    # FFF: smart cherry-pick using `git cherry` to detect squash-merged commits
    # by patch-id. Creates new branch off origin/main and applies only fresh ones.
    if ($currentBranch -eq 'main') {
        Write-Host "   [ERROR] -CherryPick is for feat-* branches, not main." -ForegroundColor Red
        exit 1
    }

    # git cherry origin/main HEAD format: "+ <sha>" = fresh, "- <sha>" = already in main (by patch-id)
    $cherryOutput = & git cherry origin/main HEAD 2>$null
    $freshCommits = @()
    $squashedCommits = @()
    foreach ($line in $cherryOutput) {
        if ($line -match '^\+\s+([a-f0-9]+)$')      { $freshCommits += $matches[1] }
        elseif ($line -match '^\-\s+([a-f0-9]+)$')  { $squashedCommits += $matches[1] }
    }

    Write-Host ""
    Write-Host ("   Found: {0} fresh commit(s), {1} already-in-main (by patch-id)" -f $freshCommits.Count, $squashedCommits.Count)
    if ($squashedCommits.Count -gt 0) {
        Write-Host "          (squashed-to-main commits will be skipped automatically)" -ForegroundColor DarkGray
    }

    if ($freshCommits.Count -eq 0) {
        Write-Host "   [OK]   nothing fresh to cherry-pick. Branch is fully merged." -ForegroundColor Green
        Write-Host ("          Safe to delete: git branch -D {0}" -f $currentBranch) -ForegroundColor DarkGray
        exit 0
    }

    # Pick destination branch name
    if ($BranchName) {
        $cleanName = $BranchName -replace '[^a-zA-Z0-9-]', '-'
        $newBranch = "feat-ops-$cleanName-$(Get-Date -Format 'yyyy-MM-dd')"
    } else {
        $newBranch = "feat-ops-resync-$(Get-Date -Format 'yyyy-MM-dd-HHmm')"
    }
    $exists = & git rev-parse --verify --quiet "refs/heads/$newBranch" 2>$null
    if ($exists) {
        Write-Host ("   [ERROR] new branch '{0}' already exists. Pass -BranchName <unique>." -f $newBranch) -ForegroundColor Red
        exit 1
    }

    # LLL: dirty handling with optional -AutoStash
    # Two kinds of dirty matter for cherry-pick:
    #   1) staged/modified that would conflict with checkout (rare)
    #   2) runtime config files (cluster_state.json etc) — safe to carry across
    # We use git stash (with -u for untracked) when -AutoStash is given.
    $dirty = & git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\?' }
    $stashed = $false
    if ($dirty -and -not $AutoStash) {
        Write-Host "   [ERROR] working tree has uncommitted changes; commit or stash first." -ForegroundColor Red
        Write-Host "           Or pass -AutoStash to auto stash+cherry-pick+pop." -ForegroundColor DarkRed
        exit 1
    }
    if ($dirty -and $AutoStash) {
        Write-Host "   [INFO] working tree dirty; stashing before cherry-pick" -ForegroundColor Cyan
        & git stash push -u -m "sync_with_main_autostash_$(Get-Date -Format yyyyMMdd-HHmmss)" 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -eq 0) {
            $stashed = $true
        } else {
            Write-Host "   [ERROR] stash failed; aborting cherry-pick" -ForegroundColor Red
            exit 1
        }
    }

    Write-Host ""
    Write-Host ("   Creating '{0}' off origin/main..." -f $newBranch)
    & git checkout -b $newBranch origin/main 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   [ERROR] checkout failed (see above)" -ForegroundColor Red
        if ($stashed) { Write-Host "          recover: git stash pop" -ForegroundColor DarkRed }
        exit 1
    }

    Write-Host ""
    Write-Host ("   Cherry-picking {0} fresh commit(s) (oldest first)..." -f $freshCommits.Count)
    foreach ($sha in $freshCommits) {
        $shortSha = $sha.Substring(0, 7)
        $subject = (& git log -1 --pretty=%s $sha 2>$null).Trim()
        Write-Host ("      pick {0}: {1}" -f $shortSha, $subject) -ForegroundColor DarkCyan
        & git cherry-pick $sha 2>&1 | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host ("   [CONFLICT] cherry-pick {0} failed. Resolve:" -f $shortSha) -ForegroundColor Yellow
            Write-Host "      1) edit conflicted files / git add <files>" -ForegroundColor DarkGray
            Write-Host "      2) git cherry-pick --continue" -ForegroundColor DarkGray
            Write-Host "      or abort: git cherry-pick --abort && git checkout - && git branch -D $newBranch" -ForegroundColor DarkGray
            exit 1
        }
    }

    Write-Host ""
    Write-Host ("   [OK]   cherry-picked {0} commit(s) onto {1}" -f $freshCommits.Count, $newBranch) -ForegroundColor Green

    # LLL: pop the stash back
    if ($stashed) {
        Write-Host "   [INFO] popping autostash..." -ForegroundColor Cyan
        & git stash pop 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "   [WARN] stash pop had conflicts; resolve manually with 'git stash list' / 'git stash pop'" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor DarkCyan
    Write-Host ("  - git push -u origin {0} && gh pr create" -f $newBranch)
    if ($squashedCommits.Count -gt 0) {
        Write-Host ("  - obsolete: git branch -D {0}" -f $currentBranch) -ForegroundColor DarkGray
    }
} elseif ($Auto) {
    # SSS: Auto mode = try -Rebase --AutoStash first (works for most squash
    # cases via git's patch-content detection). If rebase fails with a real
    # conflict, abort and fallback to -CherryPick --AutoStash.
    if ($currentBranch -eq 'main') {
        Write-Host "   [ERROR] -Auto is for feat-* branches, not main. Use -Pull instead." -ForegroundColor Red
        exit 1
    }

    # ZZZ: dry-run support - show plan without executing
    if ($DryRun) {
        Write-Host "   [DRY RUN] -Auto would do:" -ForegroundColor Yellow
        Write-Host "      [1/2] git rebase --autostash origin/main" -ForegroundColor DarkYellow
        Write-Host "             (git auto-drops 'patch contents already upstream' commits)" -ForegroundColor DarkGray
        Write-Host "      [2/2] on rebase failure: git rebase --abort" -ForegroundColor DarkYellow
        Write-Host "             then: sync_with_main.bat -CherryPick -AutoStash" -ForegroundColor DarkYellow
        Write-Host "             (creates new branch off origin/main, cherry-picks fresh commits)" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "      Re-run without -DryRun to execute." -ForegroundColor Cyan
        exit 0
    }

    Write-Host "   [Auto/1] Attempting -Rebase --AutoStash first..." -ForegroundColor Cyan

    & git rebase --autostash origin/main 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "   [OK]   -Rebase succeeded (likely git auto-dropped already-upstream patches)" -ForegroundColor Green
        $newAhead = [int](& git rev-list --count "origin/main..HEAD" 2>$null)
        Write-Host ("          now {0} commit(s) ahead of origin/main, ready to push" -f $newAhead) -ForegroundColor Cyan
        exit 0
    }

    # Rebase failed - abort and fallback
    Write-Host ""
    Write-Host "   [Auto/1] -Rebase failed (conflict). Aborting and trying -CherryPick fallback..." -ForegroundColor Yellow
    & git rebase --abort 2>&1 | Out-Null

    # Re-invoke ourselves with -CherryPick -AutoStash
    Write-Host ""
    Write-Host "   [Auto/2] sync_with_main.bat -CherryPick -AutoStash"
    & $PSCommandPath -CherryPick -AutoStash
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "   [ERROR] both -Rebase and -CherryPick failed. Manual resolution needed." -ForegroundColor Red
        Write-Host "           Inspect: git status / git log --oneline HEAD..origin/main" -ForegroundColor DarkRed
        exit 1
    }
} else {
    Write-Host "   [SKIP] no -Rebase / -Pull / -CherryPick / -Auto flag (read-only mode)" -ForegroundColor DarkGray
}

exit 0
