# OpenClaw branch cleanup
# Find local feat-ops-* / feat-* branches whose commits are all already in
# origin/main (squash-merged) — these branches are obsolete and safe to delete.
#
# Args:
#   -DryRun       Default behavior: show what would be deleted, do nothing.
#   -Apply        Actually delete the obsolete branches.
#   -Pattern P    Only consider branches matching glob pattern (default: feat-*)
#   -Json         Structured output.
#
# Usage:
#   cleanup_branches.bat                # dry-run, list candidates
#   cleanup_branches.bat -Apply         # actually delete
#   cleanup_branches.bat -Pattern "feat-ops-*"

param(
    [switch]$Apply,
    [switch]$DryRun,
    [string]$Pattern = "feat-*",
    [switch]$Json
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

# Default to dry-run unless -Apply explicitly
if (-not $Apply) { $DryRun = $true }

# Need origin/main for patch-id comparison
$hasOriginMain = (& git rev-parse --verify --quiet "refs/remotes/origin/main") 2>$null
if (-not $hasOriginMain) {
    Write-Host "[ERROR] origin/main not found. Run: git fetch origin" -ForegroundColor Red
    exit 1
}

# List local branches matching pattern (excludes current branch via filter)
$current = (& git branch --show-current 2>$null).Trim()
$allBranches = & git for-each-ref --format='%(refname:short)' "refs/heads/$Pattern" 2>$null
$mainHead = (& git rev-parse origin/main 2>$null).Trim()

$obsolete = @()
$keep = @()

foreach ($br in $allBranches) {
    $br = $br.Trim()
    if (-not $br) { continue }
    if ($br -eq $current) { continue }   # never propose deleting current branch

    # git cherry origin/main <branch>: how many commits' content is already on main?
    $cherryOut = & git cherry origin/main $br 2>$null
    $freshCnt = @($cherryOut | Where-Object { $_ -match '^\+\s+' }).Count
    $squashCnt = @($cherryOut | Where-Object { $_ -match '^\-\s+' }).Count
    $totalCnt = $freshCnt + $squashCnt
    $sha = (& git rev-parse --short $br 2>$null).Trim()
    $brHead = (& git rev-parse $br 2>$null).Trim()

    $info = @{
        branch = $br
        head = $sha
        total = $totalCnt
        fresh = $freshCnt
        squashed = $squashCnt
    }
    # Obsolete iff branch has zero commits "ahead of" origin/main, i.e. branch
    # is fully consumed by main (squash-merged OR identical-to-ancestor).
    # Protect a freshly-checked-out branch sitting *exactly* on origin/main HEAD
    # (likely active-but-empty, deleting would just lose the ref name); abandoned
    # branches at older main ancestors are real leftovers and get cleaned.
    $isObsolete = ($freshCnt -eq 0) -and ($brHead -ne $mainHead)
    if ($isObsolete) {
        $obsolete += $info
    } else {
        $keep += $info
    }
}

if ($Json) {
    @{
        current = $current
        pattern = $Pattern
        mode = if ($DryRun) { 'dry-run' } else { 'apply' }
        obsolete = $obsolete
        keep = $keep
    } | ConvertTo-Json -Depth 4
    if ($Apply) {
        foreach ($o in $obsolete) {
            & git branch -D $o.branch 2>&1 | Out-Null
        }
    }
    exit 0
}

# Console output
Write-Host "==========================================="
Write-Host "  OpenClaw Branch Cleanup"
Write-Host "==========================================="
Write-Host ""
Write-Host ("Pattern: {0}   Current: {1}" -f $Pattern, $current)
Write-Host ("Mode:    {0}" -f $(if ($DryRun) { 'DRY RUN (no changes)' } else { 'APPLY (will delete)' }))
Write-Host ""

if ($obsolete.Count -eq 0) {
    Write-Host "[OK] No obsolete branches found." -ForegroundColor Green
    if ($keep.Count -gt 0) {
        Write-Host ""
        Write-Host ("Active branches ({0}):" -f $keep.Count) -ForegroundColor DarkCyan
        foreach ($k in $keep) {
            Write-Host ("   {0}  {1,3} commit(s) ({2} fresh / {3} already-in-main)" -f $k.branch, $k.total, $k.fresh, $k.squashed) -ForegroundColor DarkGray
        }
    }
    exit 0
}

Write-Host ("Obsolete branches ({0}) — all commits already in origin/main:" -f $obsolete.Count) -ForegroundColor Yellow
foreach ($o in $obsolete) {
    Write-Host ("   {0,-50}  {1}  ({2} commit(s) all squashed to main)" -f $o.branch, $o.head, $o.total) -ForegroundColor Yellow
}
Write-Host ""

if ($keep.Count -gt 0) {
    Write-Host ("Keep ({0}, has fresh commits):" -f $keep.Count) -ForegroundColor DarkCyan
    foreach ($k in $keep) {
        Write-Host ("   {0,-50}  {1}  ({2} fresh / {3} already-in-main)" -f $k.branch, $k.head, $k.fresh, $k.squashed) -ForegroundColor DarkGray
    }
    Write-Host ""
}

if ($DryRun) {
    Write-Host "[DRY RUN] Re-run with -Apply to actually delete obsolete branches." -ForegroundColor Cyan
    exit 0
}

# APPLY mode
Write-Host "Deleting obsolete branches..." -ForegroundColor Yellow
$deleted = 0
$failed = 0
foreach ($o in $obsolete) {
    & git branch -D $o.branch 2>&1 | ForEach-Object {
        Write-Host ("   $_") -ForegroundColor DarkGray
    }
    if ($LASTEXITCODE -eq 0) { $deleted++ } else { $failed++ }
}
Write-Host ""
Write-Host ("[OK] Deleted {0} branch(es), {1} failed." -f $deleted, $failed) -ForegroundColor $(if ($failed -gt 0) { 'Yellow' } else { 'Green' })
exit $(if ($failed -gt 0) { 1 } else { 0 })
