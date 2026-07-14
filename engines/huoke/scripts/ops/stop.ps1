# OpenClaw stop script (graceful)
# Usage: stop.bat  (or directly: powershell -File stop.ps1)
# When invoked from migrate.ps1, pass -NoExit so 'exit' does not kill the caller.

param([switch]$NoExit)

$ErrorActionPreference = 'Continue'
. "$PSScriptRoot\_lib.ps1"

function Exit-StopScript { param([int]$code)
    if ($NoExit) { return }
    exit $code
}

Write-Host "==========================================="
Write-Host "  OpenClaw - stopping"
Write-Host "==========================================="
Write-Host ""

$all = Get-OpenClawProcesses

if (-not $all) {
    Write-Host "[OK] No OpenClaw process running" -ForegroundColor Green
    Exit-StopScript 0
    return
}

# Stop wrapper first (so it does not respawn the server), then everything else
$wrappers = $all | Where-Object { $_.CommandLine -match 'service_wrapper\.py' }
$others   = $all | Where-Object { $_.CommandLine -notmatch 'service_wrapper\.py' }

foreach ($w in $wrappers) {
    Write-Host ("Stop wrapper PID={0}" -f $w.ProcessId)
    Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
}

if ($wrappers) { Start-Sleep -Milliseconds 500 }

foreach ($s in $others) {
    $tag = Get-OpenClawLaunchTag $s.CommandLine
    Write-Host ("Stop {0}  PID={1}" -f $tag, $s.ProcessId)
    Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

# Check leftovers
$still = Get-OpenClawProcesses

if ($still) {
    Write-Host ""
    Write-Host "[WARN] Leftover process(es):" -ForegroundColor Yellow
    foreach ($p in $still) {
        Write-Host ("   PID={0}  {1}" -f $p.ProcessId, $p.CommandLine)
    }
    Write-Host "Manual: Stop-Process -Id <PID> -Force"
    Exit-StopScript 1
} else {
    Write-Host ""
    Write-Host "[OK] All stopped" -ForegroundColor Green
    Exit-StopScript 0
}
