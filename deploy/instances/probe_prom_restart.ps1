# probe_prom_restart.ps1 - Phase9: validate Prometheus text + write scrape credential stubs.
# Read-only against running instances (except writing token stubs under .ops, gitignored).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\probe_prom_restart.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\instances\probe_prom_restart.ps1 -WriteCredStubs

[CmdletBinding()]
param(
    [switch]$WriteCredStubs
)

$ErrorActionPreference = 'Stop'
$ops = 'D:\chengjie-instances\.ops'
$need = @(
    'chengjie_instance_restart_cooldown_active_any',
    'chengjie_instance_restart_flapping_any',
    'chengjie_instance_restart_cooldown_active',
    'chengjie_instance_http_phase',
    'chengjie_instance_restart_flapping',
    'chengjie_instance_restart_window_sla_breach_any',
    'chengjie_instance_restart_window_seconds'
)

Write-Host '=== chengjie prometheus restart probe ===' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $ops)) {
    New-Item -ItemType Directory -Force -Path $ops | Out-Null
}

$bad = 0
foreach ($t in @(
    @{ id = 'zhiliao'; port = 18799 },
    @{ id = 'tongyi';  port = 18899 }
)) {
    $token = ''
    $cfg = "D:\chengjie-instances\$($t.id)\data\config\config.local.yaml"
    if (Test-Path -LiteralPath $cfg) {
        $raw = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
        if ($raw -match '(?m)^\s*auth_token:\s*[''"]?([^\s''"#]+)') { $token = $Matches[1] }
    }
    if ($WriteCredStubs -and $token) {
        $rawPath = Join-Path $ops ("prom_bearer_{0}_raw" -f $t.id)
        # raw token only for prometheus authorization.credentials_file
        [System.IO.File]::WriteAllText($rawPath, $token)
        Write-Host ("  wrote credentials stub {0}" -f $rawPath) -ForegroundColor DarkGray
    }

    $hdr = @{}
    if ($token) { $hdr['Authorization'] = "Bearer $token" }
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/workspace/metrics?format=prometheus" -f $t.port) `
            -Headers $hdr -UseBasicParsing -TimeoutSec 12
        $txt = [string]$r.Content
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 403) {
            Write-Host ("  [{0}] STALE: prometheus Bearer scrape 403 (reload after cooldown to load Phase9 gate)" -f $t.id) -ForegroundColor Yellow
        } else {
            Write-Host ("  [{0}] FAIL scrape: {1}" -f $t.id, $_.Exception.Message) -ForegroundColor Red
        }
        $bad++
        continue
    }
    $miss = @()
    foreach ($m in $need) {
        if ($txt -notmatch [regex]::Escape($m)) { $miss += $m }
    }
    if ($miss.Count) {
        Write-Host ("  [{0}] STALE metrics missing: {1}" -f $t.id, ($miss -join ', ')) -ForegroundColor Yellow
        $bad++
    } else {
        Write-Host ("  [{0}] prom restart metrics: OK ({1} bytes)" -f $t.id, $txt.Length) -ForegroundColor Green
    }
}

$dash = Join-Path $PSScriptRoot 'grafana_dashboard_restart.json'
$rules = Join-Path $PSScriptRoot 'prometheus_alerts_chengjie_restart.yml'
$scrape = Join-Path $PSScriptRoot 'prometheus_scrape_chengjie.yml'
foreach ($p in @($dash, $rules, $scrape)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host ("FAIL missing wire file: {0}" -f $p) -ForegroundColor Red
        exit 2
    }
}
Write-Host '  wire files: dashboard + alerts + scrape snippet OK' -ForegroundColor Green

if ($bad -gt 0) {
    Write-Host 'PROBE PROM WARN' -ForegroundColor Yellow
    exit 1
}
Write-Host 'PROBE PROM OK' -ForegroundColor Green
Write-Host '  Next: merge scrape snippet into Prometheus; import grafana_dashboard_restart.json; load alert rules' -ForegroundColor DarkGray
exit 0
