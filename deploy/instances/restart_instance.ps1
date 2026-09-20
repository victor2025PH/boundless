# restart_instance.ps1 — safe single-instance restart for chengjie dual deploy
# (2026-07-22/23: root-cause fix for workbench load-timeout loops)
#
# Phase2 additions:
#   - Machine-shared cooldown (SYSTEM watchdog + human share one file)
#   - Dirty-tree gate: refuse restart when only hot-reloadable files are dirty
#   - Optional ops alert on success/fail (EVENT_INGEST_KEY, same relay as watchdog)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao -Reason "batch: <what>"
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance tongyi -Force -Reason "<why>"
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao -DryRun
#   powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance zhiliao -Advise
#   -Reason is REQUIRED for manual restarts since 2026-09-19 (watchdog/Advise/DryRun exempt).
#
# Exit: 0=ready (or Advise/DryRun ok)  1=fail/cooldown/hot-only/no-reason  2=probe/config fault
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
    [switch]$FromWatchdog,
    # P1-3 (2026-08-18): after DONE, run the read-only load-verification probe
    # (tools/post_restart_probe.py). REPORT-ONLY: probe failures never change
    # the restart result/exit code (restart already succeeded; a red probe row
    # means some expected route/endpoint is missing -> yellow warning to read).
    [switch]$Probe
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

# Reason gate (2026-09-19): 61 zhiliao restarts in 19 days (28 manual, 5 on 09-18 alone) and
# every seat sees a 20-40s outage banner per restart. Entries logged with the default reason
# ("restart_instance", e.g. 09-19 12:23) make restart_events.jsonl useless for the weekly
# "why are we restarting so much" review. Manual/agent callers MUST say what they are loading.
# -Force does NOT bypass this (a reason costs nothing); watchdog/Advise/DryRun are exempt.
if (-not $FromWatchdog -and -not $Advise -and -not $DryRun -and
    ([string]$Reason).Trim() -in @('', 'restart_instance')) {
    Write-Host ("[restart-{0}] -Reason is required for manual restarts. Say WHAT you are loading, e.g." -f $Instance) -ForegroundColor Red
    Write-Host ('   -Reason "batch: <feature/fix>; piggyback: <sibling batches>"') -ForegroundColor Yellow
    Write-Host '   (reason lands in restart_events.jsonl + ops alert + the blue maintenance banner seats see)' -ForegroundColor DarkGray
    exit 1
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
$script:InflightMarked = $false
function Fail([string]$msg) {
    if ($script:InflightMarked) {
        Clear-RestartInflight $Instance
        $script:InflightMarked = $false
    }
    Say $msg 'Red'
    exit 1
}

# Q-14 D-3 (#262, 2026-09-10): one line at the start and one at the end of a zhiliao restart
# into .ops\prod_tunnel.log, next to the tunnel's own "dropped / bind failed" lines, so the
# 502 window on katie can be reconciled against a deliberate local restart instead of being
# read as tunnel death (09-09 the edge watchdog restarted a healthy tunnel twice for this).
# zhiliao only: that is the instance behind the 18799 -R forward. Never blocks the restart.
function Note-TunnelLog([string]$m) {
    if ($Instance -ne 'zhiliao') { return }
    try {
        $tl = Join-Path $ProdBase '.ops\prod_tunnel.log'
        ("{0} [restart_instance] {1}" -f (Get-Date -Format s), $m) | Out-File $tl -Append -Encoding utf8
    } catch {}
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
    Write-Host '  Before a real restart (shared-tree GO/NO-GO; never restarts itself):'
    Write-Host ("    powershell -ExecutionPolicy Bypass -File engines\chengjie\scripts\restart_preflight.ps1 -Instance {0}" -f $Instance)
    Write-Host '  Declare the batch so siblings can piggyback (and -Done when finished):'
    Write-Host '    powershell -ExecutionPolicy Bypass -File engines\chengjie\scripts\agent_probe.ps1 -Intent "batch: <what>"'
    Write-Host '  Command (only after preflight GO; -Reason is mandatory):'
    Write-Host ("    powershell -ExecutionPolicy Bypass -File deploy\instances\restart_instance.ps1 -Instance {0} -Reason `"batch: <what>`"" -f $Instance)
    Write-Host '  Forbidden: scripts\restart_main.ps1 , mass taskkill , bare python main.py at engine root'
    Write-Host ("  Now: port={0} listening={1} login_ready={2}" -f $effPort, $listenTxt, $loginTxt)
    Write-Host ("  Dirty gate: kind={0} py={1} hot={2} other={3}" -f $dirty.kind, $dirty.py.Count, $dirty.hot.Count, $dirty.other.Count)
    $adviseDupTool = Join-Path $EngineDir 'tools\check_config_duplicates.py'
    if (Test-Path $adviseDupTool) {
        $adviseDup = & python $adviseDupTool --data-root $root 2>&1
        if ($LASTEXITCODE -eq 1) {
            Say ("config duplicate keys - restart WILL be refused (fix first):`n{0}" -f (($adviseDup | Out-String).Trim())) 'Red'
        } else {
            Write-Host '  Config keys: no duplicates'
        }
    }
    if ($gate.active) {
        Say ("cooldown ACTIVE left~{0}m reason={1}" -f $gate.left_min, $gate.record.reason) 'DarkYellow'
    } elseif ($gate.record) {
        Say ("last restart: {0} reason={1}" -f $gate.record.ts, $gate.record.reason) 'DarkYellow'
    }
    exit 0
}

# Config duplicate-key gate (2026-07-31, after a real incident): PyYAML takes the
# LAST of duplicate keys silently, so hand-inserting a second `telegram:` under
# `platform_login:` discarded the original block (protocol_enabled /
# companion_runtime / credpool token / device fingerprint) -> after restart the
# orchestrator registered no Telegram worker -> 5 accounts offline. Nothing errors,
# nothing logs; only a restart makes it real. So check BEFORE we stop anything.
# Fail-open on tooling problems: a guard must never be more dangerous than the
# thing it guards. Only an actual duplicate blocks (override: -Force).
$dupTool = Join-Path $EngineDir 'tools\check_config_duplicates.py'
if (Test-Path $dupTool) {
    $dupOut = & python $dupTool --data-root $root 2>&1
    $dupRc = $LASTEXITCODE
    if ($dupRc -eq 1) {
        $dupTxt = ($dupOut | Out-String).Trim()
        if (-not $Force) {
            Fail ("Duplicate keys in this instance's config - PyYAML silently keeps the LAST one, so an entire earlier block is being discarded. Fix the config first (restarting would make it real). Override: -Force.`n{0}" -f $dupTxt)
        }
        Say ("config duplicate keys present but -Force given:`n{0}" -f $dupTxt) 'DarkYellow'
    } elseif ($dupRc -ne 0) {
        Say ("config duplicate-key check could not run (rc={0}) - continuing" -f $dupRc) 'DarkYellow'
    }
}

# Dirty-tree gate: only hot-reloadable dirt → refuse (unless Force / AllowHotOnly)
if (-not $Force -and -not $AllowHotOnly -and $dirty.kind -eq 'hot_only') {
    Fail ("Hot-only dirty tree ({0} files). Refresh browser / wait hot-reload instead. Override: -AllowHotOnly or -Force. Sample: {1}" -f $dirty.hot.Count, (($dirty.hot | Select-Object -First 3) -join ', '))
}

$gate = Test-RestartCooldownActive -Instance $Instance -CooldownMin $CooldownMin
if ($gate.active -and -not $Force) {
    Fail ("Cooldown: last restart {0} min ago (reason={1}). Wait ~{2} min more, or -Force for emergency. Workbench load-timeout loops are usually restart flaps." -f $gate.elapsed_min, $gate.record.reason, $gate.left_min)
}

# Manual restart spacing (2026-07-31, root-cause fix for the workbench "connection
# lost" banner storms): the 10min machine cooldown still allowed 14-16 restarts/day
# (measured 07-28..07-31 in restart_events.jsonl), and EVERY restart burns a
# 15-30s all-site outage window on the seats. Manual callers now need >=30min
# since the LAST restart of this instance - BATCH your .py changes with the other
# agent lines and restart once. Watchdog self-heal keeps its own semantics
# (-FromWatchdog implies -Force and never reaches this gate). Note: the seat
# quiet_poll window still follows the 10min cooldown record - this gate only
# spaces HUMAN/agent restarts, it does not slow the workbench for 30min.
$ManualSpacingMin = 30
$SpacingBlocked = $false
$SpacingElapsedMin = -1
if (-not $FromWatchdog -and $gate.record -and $gate.record.ts) {
    try {
        $lastRestartTs = [datetime]::Parse([string]$gate.record.ts, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
        $SpacingElapsedMin = [int](((Get-Date) - $lastRestartTs).TotalMinutes)
        if ($SpacingElapsedMin -ge 0 -and $SpacingElapsedMin -lt $ManualSpacingMin) { $SpacingBlocked = $true }
    } catch {}
}
if ($SpacingBlocked -and -not $Force) {
    Fail ("Restart spacing: last restart was {0} min ago (reason={1}); manual restarts need >={2} min apart - every restart shows every seat a 15-30s outage banner. BATCH your changes (scripts\agent_probe.ps1 lists other lines' pending work) and restart once. Real emergency: -Force (event log will carry a [forced] flag)." -f $SpacingElapsedMin, $gate.record.reason, $ManualSpacingMin)
}
if ($Force -and -not $FromWatchdog -and ($gate.active -or $SpacingBlocked)) {
    # Forced through an active gate: make it auditable in the cooldown record +
    # restart_events.jsonl + ops alert text. The 12:30/12:32 double restart FLAP
    # of 2026-07-31 was exactly this path and was invisible until now.
    $Reason = "$Reason [forced]"
    Say ("FORCED through restart gate (cooldown/spacing) - event log flagged: {0}" -f $Reason) 'Yellow'
}

if ($DryRun) {
    Say 'DryRun - would run:' 'Yellow'
    Write-Host ("  0) POST 127.0.0.1:{0}/api/internal/ops/maintenance-notice (blue maintenance banner on open workbenches)" -f $effPort)
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

# Ghost-instance guard (2026-07-26): refuse when another orchestrator is
# mid-restart (fresh inflight sentinel). -Force = operator override.
$inflight = Test-RestartInflight $Instance
if ($inflight.active -and -not $Force) {
    Fail ("Another restart of {0} is in flight (age={1}s reason={2} pid={3}). Wait for it to finish; -Force only if you are sure it died." -f $Instance, $inflight.age_sec, $inflight.record.reason, $inflight.record.script_pid)
}
# Mark the stop->bind window so the watchdog DOWN path does not double-start
# (a second engine on the same data root loses the port bind but its Telegram
# client still runs = session-fighting ghost).
$null = Write-RestartInflight -Instance $Instance -Reason $Reason -TtlSec ($ReadyWaitSec + 120)
$script:InflightMarked = $true

# Pre-stop maintenance broadcast (2026-07-31): tell every OPEN workbench page the
# coming connection failures are a PLANNED window BEFORE the port dies - the inbox
# banner shows the blue "maintenance window" text instead of the red "connection
# lost" one, and polling backs off. Old behavior: the cooldown record was written
# AFTER the restart and the status API is unreachable during the outage, so seats
# could never learn "this is maintenance" in time (red banner every restart).
# Best-effort by design: a hung/dead instance cannot be told (the banner then
# honestly shows red), and the restart must never be blocked by its own announce.
if ($listenPids.Count) {
    $noticeSec = [Math]::Min(900, [Math]::Max(60, $ReadyWaitSec + 30))
    try {
        $noticeBody = @{ window_sec = $noticeSec; reason = $Reason } | ConvertTo-Json -Compress
        # Bearer header = "programmatic client" marker so the CSRF middleware admits the
        # POST (it rightly rejects bare cross-site-shaped writes). Authorization itself
        # is enforced by the route's loopback check - this value is an identifier, not
        # a secret.
        Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/internal/ops/maintenance-notice" -f $effPort) `
            -Method Post -ContentType 'application/json' -Body $noticeBody -TimeoutSec 3 `
            -Headers @{ Authorization = 'Bearer restart-orchestrator' } | Out-Null
        Say ("maintenance notice broadcast to open workbenches (window<={0}s)" -f $noticeSec)
        # Pre-stop grace (2026-09-19, was 2s): the inbox SSE client reconnects with a 6s backoff
        # after any stream error, and /api/workspace/stream replays the last 30 bus events to a
        # fresh subscriber. A 2s grace skipped every page that happened to be inside that backoff
        # (or a throttled background tab) -> those seats never learned "this is maintenance" and
        # rendered the red "server unreachable" banner for a planned restart (09-19 12:23 case).
        # 8s covers one full reconnect cycle + replay. Seats are still fully served during the
        # grace; only the operator waits 6s longer.
        Say 'pre-stop grace 8s (covers one SSE reconnect cycle so late/reconnecting tabs get the notice)...'
        Start-Sleep -Seconds 8
    } catch {
        Say ("maintenance notice skipped ({0})" -f $_.Exception.Message) 'DarkYellow'
    }
}

$t0 = Get-Date
Note-TunnelLog ("BEGIN restart port={0} reason={1} pid={2} - local :{0} will refuse for the window; tunnel_http!=200 in this span is the instance, not the tunnel" -f $meta.port, $Reason, $PID)

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
# 透传 -InstanceId：start_*.ps1 据此注入 AITR_INSTANCE_ID（托管 AI 按实例计量）
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -DataDir $root -InstanceId $Instance
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
Note-TunnelLog ("END restart port={0} ready={1} window={2}s" -f $effPort, $ready, $sec)
if (-not $ready) {
    Send-RestartAlert ("⛔ {0} restarted but /login not ready after {1}s ({2})" -f $meta.name, $sec, $env:COMPUTERNAME)
    Fail ("Not ready after {0}s (/login). Check {1}\logs\ latest boot_*.out.log; keep agents off the workbench." -f $sec, $root)
}

Clear-RestartInflight $Instance
$script:InflightMarked = $false
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
# P1-3: optional load-verification probe (report-only; never changes exit code).
if ($Probe) {
    $engine = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'engines\chengjie'
    $probePy = Join-Path $engine 'tools\post_restart_probe.py'
    if (Test-Path $probePy) {
        Say 'running post-restart load-verification probe (read-only)...' 'Cyan'
        & python -X utf8 $probePy --base ("http://127.0.0.1:{0}" -f $effPort) --data-root $root
        if ($LASTEXITCODE -ne 0) {
            Say ("PROBE REPORTED ISSUES (exit {0}) - restart itself succeeded; read the FAIL rows above (missing route = code not on disk before restart / registration error)." -f $LASTEXITCODE) 'Yellow'
        } else {
            Say 'probe: all expected routes/endpoints answering.' 'Green'
        }
    } else {
        Say ("probe script missing: {0}" -f $probePy) 'Yellow'
    }
}
exit 0
