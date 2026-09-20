# probe_ops_surface.ps1 — Phase8: check whether live processes expose restart-resilience APIs.
# Read-only. Does NOT restart. Use after code land to see if Python surface is loaded
# (stale process = missing quiet_poll / http_phase until next cooldown-respecting restart).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\probe_ops_surface.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\probe_ops_surface.ps1 -Token <web_token>

[CmdletBinding()]
param(
    [string]$Token = $env:AITR_WEB_TOKEN
)

$ErrorActionPreference = 'Stop'
$targets = @(
    @{ id = 'zhiliao'; port = 18799 },
    @{ id = 'tongyi';  port = 18899 }
)

Write-Host '=== chengjie ops surface probe (read-only) ===' -ForegroundColor Cyan

$need = @('quiet_poll', 'flapping', 'http_phase', 'cooldown_active')
$stale = 0
$down = 0

foreach ($t in $targets) {
    $base = "http://127.0.0.1:$($t.port)"
    $loginOk = $false
    try {
        $lr = Invoke-WebRequest -Uri "$base/login" -UseBasicParsing -TimeoutSec 5
        $loginOk = ($lr.StatusCode -eq 200)
    } catch {
        Write-Host ("  [{0}] DOWN login probe failed: {1}" -f $t.id, $_.Exception.Message) -ForegroundColor Red
        $down++
        continue
    }
    Write-Host ("  [{0}] login=200" -f $t.id) -ForegroundColor Green

    $hdr = @{}
    $instToken = $Token
    if (-not $instToken) {
        foreach ($cand in @(
            "D:\chengjie-instances\$($t.id)\data\config\config.local.yaml",
            (Join-Path $PSScriptRoot "$($t.id)\config.local.yaml")
        )) {
            if (-not (Test-Path -LiteralPath $cand)) { continue }
            try {
                $raw = Get-Content -LiteralPath $cand -Raw -Encoding UTF8
                if ($raw -match '(?m)^\s*auth_token:\s*[''"]?([^\s''"#]+)') {
                    $instToken = $Matches[1]
                    Write-Host '         auth_token loaded from instance overlay' -ForegroundColor DarkGray
                    break
                }
            } catch {}
        }
    }
    if ($instToken) {
        $hdr['Authorization'] = "Bearer $instToken"
    }

    $surface = $null
    $routePresent = $false   # 401/403 => route loaded (auth only); 404 => missing/stale
    foreach ($path in @(
        '/api/workspace/ai-runtime-status',
        '/api/admin/instance-restart-status'
    )) {
        try {
            $r = Invoke-WebRequest -Uri ($base + $path) -Headers $hdr -UseBasicParsing -TimeoutSec 8
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) {
                $surface = $r.Content | ConvertFrom-Json
                $routePresent = $true
                Write-Host ("         OK {0}" -f $path) -ForegroundColor DarkGray
                break
            }
        } catch {
            $code = 0
            try { $code = [int]$_.Exception.Response.StatusCode } catch {}
            if ($code -eq 401 -or $code -eq 403) {
                $routePresent = $true
                Write-Host ("         AUTH {0} HTTP {1} (route present; pass -Token for field check)" -f $path, $code) -ForegroundColor DarkYellow
                break
            }
            if ($code -eq 404) {
                Write-Host ("         MISS {0} HTTP 404 (stale process?)" -f $path) -ForegroundColor Yellow
            } else {
                Write-Host ("         skip {0}: {1}" -f $path, $_.Exception.Message) -ForegroundColor DarkYellow
            }
        }
    }

    if (-not $surface) {
        if ($routePresent) {
            Write-Host ("  [{0}] ROUTE OK (auth required for JSON fields; login=200)" -f $t.id) -ForegroundColor Green
            continue
        }
        Write-Host ("  [{0}] STALE: restart status routes missing (404) — reload after cooldown" -f $t.id) -ForegroundColor Yellow
        $stale++
        continue
    }

    # ai-runtime-status nests instance_restart; admin route is top-level
    $ir = $surface.instance_restart
    if (-not $ir) { $ir = $surface }
    $banner = $null
    if ($ir.PSObject.Properties.Name -contains 'cooldown_active' -and $ir.PSObject.Properties.Name -contains 'quiet_poll') {
        $banner = $ir
    } elseif ($surface.instance_restart) {
        $banner = $surface.instance_restart
    }

    $missing = @()
    if ($banner) {
        foreach ($k in $need) {
            if (-not ($banner.PSObject.Properties.Name -contains $k)) { $missing += $k }
        }
        Write-Host ("         banner quiet_poll={0} cool={1} flap={2} phase={3}" -f `
            $banner.quiet_poll, $banner.cooldown_active, $banner.flapping, $banner.http_phase) -ForegroundColor DarkGray
    } else {
        # admin full snapshot: check instances[].http_phase + flap
        $rows = @($ir.instances)
        if (-not $rows.Count) {
            $missing = @('instances')
        } else {
            $row = $rows | Where-Object { $_.id -eq $t.id } | Select-Object -First 1
            if (-not $row) { $row = $rows[0] }
            if (-not $row.http_phase) { $missing += 'http_phase' }
            if (-not $row.flap) { $missing += 'flap' }
            Write-Host ("         row phase={0} cool={1} flap={2} note={3}" -f `
                $row.http_phase, $row.cooldown_active, $row.flap.flapping, $row.live_note) -ForegroundColor DarkGray
        }
    }

    if ($missing.Count) {
        Write-Host ("  [{0}] STALE Python surface missing: {1}" -f $t.id, ($missing -join ',')) -ForegroundColor Yellow
        Write-Host '         wait cooldown then: restart_instance.ps1 -Instance <id> (no -Force)' -ForegroundColor DarkGray
        $stale++
    } else {
        Write-Host ("  [{0}] SURFACE OK (Phase6-8 fields present)" -f $t.id) -ForegroundColor Green
    }

    # Phase12b: boot phase timing (optional display; absent until first boot on
    # Phase11+ code — never fails the probe). Lives only on the admin route.
    try {
        $ar = Invoke-WebRequest -Uri ($base + '/api/admin/instance-restart-status') `
            -Headers $hdr -UseBasicParsing -TimeoutSec 8
        if ($ar.StatusCode -ge 200 -and $ar.StatusCode -lt 300) {
            $adm = $ar.Content | ConvertFrom-Json
            $bt = $adm.boot_timing
            if ($bt -and $bt.total_sec) {
                $seat = ''
                if ($adm.boot_seat_ready_sec) {
                    $seat = (' seat-ready={0}s' -f [math]::Round([double]$adm.boot_seat_ready_sec, 1))
                }
                Write-Host ("         boot total={0}s{1} slowest={2} {3}s" -f `
                    $bt.total_sec, $seat, $bt.slowest_phase, $bt.slowest_sec) -ForegroundColor DarkCyan
                $phTxt = @($bt.phases | ForEach-Object { '{0}={1}s' -f $_.name, $_.delta_sec }) -join ' '
                if ($phTxt) { Write-Host ("         boot phases: {0}" -f $phTxt) -ForegroundColor DarkGray }
            } else {
                Write-Host '         boot timing: n/a (process booted on pre-Phase11 code)' -ForegroundColor DarkGray
            }
        }
    } catch {}

    if (-not $loginOk) { $down++ }
}

Write-Host ''
if ($down -gt 0) {
    Write-Host 'PROBE FAIL — instance(s) down' -ForegroundColor Red
    exit 2
}
if ($stale -gt 0) {
    Write-Host 'PROBE WARN — surface incomplete (code landed but process not reloaded)' -ForegroundColor Yellow
    exit 1
}
Write-Host 'PROBE OK' -ForegroundColor Green
exit 0
