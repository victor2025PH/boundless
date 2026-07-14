# OpenClaw log cleanup
# Deletes *.log files older than N days under logs/_archive/ (and rotated
# logs/openclaw.log.* if requested). Active logs/openclaw.log is NEVER touched.
#
# Args:
#   -Days N      Delete files older than N days. Default 30.
#   -DryRun      Print what would be deleted, do not delete.
#   -IncludeRotated   Also include logs/openclaw.log.N rotated files.
#
# Usage:
#   cleanup_logs.bat                      # default 30d, archive only
#   cleanup_logs.bat -Days 7              # 7 days
#   cleanup_logs.bat -DryRun              # preview
#   cleanup_logs.bat -IncludeRotated      # also rotated openclaw.log.N

param(
    [int]$Days = 30,
    [switch]$DryRun,
    [switch]$IncludeRotated
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

Write-Host "==========================================="
Write-Host "  OpenClaw Log Cleanup"
Write-Host "==========================================="
Write-Host ""
Write-Host ("Mode:    {0}" -f $(if ($DryRun) { 'DRY RUN (no delete)' } else { 'LIVE DELETE' }))
Write-Host ("Cutoff:  files older than $Days days ($(((Get-Date).AddDays(-$Days)).ToString('yyyy-MM-dd HH:mm')))")
Write-Host ""

$cutoff = (Get-Date).AddDays(-$Days)
$candidates = New-Object System.Collections.Generic.List[System.IO.FileInfo]

# 1. logs/_archive/ all files (recursive)
$archiveDir = Join-Path $ProjectRoot "logs\_archive"
if (Test-Path $archiveDir) {
    Get-ChildItem $archiveDir -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.LastWriteTime -lt $cutoff) {
            $candidates.Add($_)
        }
    }
}

# 2. Rotated openclaw.log.N (do NOT touch openclaw.log itself)
if ($IncludeRotated) {
    $logsDir = Join-Path $ProjectRoot "logs"
    if (Test-Path $logsDir) {
        Get-ChildItem $logsDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^openclaw\.log\.\d+$' } |
            ForEach-Object {
                if ($_.LastWriteTime -lt $cutoff) {
                    $candidates.Add($_)
                }
            }
    }
}

if ($candidates.Count -eq 0) {
    Write-Host "[OK] No files older than $Days days. Nothing to clean." -ForegroundColor Green
    exit 0
}

# Summary
$totalBytes = ($candidates | Measure-Object -Property Length -Sum).Sum
$totalMB = [Math]::Round($totalBytes / 1MB, 2)
Write-Host ("Found {0} file(s), total {1} MB:" -f $candidates.Count, $totalMB) -ForegroundColor Cyan
Write-Host ""

# Sort by size desc and show top 10
$candidates | Sort-Object -Property Length -Descending | Select-Object -First 10 | ForEach-Object {
    $sizeMB = [Math]::Round($_.Length / 1MB, 2)
    Write-Host ("   {0,8} MB   {1}   {2}" -f $sizeMB, $_.LastWriteTime.ToString('yyyy-MM-dd'), $_.FullName.Replace($ProjectRoot, '.'))
}
if ($candidates.Count -gt 10) {
    Write-Host ("   ... and {0} more" -f ($candidates.Count - 10)) -ForegroundColor DarkGray
}
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] No files deleted. Re-run without -DryRun to delete." -ForegroundColor Yellow
    exit 0
}

# Delete
$deleted = 0
$failed = 0
foreach ($f in $candidates) {
    try {
        Remove-Item $f.FullName -Force -ErrorAction Stop
        $deleted++
    } catch {
        Write-Host ("[FAIL] {0}: {1}" -f $f.Name, $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host ("[OK] Deleted {0} files, freed {1} MB" -f $deleted, $totalMB) -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host ("[WARN] {0} files failed to delete" -f $failed) -ForegroundColor Yellow
    exit 1
}
exit 0
