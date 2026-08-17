# seat_temp_clean.ps1 -- weekly seat-side temp hygiene (2026-08-17 .198 lesson).
#
# Why: Chromium webviews inside the ChatX shell park download staging in
# %TEMP%\scoped_dir* and never clean it. On .198 (small C:) 45 stale dirs ate
# ~69 GB and drove free space to 2.1 GB -- hurting BOTH the release push and
# the running seat (caches/DB writes/downloads). push_chatx.ps1 now auto-remedies
# at release time; this task covers the weeks in between on small-disk seats.
#
# Conservative by design:
#   - scoped_dir* only when >7 days stale (release remedy uses >24h; a standing
#     weekly task can afford a wider margin -- zero risk to live sessions);
#   - old ChatX-Setup-*.exe in the stage dir: keep the newest one only;
#   - never touches *bak20* recovery dirs, app data, or anything outside
#     TEMP/stage; files locked by running processes silently survive.
# ASCII-only output (PS 5.1 GBK lesson). One log line per run, log capped.
#
# Registered on: kouxing (.198) as schtasks ChatXSeatTempClean, weekly SUN 06:40.
# Source of truth: deploy/desktop/seat_temp_clean.ps1 (scp a fresh copy when edited).
[CmdletBinding()]
param(
  [string]$StageDir = 'C:\Users\Administrator\Downloads\chatx',
  [int]$StaleDays = 7
)

$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path $StageDir 'seat_temp_clean.log'

$freeBefore = [math]::Round((Get-PSDrive C).Free / 1GB, 2)

# 1. stale Chromium scoped_dir* staging corpses in TEMP
$cut = (Get-Date).AddDays(-$StaleDays)
$stale = @(Get-ChildItem $env:TEMP -Directory -Filter scoped_dir* -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt $cut })
foreach ($d in $stale) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# 2. parked installers: keep newest only
$setups = @(Get-ChildItem $StageDir -Filter ChatX-Setup-*.exe -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending)
$removedSetups = 0
if ($setups.Count -gt 1) {
  foreach ($s in ($setups | Select-Object -Skip 1)) {
    Remove-Item $s.FullName -Force -ErrorAction SilentlyContinue
    $removedSetups++
  }
}

$freeAfter = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
$line = ('{0} scoped_dirs_removed={1} setups_removed={2} free_gb {3} -> {4}' -f `
  (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $stale.Count, $removedSetups, $freeBefore, $freeAfter)
Add-Content -Path $log -Value $line -Encoding ASCII

# log cap: keep the last 60 lines (one line/week = years of history, bounded)
$lines = @(Get-Content $log -ErrorAction SilentlyContinue)
if ($lines.Count -gt 60) {
  Set-Content -Path $log -Value ($lines | Select-Object -Last 60) -Encoding ASCII
}
Write-Output $line
exit 0
