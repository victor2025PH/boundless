# drill_restart_resilience.ps1 - Phase8 controlled resilience drill.
# Default is SAFE: contract checks only (no process kill).
# Optional -Live restarts ONE instance via restart_instance (respects cooldown; never -Force).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\drill_restart_resilience.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\drill_restart_resilience.ps1 -Live -Instance tongyi

[CmdletBinding()]
param(
    [ValidateSet('zhiliao', 'tongyi')]
    [string]$Instance = 'tongyi',
    [switch]$Live
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_restart_cooldown.ps1')

Write-Host '=== chengjie restart-resilience drill ===' -ForegroundColor Cyan
Write-Host ("  mode={0} instance={1}" -f ($(if ($Live) { 'LIVE' } else { 'DRY' }), $Instance)) -ForegroundColor DarkGray

# 1) read-only smoke (phase / encoding / coalesce markers)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'smoke_restart_resilience.ps1')
if ($LASTEXITCODE -ge 2) {
    Write-Host 'DRILL FAIL - smoke failed hard' -ForegroundColor Red
    exit 2
}

# 2) contract: cooldown must block a second restart without -Force
$cd = Test-RestartCooldownActive -Instance $Instance
$advise = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    (Join-Path $PSScriptRoot 'restart_instance.ps1') `
    -Instance $Instance -Advise 2>&1 | Out-String
Write-Host '  --- restart_instance -Advise ---' -ForegroundColor DarkGray
Write-Host $advise

if ($cd.active) {
    Write-Host ("  cooldown ACTIVE left~{0}min - verifying block without -Force" -f $cd.left_min) -ForegroundColor Yellow
    $blocked = $true
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
            (Join-Path $PSScriptRoot 'restart_instance.ps1') `
            -Instance $Instance -DryRun 2>&1 | Out-Null
        # DryRun may still exit 0 after printing cool; check real gate:
        $gate = Test-RestartCooldownActive -Instance $Instance
        if (-not $gate.active) { $blocked = $false }
    } catch {}
    # Explicit non-force attempt should fail when cool (skip if DryRun doesn't hit gate the same way)
    $realTry = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $PSScriptRoot 'restart_instance.ps1') `
        -Instance $Instance 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'DRILL FAIL - cooldown did not block restart' -ForegroundColor Red
        Write-Host $realTry
        exit 2
    }
    Write-Host '  cooldown gate: OK (restart blocked)' -ForegroundColor Green
} else {
    Write-Host '  cooldown idle - gate block check skipped (would restart if -Live)' -ForegroundColor DarkGray
}

# 3) flap must not already be true before a deliberate single restart
$flapBefore = Test-RestartFlap -Instance $Instance
if ($flapBefore.flapping) {
    Write-Host ("DRILL WARN - already FLAPPING count={0}; do not -Live" -f $flapBefore.count) -ForegroundColor Yellow
    if ($Live) {
        Write-Host 'DRILL ABORT - refusing -Live during FLAP' -ForegroundColor Red
        exit 2
    }
} else {
    Write-Host ("  flap clear (count={0})" -f $flapBefore.count) -ForegroundColor Green
}

if (-not $Live) {
    Write-Host 'DRILL OK (dry) - pass -Live -Instance <id> at off-peak when cooldown idle' -ForegroundColor Green
    exit 0
}

# 4) LIVE: one restart via unique entry, then assert seat-ready + no flap from this alone
if ((Test-RestartCooldownActive -Instance $Instance).active) {
    Write-Host 'DRILL ABORT - cooldown active; wait then retry -Live (never -Force for drill)' -ForegroundColor Red
    exit 2
}

Write-Host ("  LIVE restart {0} via restart_instance ..." -f $Instance) -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
    (Join-Path $PSScriptRoot 'restart_instance.ps1') `
    -Instance $Instance `
    -Reason 'drill_restart_resilience'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'DRILL FAIL - restart_instance non-zero' -ForegroundColor Red
    exit 2
}

Start-Sleep -Seconds 3
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'status_instances.ps1') | Out-Host

$flapAfter = Test-RestartFlap -Instance $Instance
# A single deliberate restart must NOT trip FLAP (threshold 2 / 30m)
if ($flapAfter.flapping -and $flapBefore.count -eq 0 -and $flapAfter.count -ge 2) {
    Write-Host 'DRILL FAIL - single restart flipped FLAP (event double-write?)' -ForegroundColor Red
    exit 2
}
if ($flapAfter.count -ne ($flapBefore.count + 1)) {
    Write-Host ("DRILL FAIL - flap count expected {0}->{1}, got {2}" -f `
        $flapBefore.count, ($flapBefore.count + 1), $flapAfter.count) -ForegroundColor Red
    exit 2
}

$cdAfter = Test-RestartCooldownActive -Instance $Instance
if (-not $cdAfter.active) {
    Write-Host 'DRILL FAIL - cooldown not written after live restart' -ForegroundColor Red
    exit 2
}

# Phase9: seat surface must report quiet_poll after live restart (Python loaded)
$port = if ($Instance -eq 'zhiliao') { 18799 } else { 18899 }
$auth = ''
$cfg = "D:\chengjie-instances\$Instance\data\config\config.local.yaml"
if (Test-Path -LiteralPath $cfg) {
    $raw = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
    if ($raw -match '(?m)^\s*auth_token:\s*[''"]?([^\s''"#]+)') { $auth = $Matches[1] }
}
try {
    $hdr = @{}
    if ($auth) { $hdr['Authorization'] = "Bearer $auth" }
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/workspace/ai-runtime-status" `
        -Headers $hdr -UseBasicParsing -TimeoutSec 12
    $js = $r.Content | ConvertFrom-Json
    $ir = $js.instance_restart
    if (-not $ir) { throw 'missing instance_restart on ai-runtime-status' }
    if (-not $ir.quiet_poll) {
        Write-Host 'DRILL FAIL - quiet_poll not true after live restart' -ForegroundColor Red
        exit 2
    }
    if (-not $ir.cooldown_active) {
        Write-Host 'DRILL FAIL - cooldown_active not true on seat banner' -ForegroundColor Red
        exit 2
    }
    Write-Host ("  seat banner: quiet_poll={0} cool_left={1}s phase={2}" -f `
        $ir.quiet_poll, $ir.cooldown_left_sec, $ir.http_phase) -ForegroundColor Green
} catch {
    Write-Host ("DRILL FAIL - seat surface check: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 2
}

# Prom gauges must expose post-restart cooldown (scrape contract for Grafana)
try {
    $pm = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/workspace/metrics?format=prometheus" `
        -Headers $hdr -UseBasicParsing -TimeoutSec 12
    $txt = [string]$pm.Content
    if ($txt -notmatch 'chengjie_instance_restart_cooldown_active_any 1') {
        Write-Host 'DRILL FAIL - prom missing cooldown_active_any 1' -ForegroundColor Red
        exit 2
    }
    if ($txt -notmatch ("chengjie_instance_restart_cooldown_active\{instance=`"$Instance`"\} 1")) {
        Write-Host 'DRILL FAIL - prom instance cooldown gauge not 1' -ForegroundColor Red
        exit 2
    }
    Write-Host '  prometheus restart gauges: OK' -ForegroundColor Green
} catch {
    Write-Host ("DRILL FAIL - prom scrape: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 2
}

Write-Host 'DRILL OK (live) - ready + cooldown + quiet_poll + prom + no flap storm' -ForegroundColor Green
Write-Host '  seat: refresh browser; expect blue cool banner + quiet poll 15s/45s' -ForegroundColor DarkGray
exit 0
