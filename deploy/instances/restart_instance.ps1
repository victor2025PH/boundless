# restart_instance.ps1 — safe single-instance restart for chengjie dual deploy
# (2026-07-22/23: root-cause fix for workbench load-timeout loops)
#
# Phase2 additions:
#   - Machine-shared cooldown (SYSTEM watchdog + human share one file)
#   - Dirty-tree gate: refuse restart when only hot-reloadable files are dirty
#   - Optional ops alert on success/fail (EVENT_INGEST_KEY, same relay as watchdog)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance tongyi -Force
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao -DryRun
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao -Advise
#
# Exit: 0=ready (or Advise/DryRun ok)  1=fail/cooldown/hot-only  2=probe/config fault
# ASCII-only messages (PS 5.1 GBK decode pit).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('zhiliao', 'tongyi')]
    [string]$Instance,

    [string]$DataDir = '',
    [int]$CooldownMin = 10,
    # Heavy engine cold-start (Telegram login + voice preheat + KB) serves /login 200
    # only after full init - observed ~2-3.5min. 90s falsely failed the restart AND
    # skipped writing the cooldown (flap risk). 210s covers the cold-start tail.
    [int]$ReadyWaitSec = 210,
    [string]$Reason = 'restart_instance',
    # Alert if restart window (stop->/login ready) exceeds this. 180s = above a normal
    # heavy cold start; fires only on genuinely abnormal slowness (60s always breached).
    [int]$WindowSlaSec = 180,
    [switch]$Force,              # skip cooldown + hot-only gate
    [switch]$AllowHotOnly,       # allow restart even if only templates/i18n dirty
    [switch]$DryRun,
    [switch]$Advise,
    [switch]$SkipReadyWait,
    [switch]$NoAlert,            # skip ops alert relay
    # Watchdog caller: implies Force+AllowHotOnly+NoAlert (watchdog sends its own alert)
    [switch]$FromWatchdog
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

. (Join-Path $PSScriptRoot '_restart_cooldown.ps1')

if ($FromWatchdog) {
    $Force = $true
    $AllowHotOnly = $true
    $NoAlert = $true
    if ($Reason -eq 'restart_instance') { $Reason = 'watchdog' }
}

$PortMap = @{
    zhiliao = @{ port = 18799; alt = 18787; start = 'start_zhiliao.ps1'; name = 'ChatX/zhiliao' }
    tongyi  = @{ port = 18899; alt = 18887; start = 'start_tongyi.ps1';  name = 'LingoX/tongyi' }
}
$meta = $PortMap[$Instance]
$ProdBase = 'D:\chengjie-instances'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EngineDir = Join-Path $RepoRoot 'engines\chengjie'

function Say([string]$m, [string]$color = 'Gray') {
    Write-Host ("[restart-{0}] {1}" -f $Instance, $m) -ForegroundColor $color
}
function Fail([string]$msg) {
    Say $msg 'Red'
    exit 1
}

function Send-RestartAlert([string]$text) {
    if ($NoAlert) { return }
    $key = [Environment]::GetEnvironmentVariable('EVENT_INGEST_KEY', 'Machine')
    if (-not $key) { $key = [string]$env:EVENT_INGEST_KEY }
    if (-not $key) { return }
    $base = [Environment]::GetEnvironmentVariable('PERSONA_SYNC_BASE', 'Machine')
    if (-not $base) { $base = [string]$env:PERSONA_SYNC_BASE }
    if (-not $base) { $base = 'https://bd2026.cc' }
    try {
        $body = @{ text = $text; source = ("restart_instance@{0}" -f $env:COMPUTERNAME) } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri ($base.TrimEnd('/') + '/api/ops/alert') -Method Post `
            -Headers @{ Authorization = ("Bearer {0}" -f $key) } -ContentType 'application/json' `
            -Body $body -TimeoutSec 15 | Out-Null
        Say 'ops alert sent'
    } catch {
        Say ("ops alert failed: {0}" -f $_.Exception.Message) 'DarkYellow'
    }
}

function Get-PortHolders([int]$port) {
    @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Sniff-DataRoot([object[]]$pids) {
    foreach ($holderPid in @($pids)) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$holderPid" -ErrorAction SilentlyContinue
        for ($hop = 0; ($hop -lt 2) -and $p; $hop++) {
            if ($p.CommandLine -match 'AITR_DATA_DIR=([^"&]+)') {
                $root = $Matches[1].Trim().TrimEnd('\')
                if ($root) { return $root }
            }
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        }
    }
    return $null
}

function Resolve-DataRoot {
    if ($DataDir) {
        return [IO.Path]::GetFullPath($DataDir).TrimEnd('\')
    }
    $pids = Get-PortHolders ([int]$meta.port)
    if (-not $pids.Count) { $pids = Get-PortHolders ([int]$meta.alt) }
    $root = Sniff-DataRoot $pids
    if ($root) { return $root }
    $prod = Join-Path $ProdBase (Join-Path $Instance 'data')
    if (Test-Path (Join-Path $prod 'config\config.yaml')) { return $prod }
    $repo = Join-Path $PSScriptRoot (Join-Path $Instance 'data')
    if (Test-Path (Join-Path $repo 'config\config.yaml')) { return $repo }
    return $null
}

function Test-LoginReady([int]$port) {
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/login" -f $port) -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return ([int]$r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

$root = Resolve-DataRoot
if (-not $root) {
    Fail ("Cannot resolve data root. Pass -DataDir (prod often: {0}\{1}\data)" -f $ProdBase, $Instance)
}
if (-not (Test-Path (Join-Path $root 'config\config.yaml'))) {
    Fail ("Bad data root (missing config\config.yaml): {0}" -f $root)
}

$listenPids = Get-PortHolders ([int]$meta.port)
$effPort = [int]$meta.port
if (-not $listenPids.Count) {
    $altPids = Get-PortHolders ([int]$meta.alt)
    if ($altPids.Count) { $effPort = [int]$meta.alt; $listenPids = $altPids }
}

$cdPaths = Get-RestartCooldownPath $Instance
$dirty = Get-ChengjieDirtyRestartAdvice -EngineDir $EngineDir

Say ("target={0} port={1} data_root={2}" -f $meta.name, $effPort, $root) 'Cyan'
Say ("cooldown_file={0}" -f $cdPaths.primary) 'DarkGray'

if ($Advise) {
    $listenTxt = if ($listenPids.Count) { "yes PID=$($listenPids -join ',')" } else { 'no' }
    $loginTxt = if (Test-LoginReady $effPort) { 'yes' } else { 'no' }
    $gate = Test-RestartCooldownActive -Instance $Instance -CooldownMin $CooldownMin
    Say '--- restart advice (no changes) ---' 'Yellow'
    Write-Host '  Skip restart (refresh browser / hot-reload):'
    Write-Host '    - src/web/templates/**.html'
    Write-Host '    - src/web/web_i18n.py and src/web/i18n_packs/**'
    Write-Host '    - instance config/config.local.yaml (most ops flags, ~30s)'
    Write-Host '  Must restart (business .py / boot path):'
    Write-Host '    - engines/chengjie/src/**/*.py , main.py , bootstrap'
    Write-Host '  Command:'
    Write-Host ("    powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance {0}" -f $Instance)
    Write-Host '  Forbidden: scripts\restart_main.ps1 , mass taskkill , bare python main.py at engine root'
    Write-Host ("  Now: port={0} listening={1} login_ready={2}" -f $effPort, $listenTxt, $loginTxt)
    Write-Host ("  Dirty gate: kind={0} py={1} hot={2} other={3}" -f $dirty.kind, $dirty.py.Count, $dirty.hot.Count, $dirty.other.Count)
    if ($gate.active) {
        Say ("cooldown ACTIVE left~{0}m reason={1}" -f $gate.left_min, $gate.record.reason) 'DarkYellow'
    } elseif ($gate.record) {
        Say ("last restart: {0} reason={1}" -f $gate.record.ts, $gate.record.reason) 'DarkYellow'
    }
    exit 0
}

# Dirty-tree gate: only hot-reloadable dirt → refuse (unless Force / AllowHotOnly)
if (-not $Force -and -not $AllowHotOnly -and $dirty.kind -eq 'hot_only') {
    Fail ("Hot-only dirty tree ({0} files). Refresh browser / wait hot-reload instead. Override: -AllowHotOnly or -Force. Sample: {1}" -f $dirty.hot.Count, (($dirty.hot | Select-Object -First 3) -join ', '))
}

$gate = Test-RestartCooldownActive -Instance $Instance -CooldownMin $CooldownMin
if ($gate.active -and -not $Force) {
    Fail ("Cooldown: last restart {0} min ago (reason={1}). Wait ~{2} min more, or -Force for emergency. Workbench load-timeout loops are usually restart flaps." -f $gate.elapsed_min, $gate.record.reason, $gate.left_min)
}

if ($DryRun) {
    Say 'DryRun - would run:' 'Yellow'
    Write-Host ("  1) stop_instance.ps1 -Instance {0}" -f $Instance)
    Write-Host ("  2) clear {0}\logs\run_sentinel.json" -f $root)
    Write-Host ("  3) {0} -DataDir {1}" -f $meta.start, $root)
    if (-not $SkipReadyWait) {
        Write-Host ("  4) poll http://127.0.0.1:{0}/login until 200 (<= {1}s)" -f $effPort, $ReadyWaitSec)
    }
    Write-Host ("  5) write cooldown reason={0}" -f $Reason)
    Write-Host ("  cooldown file: {0}" -f $cdPaths.primary)
    Write-Host ("  dirty gate: {0}" -f $dirty.kind)
    exit 0
}

$t0 = Get-Date

$stopScript = Join-Path $PSScriptRoot 'stop_instance.ps1'
Say 'stopping instance...' 'Yellow'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopScript -Instance $Instance
if ($LASTEXITCODE -ne 0) {
    Send-RestartAlert ("⛔ {0} restart FAILED at stop (rc={1}, {2})" -f $meta.name, $LASTEXITCODE, $env:COMPUTERNAME)
    Fail ("stop_instance failed exit={0}" -f $LASTEXITCODE)
}

$sentinel = Join-Path $root 'logs\run_sentinel.json'
if (Test-Path $sentinel) {
    Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
    Say 'cleared run_sentinel.json (expected restart)'
}

$startScript = Join-Path $PSScriptRoot $meta.start
Say ("starting {0}..." -f $meta.name) 'Yellow'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -DataDir $root
if ($LASTEXITCODE -ne 0) {
    Send-RestartAlert ("⛔ {0} restart FAILED at start (rc={1}, {2})" -f $meta.name, $LASTEXITCODE, $env:COMPUTERNAME)
    Fail ("start failed exit={0}" -f $LASTEXITCODE)
}

$ready = $false
if ($SkipReadyWait) {
    $ready = $true
    Say 'skipped ready wait (-SkipReadyWait)' 'DarkYellow'
} else {
    Say ("waiting for /login ready (<= {0}s)..." -f $ReadyWaitSec)
    $deadline = (Get-Date).AddSeconds($ReadyWaitSec)
    do {
        foreach ($p in @([int]$meta.port, [int]$meta.alt)) {
            if (Test-LoginReady $p) { $effPort = $p; $ready = $true; break }
        }
        if ($ready) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
}

$sec = [int]((Get-Date) - $t0).TotalSeconds
if (-not $ready) {
    Send-RestartAlert ("⛔ {0} restarted but /login not ready after {1}s ({2})" -f $meta.name, $sec, $env:COMPUTERNAME)
    Fail ("Not ready after {0}s (/login). Check {1}\logs\ latest boot_*.out.log; keep agents off the workbench." -f $sec, $root)
}

$written = Write-RestartCooldown -Instance $Instance -DataRoot $root -Port $effPort -Reason $Reason -CooldownMin $CooldownMin -WindowSec $sec
$cdPath = if ($written -is [hashtable]) { [string]$written.path } else { [string]$written }
$flap = if ($written -is [hashtable]) { $written.flap } else { $null }
Say ("DONE - {0} ready http://127.0.0.1:{1}/login  window={2}s (SLA {3}s)" -f $meta.name, $effPort, $sec, $WindowSlaSec) 'Green'
Say ("cooldown written: {0}" -f $cdPath) 'Green'
Say 'Agents may refresh the workbench now. Do not restart again inside the cooldown window.' 'Green'
$slaBreach = ($WindowSlaSec -gt 0 -and $sec -gt $WindowSlaSec)
if ($slaBreach) {
    Say ("SLA BREACH: window {0}s > {1}s. Seat was unavailable too long; check cold-start / boot logs." -f $sec, $WindowSlaSec) 'Yellow'
    Send-RestartAlert ("⏱️ {0} restart SLOW: {1}s > SLA {2}s (reason={3}, {4}). Seat down too long; check cold-start." -f $meta.name, $sec, $WindowSlaSec, $Reason, $env:COMPUTERNAME)
} else {
    Send-RestartAlert ("⚠️ {0} restarted OK in {1}s (reason={2}, port={3}, {4}). Cooldown {5}m." -f $meta.name, $sec, $Reason, $effPort, $env:COMPUTERNAME, $CooldownMin)
}
# Phase6: flap = >=2 restarts of this instance in 30m (root cause of seat load-timeout storms)
if ($flap -and $flap.flapping) {
    $rs = @($flap.reasons) -join ','
    Say ("FLAP DETECTED: {0} restarts in {1}m (threshold {2}) reasons={3}" -f $flap.count, $flap.window_min, $flap.threshold, $rs) 'Yellow'
    Send-RestartAlert ("🚨 {0} RESTART FLAP: {1}x in {2}m on {3} (reasons={4}). Stop killing; wait cooldown; check boot logs." -f $meta.name, $flap.count, $flap.window_min, $env:COMPUTERNAME, $rs)
}
exit 0
