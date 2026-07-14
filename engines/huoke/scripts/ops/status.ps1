# OpenClaw status check (5 items)
# Usage: status.bat  (or directly: powershell -File status.ps1)
# For details on each item, see docs/SYSTEM_RUNBOOK.md
#
# Exit codes (for cron / monitor consumption):
#   0 = GO        all 5 items healthy
#   1 = DEGRADED  service up but with warnings (loopback bind / partial devices / errors in log)
#   2 = DOWN      no process / no port / /health not 200 / 0 devices
#
# Args:
#   -NoExit          When invoked from start.ps1 / migrate.ps1, prevent 'exit'
#                    from propagating to the caller (returns code instead).
#   -Watch [-Interval N]  Refresh every N seconds (default 5). Ctrl+C to exit.
#   -Open            After check, open dashboard in default browser (if up).
#   -Beep            Audible beep when DEGRADED/DOWN or any AUTH device.
#   -Json            Output structured JSON (no colored console). Useful for
#                    cron / monitor / Prometheus exporter consumption.

param(
    [switch]$NoExit,
    [switch]$Watch,
    [int]$Interval = 5,
    [switch]$Open,
    [switch]$Beep,
    [switch]$Json
)

# Mutex check: -Watch and -Json are exclusive (Watch is interactive console,
# Json is one-shot machine-readable output). Combining them makes no sense.
if ($Watch -and $Json) {
    Write-Host "[ERROR] -Watch and -Json are mutually exclusive." -ForegroundColor Red
    Write-Host "        -Watch is for interactive console refresh." -ForegroundColor DarkGray
    Write-Host "        -Json is for one-shot structured output." -ForegroundColor DarkGray
    exit 2
}

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot
. "$PSScriptRoot\_lib.ps1"

# In watch mode, NoExit is implied (we own the loop) and we recurse into
# the same script with a fresh state per iteration.
if ($Watch) {
    Write-Host "Watch mode: refresh every $Interval seconds. Ctrl+C to exit." -ForegroundColor DarkCyan
    while ($true) {
        Clear-Host
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Write-Host "[$stamp]  status --watch (interval=${Interval}s)" -ForegroundColor DarkGray
        & $PSCommandPath -NoExit @(
            if ($Open) { '-Open' }
            if ($Beep) { '-Beep' }
        )
        Start-Sleep -Seconds $Interval
    }
}

# ---- JSON mode: structured output for cron / monitor consumption ----
if ($Json) {
    $r = [ordered]@{
        timestamp        = (Get-Date).ToString('o')
        verdict          = 0
        verdict_label    = $null
        process          = @()
        ports            = @()
        health           = @()
        devices          = @{ connected = 0; total = 0; unauthorized = 0; list = @() }
        recent_errors    = @()
        pg_central_store = @{ status = 'unknown'; note = $null }
    }
    function _bump { param([int]$lvl) if ($lvl -gt $r.verdict) { $r.verdict = $lvl } }

    # [1] process
    $procs = Get-OpenClawProcesses
    foreach ($p in $procs) {
        $tag = (Get-OpenClawLaunchTag $p.CommandLine).Trim()
        $r.process += @{ kind = $tag; pid = $p.ProcessId }
    }
    if (-not $procs) { _bump 2 }
    if ($procs -and -not ($procs | Where-Object { $_.CommandLine -match 'service_wrapper\.py' })) {
        _bump 1
    }

    # [2] ports
    $foundPort = $null
    foreach ($port in @(8000, 18080)) {
        $listening = netstat -ano | Select-String "LISTENING" | Select-String ":$port "
        if ($listening) {
            foreach ($line in $listening) {
                $parts = $line.Line -split '\s+' | Where-Object { $_ }
                $localAddr = $parts[1]
                $pidVal = [int]$parts[-1]
                $bind = if ($localAddr -match '^([\d.]+):') { $matches[1] } else { 'unknown' }
                $lanReachable = ($bind -eq '0.0.0.0')
                $r.ports += @{ port = $port; pid = $pidVal; bind = $bind; lan_reachable = $lanReachable }
                if (-not $foundPort) { $foundPort = $port }
                if ($bind -eq '127.0.0.1') { _bump 1 }
            }
        }
    }
    if (-not $foundPort) { _bump 2 }

    # [3] health
    foreach ($port in @(8000, 18080)) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 `
                    -UseBasicParsing -ErrorAction Stop
            $r.health += @{ port = $port; code = [int]$resp.StatusCode }
        } catch {
            $code = $_.Exception.Response.StatusCode.value__
            if ($code) {
                $r.health += @{ port = $port; code = [int]$code }
                _bump 1
            }
        }
    }

    # [4] devices
    if ($foundPort) {
        try {
            $devicesJson = Invoke-WebRequest -Uri "http://127.0.0.1:$foundPort/devices" `
                           -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            $devices = $devicesJson.Content | ConvertFrom-Json
            $connected = @($devices | Where-Object { $_.status -eq 'connected' }).Count
            $unauth = @($devices | Where-Object { $_.usb_issue -eq 'unauthorized' }).Count
            $r.devices.connected = $connected
            $r.devices.total = $devices.Count
            $r.devices.unauthorized = $unauth
            foreach ($d in $devices) {
                $r.devices.list += @{
                    device_id = $d.device_id
                    display_name = $d.display_name
                    status = $d.status
                    usb_issue = $d.usb_issue
                }
            }
            if ($connected -lt $devices.Count -and $connected -gt 0) { _bump 1 }
            elseif ($connected -eq 0 -and $devices.Count -gt 0)      { _bump 2 }
        } catch { }
    }

    # [5+6] log tail
    $logFile = Join-Path $ProjectRoot "logs\openclaw.log"
    $tail = @()
    if (Test-Path $logFile) {
        try {
            $fs = [System.IO.File]::Open($logFile, [System.IO.FileMode]::Open,
                                          [System.IO.FileAccess]::Read,
                                          [System.IO.FileShare]::ReadWrite)
            try {
                $tailBytes = [Math]::Min(65536, $fs.Length)
                $fs.Seek(-$tailBytes, [System.IO.SeekOrigin]::End) | Out-Null
                $buf = New-Object byte[] $tailBytes
                [void]$fs.Read($buf, 0, $tailBytes)
                $text = [System.Text.Encoding]::UTF8.GetString($buf)
            } finally { $fs.Close() }
            $tail = @(($text -split "`r?`n") | Where-Object { $_ } | Select-Object -Last 200)
        } catch { }
    }
    # [5] recent errors
    $errs = $tail | Select-String -Pattern '"level":\s*"ERROR"' | Select-Object -Last 3
    foreach ($e in $errs) {
        $line = $e.Line
        if ($line.Length -gt 300) { $line = $line.Substring(0, 300) + '...' }
        $r.recent_errors += $line
    }
    if ($errs) { _bump 1 }
    # [6] PG central_store
    $readyIdx = -1; $failedIdx = -1
    for ($i = $tail.Count - 1; $i -ge 0; $i--) {
        if ($readyIdx -lt 0 -and $tail[$i] -match 'PG pool ready')        { $readyIdx = $i }
        if ($failedIdx -lt 0 -and $tail[$i] -match 'PG pool init failed') { $failedIdx = $i }
        if ($readyIdx -ge 0 -and $failedIdx -ge 0) { break }
    }
    if ($readyIdx -lt 0 -and $failedIdx -lt 0) {
        $r.pg_central_store.status = 'unknown'
    } elseif ($readyIdx -gt $failedIdx) {
        $r.pg_central_store.status = 'ready'
    } else {
        $r.pg_central_store.status = 'failed'
        $r.pg_central_store.note = 'See RUNBOOK F9'
        _bump 1
    }

    $r.verdict_label = switch ($r.verdict) { 0 {'GO'} 1 {'DEGRADED'} 2 {'DOWN'} default {'UNKNOWN'} }
    $r | ConvertTo-Json -Depth 6
    if ($NoExit) { return $r.verdict }
    exit $r.verdict
}

# Track worst severity. Helper: bump exit code if worse.
$script:exitCode = 0
$script:hasAuthDevice = $false
function Bump-Exit { param([int]$lvl) if ($lvl -gt $script:exitCode) { $script:exitCode = $lvl } }

Write-Host "==========================================="
Write-Host "  OpenClaw Status Check (6 items)"
Write-Host "==========================================="

# ---- [1/5] Process ----
Write-Host ""
Write-Host "[1/6] Process"
$procs = Get-OpenClawProcesses

if ($procs) {
    foreach ($p in $procs) {
        $tag = Get-OpenClawLaunchTag $p.CommandLine
        Write-Host ("   [OK]   {0}  PID={1}" -f $tag, $p.ProcessId) -ForegroundColor Green
    }
    # Warn if not started via wrapper (no auto-restart)
    $hasWrapper = $procs | Where-Object { $_.CommandLine -match 'service_wrapper\.py' }
    if (-not $hasWrapper) {
        Write-Host "   [WARN] Not started via service_wrapper - no auto-restart on crash" -ForegroundColor Yellow
        Write-Host "          (see RUNBOOK F8) Recommend: migrate.bat" -ForegroundColor DarkYellow
        Bump-Exit 1
    }
} else {
    Write-Host "   [DOWN] No OpenClaw process running" -ForegroundColor Red
    Bump-Exit 2
}

# ---- [2/5] Port listening ----
Write-Host ""
Write-Host "[2/6] Port listening"
$foundPort = $null
foreach ($port in @(8000, 18080)) {
    $listening = netstat -ano | Select-String "LISTENING" | Select-String ":$port "
    if ($listening) {
        foreach ($line in $listening) {
            $parts = $line.Line -split '\s+' | Where-Object { $_ }
            $localAddr = $parts[1]
            $pidVal = $parts[-1]
            $bindNote = if ($localAddr -match '^127\.0\.0\.1:') { ' (loopback only - LAN unreachable, see F1)' }
                        elseif ($localAddr -match '^0\.0\.0\.0:') { ' (LAN reachable)' }
                        else { '' }
            $color = if ($localAddr -match '^127\.0\.0\.1:') { 'Yellow' } else { 'Green' }
            Write-Host ("   [OK]   :{0}  PID={1}  bind={2}{3}" -f $port, $pidVal, $localAddr, $bindNote) -ForegroundColor $color
            if (-not $foundPort) { $foundPort = $port }
            if ($localAddr -match '^127\.0\.0\.1:') { Bump-Exit 1 }
        }
    }
}
if (-not $foundPort) {
    Write-Host "   [DOWN] :8000 / :18080 not listening" -ForegroundColor Red
    Bump-Exit 2
}

# ---- [3/5] /health endpoint ----
Write-Host ""
Write-Host "[3/6] /health endpoint"
foreach ($port in @(8000, 18080)) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 `
                -UseBasicParsing -ErrorAction Stop
        Write-Host ("   [OK]   http://127.0.0.1:{0}/health  ->  {1}" -f $port, $resp.StatusCode) -ForegroundColor Green
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code) {
            Write-Host ("   [DEGRADED] http://127.0.0.1:{0}/health  ->  {1}" -f $port, $code) -ForegroundColor Yellow
            Bump-Exit 1
        }
    }
}

# ---- [4/5] Devices ----
Write-Host ""
Write-Host "[4/6] Devices"
if ($foundPort) {
    try {
        $devicesJson = Invoke-WebRequest -Uri "http://127.0.0.1:$foundPort/devices" `
                       -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        $devices = $devicesJson.Content | ConvertFrom-Json
        $connected = @($devices | Where-Object { $_.status -eq 'connected' }).Count
        $unauth = @($devices | Where-Object { $_.usb_issue -eq 'unauthorized' }).Count
        $total = $devices.Count

        $summaryColor = 'Green'
        $summaryTag = '[OK]   '
        if ($connected -lt $total -and $connected -gt 0) {
            $summaryColor = 'Yellow'
            $summaryTag = '[PART] '
            Bump-Exit 1
        } elseif ($connected -eq 0) {
            $summaryColor = 'Red'
            $summaryTag = '[DOWN] '
            Bump-Exit 2
        }
        Write-Host ("   {0}{1}/{2} connected ({3} unauthorized)" -f $summaryTag, $connected, $total, $unauth) -ForegroundColor $summaryColor

        foreach ($d in $devices) {
            $icon = if ($d.status -eq 'connected') { '[OK  ]' }
                    elseif ($d.usb_issue -eq 'unauthorized') { '[AUTH]'; $script:hasAuthDevice = $true }
                    else { '[DOWN]' }
            $color = if ($d.status -eq 'connected') { 'Green' } else { 'Yellow' }
            Write-Host ("       {0}  {1}  ({2})" -f $icon, $d.display_name, $d.device_id) -ForegroundColor $color
        }
    } catch {
        Write-Host ("   [DOWN] /devices query failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
} else {
    Write-Host "   [SKIP] no listening port, skipped" -ForegroundColor DarkGray
}

# ---- [5/6] Recent errors ----
# Read last 200 lines of logs/openclaw.log once, share with [6/6] below.
Write-Host ""
Write-Host "[5/6] Recent errors (last 200 lines of logs/openclaw.log)"
$logFile = Join-Path $ProjectRoot "logs\openclaw.log"
$tail = @()
if (Test-Path $logFile) {
    try {
        # Open with FileShare.ReadWrite so uvicorn/server can keep writing while we read.
        # Read only the tail (~64KB is enough for 200 JSON lines).
        $fs = [System.IO.File]::Open($logFile, [System.IO.FileMode]::Open,
                                      [System.IO.FileAccess]::Read,
                                      [System.IO.FileShare]::ReadWrite)
        try {
            $tailBytes = [Math]::Min(65536, $fs.Length)
            $fs.Seek(-$tailBytes, [System.IO.SeekOrigin]::End) | Out-Null
            $buf = New-Object byte[] $tailBytes
            [void]$fs.Read($buf, 0, $tailBytes)
            $text = [System.Text.Encoding]::UTF8.GetString($buf)
        } finally {
            $fs.Close()
        }
        $allLines = $text -split "`r?`n" | Where-Object { $_ }
        $tail = @($allLines | Select-Object -Last 200)
        $errs = $tail | Select-String -Pattern '"level":\s*"ERROR"' | Select-Object -Last 3
        if ($errs) {
            Write-Host "   [WARN] last 3 ERROR lines:" -ForegroundColor Yellow
            foreach ($e in $errs) {
                $line = $e.Line
                if ($line.Length -gt 200) { $line = $line.Substring(0, 200) + '...' }
                Write-Host ("       " + $line) -ForegroundColor DarkYellow
            }
            Bump-Exit 1
        } else {
            Write-Host "   [OK]   no ERROR in last 200 lines" -ForegroundColor Green
        }
    } catch {
        Write-Host ("   [SKIP] read log failed: {0}" -f $_.Exception.Message) -ForegroundColor DarkGray
    }
} else {
    Write-Host "   [SKIP] $logFile not found" -ForegroundColor DarkGray
}

# ---- [6/6] L2 central_store PG status (RUNBOOK F9) ----
Write-Host ""
Write-Host "[6/6] L2 central_store PG status"
if ($tail.Count -eq 0) {
    Write-Host "   [SKIP] no log tail to inspect" -ForegroundColor DarkGray
} else {
    # Find latest occurrence of either 'PG pool ready' or 'PG pool init failed'
    $readyIdx = -1
    $failedIdx = -1
    for ($i = $tail.Count - 1; $i -ge 0; $i--) {
        if ($readyIdx -lt 0 -and $tail[$i] -match 'PG pool ready')        { $readyIdx = $i }
        if ($failedIdx -lt 0 -and $tail[$i] -match 'PG pool init failed') { $failedIdx = $i }
        if ($readyIdx -ge 0 -and $failedIdx -ge 0) { break }
    }

    if ($readyIdx -lt 0 -and $failedIdx -lt 0) {
        Write-Host "   [SKIP] no PG status in last 200 lines (central_store maybe disabled)" -ForegroundColor DarkGray
    } elseif ($readyIdx -gt $failedIdx) {
        Write-Host "   [OK]   PG pool ready (most recent)" -ForegroundColor Green
    } elseif ($failedIdx -gt $readyIdx -and $readyIdx -ge 0) {
        Write-Host "   [DEGRADED] PG init failed AFTER ready (RUNBOOK F9 — connection poisoned)" -ForegroundColor Yellow
        Bump-Exit 1
    } else {
        Write-Host "   [DEGRADED] PG init failed, no recovery (RUNBOOK F9)" -ForegroundColor Yellow
        Write-Host "             root fix: PG superuser run 'ALTER ROLE openclaw_app SET lc_messages=''C'';'" -ForegroundColor DarkYellow
        Bump-Exit 1
    }
}

# ---- Summary ----
# ---- (informational) Temporary config TTL scan ----
# 不影响 verdict, 只提醒配置临时改动是否过期 (4-21 VPN gate / 4-26
# enabled_probability 同类潜伏事故的元工程防护). 见 Phase 2 P1.5.
# JSON 模式跳过 (避免污染 -Json 输出); 失败静默 (扫描器是 nice-to-have).
if (-not $Json) {
    try {
        $tempScript = Join-Path $PSScriptRoot "check_temp_configs.ps1"
        if (Test-Path $tempScript) {
            $ttlOut = & $tempScript -Json 2>&1 | Out-String
            $ttlData = $null
            try { $ttlData = $ttlOut | ConvertFrom-Json } catch {}
            if ($ttlData -and $ttlData.expired -gt 0) {
                Write-Host ""
                Write-Host ("[!] {0} expired temporary config marker(s) — run check_temp_configs.ps1 for details" -f $ttlData.expired) -ForegroundColor Red
            } elseif ($ttlData -and $ttlData.soon -gt 0) {
                Write-Host ""
                Write-Host ("[i] {0} temporary config marker(s) due within {1}d — run check_temp_configs.ps1" -f $ttlData.soon, $ttlData.days_ahead) -ForegroundColor Yellow
            }
        }
    } catch {
        # silent — 扫描器异常不该影响 status verdict
    }
}

Write-Host ""
Write-Host "==========================================="
if ($foundPort) {
    Write-Host ("  >> Dashboard: http://localhost:{0}/dashboard" -f $foundPort) -ForegroundColor Cyan
    Write-Host "  >> Use 'localhost' NOT 192.168.x.x   (see RUNBOOK F1)" -ForegroundColor DarkCyan
} else {
    Write-Host "  >> Service is down. Run start.bat" -ForegroundColor Yellow
}
$verdict = switch ($script:exitCode) {
    0 { 'GO       all 5 items healthy' }
    1 { 'DEGRADED service up with warnings' }
    2 { 'DOWN     critical failure' }
}
$verdictColor = switch ($script:exitCode) { 0 { 'Green' } 1 { 'Yellow' } 2 { 'Red' } }
Write-Host ("  >> Verdict: [{0}] {1}" -f $script:exitCode, $verdict) -ForegroundColor $verdictColor
Write-Host "==========================================="

# -Open: open dashboard in default browser (only if service is up)
if ($Open -and $foundPort -and $script:exitCode -lt 2) {
    $url = "http://localhost:$foundPort/dashboard"
    Write-Host "Opening browser: $url" -ForegroundColor Cyan
    Start-Process $url -ErrorAction SilentlyContinue
}

# -Beep: audible alert if degraded/down or any AUTH device
if ($Beep -and ($script:exitCode -gt 0 -or $script:hasAuthDevice)) {
    try {
        # 800Hz / 200ms; repeat 2x for AUTH/DEGRADED, 3x for DOWN
        $reps = if ($script:exitCode -ge 2) { 3 } else { 2 }
        for ($i = 0; $i -lt $reps; $i++) {
            [Console]::Beep(800, 200)
            Start-Sleep -Milliseconds 100
        }
    } catch {
        # Beep not available (e.g. no console) — silently ignore
    }
}

if ($NoExit) { return $script:exitCode }
exit $script:exitCode
