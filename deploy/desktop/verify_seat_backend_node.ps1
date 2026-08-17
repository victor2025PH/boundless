# verify_seat_backend_node.ps1 -- prove the SEAT app is running a FRESH backend
# on the real seat port after an install+relaunch (WP-5 root-fix companion,
# 2026-08-17).
#
# Runs ON THE TARGET. Closes the 2026-08-13 false-positive hole for good: the
# throwaway-port smoke proves "the shipped bytes can serve", but only THIS check
# proves "the operator's actual app came back on a NEW backend". Judge (per the
# SOP note): %APPDATA%\telegram-ai-desktop\data\logs\run_sentinel.json --
# started_at must be LATER than the install moment (a surviving old backend has
# started_at from days ago while beat_at keeps ticking = the smoking gun), and
# the real seat port must answer /login.
#
# ASCII-only (PS 5.1 decodes BOM-less UTF-8 as GBK).
# Exit: 0 fresh backend serving / 1 STALE backend detected (started_at predates
#       install) / 2 sentinel never appeared (backend did not boot) / 3 sentinel
#       fresh but seat port never answered
[CmdletBinding()]
param(
  # Target-machine epoch seconds captured just before the relaunch; the new
  # backend's started_at must be >= this (minus a small slop for clock reads).
  [Parameter(Mandatory = $true)][double]$SinceEpoch,
  [int]$TimeoutSec = 180,
  [int]$SeatPort = 18799,
  [double]$SlopSec = 90
)
$ErrorActionPreference = 'Continue'
function Say($m) { Write-Output ("[seatverify] " + $m) }

$sentinel = Join-Path $env:APPDATA 'telegram-ai-desktop\data\logs\run_sentinel.json'
Say ("sentinel: " + $sentinel)
Say ("since-epoch: " + [long]$SinceEpoch + " (backend started_at must be newer)")

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$sawStale = $false
$fresh = $false
$started = 0.0
while ((Get-Date) -lt $deadline) {
  if (Test-Path $sentinel) {
    try {
      $j = Get-Content $sentinel -Raw -Encoding UTF8 | ConvertFrom-Json
      $started = [double]$j.started_at
      if ($started -ge ($SinceEpoch - $SlopSec)) {
        Say ("sentinel FRESH: pid=" + $j.pid + " started_at=" + [long]$started +
             " beat_at=" + [long]$j.beat_at)
        $fresh = $true
        break
      }
      if (-not $sawStale) {
        # An old started_at while we wait is normal for the first seconds (the
        # new backend has not overwritten the file yet) -- only report once and
        # keep polling until the deadline decides.
        Say ("sentinel still shows OLD backend: started_at=" + [long]$started +
             " (predates install) -- waiting for the new backend to overwrite it")
        $sawStale = $true
      }
    } catch {
      Say ("sentinel parse failed (retrying): " + $_.Exception.Message)
    }
  }
  Start-Sleep -Seconds 5
}
if (-not $fresh) {
  if ($sawStale -or $started -gt 0) {
    Say "FAIL: backend sentinel still predates this install = the seat is on a STALE backend"
    Say "      (this is the exact 'exe only (backend not serving)' incident signature)"
    exit 1
  }
  Say "FAIL: sentinel never appeared -- the app shell may be up but its backend never booted"
  exit 2
}

# Sentinel says the new backend booted; now prove it actually serves the seat port.
$portDeadline = (Get-Date).AddSeconds([Math]::Max(60, $TimeoutSec / 2))
while ((Get-Date) -lt $portDeadline) {
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $SeatPort + "/login") -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) {
      Say ("seat port " + $SeatPort + " serving (login 200)")
      Say "OK - seat is on a fresh backend and it serves"
      exit 0
    }
  } catch { Start-Sleep -Seconds 4 }
}
Say ("FAIL: fresh backend booted but seat port " + $SeatPort + " never answered /login")
exit 3
