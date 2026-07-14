# OpenClaw one-shot migration: any-launch-shape -> standard service_wrapper
# Usage: migrate.bat  (or directly: powershell -File migrate.ps1)
#
# What it does:
#   1. Detect current running shape (uvicorn / server.py / wrapper)
#   2. Detect current port (preserve it via config/launch.env)
#   3. Stop current process(es)
#   4. Start standard wrapper using launch.env
#   5. Verify (status check)
#
# Args:
#   -Force   Even if already running as wrapper, stop+start anyway (useful
#            when wrapper exists but server is unhealthy / port wrong / etc).
#
# Reference: docs/SYSTEM_RUNBOOK.md F8

param([switch]$Force)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot
. "$PSScriptRoot\_lib.ps1"

Write-Host "==========================================="
Write-Host "  OpenClaw Migration: -> standard wrapper"
Write-Host "==========================================="
Write-Host ""

# ---- 1. Detect current shape ----
$kind = Get-OpenClawProcessKind
if (-not $kind) {
    Write-Host "[INFO] No OpenClaw process running. Will just start fresh." -ForegroundColor Cyan
} elseif ($kind -eq 'wrapper' -and -not $Force) {
    Write-Host "[INFO] Already using service_wrapper. Nothing to migrate." -ForegroundColor Green
    Write-Host "       (run stop.bat then start.bat for a clean restart)"
    Write-Host "       (or migrate.bat -Force to forcibly stop+start)"
    Write-Host ""
    Write-Host "Running status check..."
    & (Join-Path $PSScriptRoot "status.ps1")
    exit 0
} elseif ($kind -eq 'wrapper' -and $Force) {
    Write-Host "[INFO] Already wrapper, but -Force given. Will stop+start anyway." -ForegroundColor Yellow
} else {
    Write-Host ("[INFO] Current launch shape: {0}" -f $kind) -ForegroundColor Yellow
    Write-Host "       Will migrate to service_wrapper (auto-restart + OTA)"
}

# ---- 2. Detect current port (preserve it) ----
$currentPort = $null
foreach ($port in @(8000, 18080)) {
    $listening = netstat -ano | Select-String "LISTENING" | Select-String ":$port "
    if ($listening) {
        $currentPort = $port
        break
    }
}

if ($currentPort) {
    Write-Host ("[INFO] Detected current port: {0}" -f $currentPort) -ForegroundColor Cyan
}

# ---- 3. Update config/launch.env to preserve port ----
$envFile = Join-Path $ProjectRoot "config\launch.env"
if ($currentPort) {
    if (Test-Path $envFile) {
        $content = Get-Content $envFile -Raw
        $hasActiveLine = $content -match '(?m)^\s*OPENCLAW_PORT\s*='
        if ($hasActiveLine) {
            $newContent = $content -replace '(?m)^\s*OPENCLAW_PORT\s*=.*$', "OPENCLAW_PORT=$currentPort"
            $newContent | Set-Content -Path $envFile -Encoding UTF8 -NoNewline
            Write-Host ("[OK] Updated config/launch.env: OPENCLAW_PORT={0}" -f $currentPort) -ForegroundColor Green
        } else {
            Add-Content -Path $envFile -Value "`nOPENCLAW_PORT=$currentPort" -Encoding UTF8
            Write-Host ("[OK] Appended to config/launch.env: OPENCLAW_PORT={0}" -f $currentPort) -ForegroundColor Green
        }
    } else {
        @"
# OpenClaw launch profile (auto-created by migrate.ps1)
OPENCLAW_PORT=$currentPort
"@ | Set-Content -Path $envFile -Encoding UTF8
        Write-Host ("[OK] Created config/launch.env with OPENCLAW_PORT={0}" -f $currentPort) -ForegroundColor Green
    }
}

# ---- 4. Stop current ----
if ($kind) {
    Write-Host ""
    Write-Host "--- Stopping current process(es) ---"
    & (Join-Path $PSScriptRoot "stop.ps1") -NoExit
    Start-Sleep -Seconds 1
}

# ---- 5. Start wrapper ----
Write-Host ""
Write-Host "--- Starting service_wrapper ---"
& (Join-Path $PSScriptRoot "start.ps1")

# start.ps1 already runs status at the end, so we are done.
Write-Host ""
Write-Host "==========================================="
Write-Host "  Migration complete"
Write-Host "==========================================="
Write-Host "If [1/5] now shows wrapper + server, you are good."
Write-Host "Future starts: just run start.bat (loads config/launch.env automatically)"
