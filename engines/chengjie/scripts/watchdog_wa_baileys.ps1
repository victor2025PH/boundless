# watchdog_wa_baileys.ps1 - business-level watchdog for the WhatsApp Baileys Node service (:8790)
#
# WHY: 2026-07-22 incident - after a network drop at 14:26 the Node process stayed alive and
#   :8790/health kept returning {ok:true}, but GET /accounts returned an empty list (all
#   sessions expired). A port/health probe cannot catch this failure mode; only comparing the
#   ORCHESTRATOR-EXPECTED account set against the Node-side ACTUAL account set can. The outage
#   went unnoticed for ~2 hours.
#
# WHAT: every run (scheduled task, 5 min cadence):
#   1. GET <main>/api/accounts/orchestrator (Bearer web_admin.auth_token) -> expected set =
#      accounts[] with platform=whatsapp. Main site unreachable -> silently exit 0 (the main
#      site has its own watchdog; we do not overstep).
#   2. GET <node>/accounts (3s timeout) -> actual set. Any expected account missing (or the
#      port down/timing out) counts one miss in the state file; all present -> streak reset.
#   3. missStreak >= MissLimit (2 rounds ~ 10 min) -> self-heal:
#        gentle : POST <node>/accounts/<id>/reconnect for each missing account (endpoint may
#                 not exist yet -> 404 tolerated), wait 20s, re-check /accounts;
#        hard   : still missing -> kill the node.exe owning port 8790 (verified by process
#                 name AND command line containing whatsapp-baileys\server.js - never kill a
#                 foreign process) + leftover baileys node processes, then
#                 schtasks /Run /TN "WhatsApp-Baileys-Service"; 30 min restart cooldown
#                 (inside cooldown: alert only, no restart).
#   4. Every self-heal action (reconnect attempt / hard restart / cooldown skip / refusal to
#      kill a foreign port owner) POSTs an alert to the GROUP TG RELAY on the VPS
#      (<AlertBase>/api/ops/alert, Bearer = EVENT_INGEST_KEY machine env) - the SAME relay
#      watchdog_instances.ps1 / src/ops/ops_alert.py use. NOTE the main site (:18799) has NO
#      /api/ops/alert route; posting there would 404 silently. No key -> log-only, never fail.
#
# State:  logs\watchdog_wa_baileys.state.json   {missStreak, lastRestartTs}
# Log:    logs\watchdog_wa_baileys.log          (rotates at >2MB, keeps one .1 copy)
#
# Manual runs:
#   probe only : powershell -ExecutionPolicy Bypass -File scripts\watchdog_wa_baileys.ps1 -DryRun
#   normal     : powershell -ExecutionPolicy Bypass -File scripts\watchdog_wa_baileys.ps1
#
# -DryRun probes and prints every decision but performs NO self-heal, NO alerts and NO state
# writes (safe to run while the scheduled task is active).
#
# Exit code is always 0 (scheduled-task semantics).
#
# NOTE: ASCII-only on purpose (PowerShell 5.1 decodes BOM-less UTF-8 as GBK and corrupts CJK
#   literals; same convention as watchdog_emotion_tts.ps1).

param(
    [string]$MainBase           = "http://127.0.0.1:18799",
    [string]$NodeBase           = "http://127.0.0.1:8790",
    # alert relay = VPS group TG channel (same as watchdog_instances.ps1); NOT the main site
    [string]$AlertBase          = "",
    [string]$IngestKey          = "",
    [string]$DataDir            = "D:\chengjie-instances\zhiliao\data",
    [string]$TaskName           = "WhatsApp-Baileys-Service",
    [int]   $MissLimit          = 2,
    [int]   $RestartCooldownMin = 30,
    [int]   $ReconnectWaitSec   = 20,
    [int]   $MainTimeoutSec     = 5,
    [int]   $NodeTimeoutSec     = 3,
    [switch]$DryRun,
    [string]$LogPath            = "$PSScriptRoot\..\logs\watchdog_wa_baileys.log",
    [string]$StatePath          = "$PSScriptRoot\..\logs\watchdog_wa_baileys.state.json"
)

$ErrorActionPreference = "SilentlyContinue"
$NodePort = ([Uri]$NodeBase).Port

# resolve alert relay + key (param > env; machine-level env is inherited by scheduled tasks)
if (-not $AlertBase) { $AlertBase = [string]$env:PERSONA_SYNC_BASE }
if (-not $AlertBase) { $AlertBase = "https://bd2026.cc" }
$AlertBase = $AlertBase.TrimEnd("/")
if (-not $IngestKey) { $IngestKey = [string]$env:EVENT_INGEST_KEY }

# -- log / state helpers -----------------------------------------------------
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
try {
    if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt 2MB)) {
        Move-Item -Force $LogPath ($LogPath + ".1")
    }
} catch {}

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Read-State {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        return @{ missStreak = [int]$s.missStreak; lastRestartTs = [double]$s.lastRestartTs }
    } catch {
        return @{ missStreak = 0; lastRestartTs = 0.0 }
    }
}

function Save-State($st) {
    if ($DryRun) { return }
    try { ($st | ConvertTo-Json -Compress) | Set-Content -Path $StatePath } catch {}
}

function Get-Epoch { return [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

# -- main-site access (token, expected set, alerts) --------------------------
function Get-AuthToken {
    # same resolution as services/whatsapp-baileys/start.ps1::Get-CfgToken:
    # overlay (config.local.yaml) first, then config.yaml; last auth_token line wins.
    $files = @(
        (Join-Path $DataDir "config\config.local.yaml"),
        (Join-Path $DataDir "config\config.yaml")
    )
    foreach ($f in $files) {
        if (Test-Path $f) {
            $m = Select-String -Path $f -Pattern '^\s*auth_token:\s*(\S+)' | Select-Object -Last 1
            if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"').Trim("'") }
        }
    }
    return ""
}

function Get-ExpectedAccounts([string]$token) {
    # expected set = orchestrator-managed whatsapp account_ids. @{ok; ids; err}
    try {
        $r = Invoke-RestMethod -Uri "$MainBase/api/accounts/orchestrator" `
            -Headers @{ Authorization = "Bearer $token" } `
            -TimeoutSec $MainTimeoutSec -ErrorAction Stop
        if (-not $r -or -not $r.ok) { return @{ ok = $false; err = "response not ok" } }
        $ids = @()
        foreach ($a in @($r.accounts)) {
            if ("$($a.platform)" -eq "whatsapp" -and "$($a.account_id)" -ne "") {
                $ids += "$($a.account_id)"
            }
        }
        return @{ ok = $true; ids = @($ids | Select-Object -Unique) }
    } catch {
        return @{ ok = $false; err = "$($_.Exception.Message)" }
    }
}

function Get-NodeAccounts {
    # actual set = authorized accounts reported by the Node service. @{ok; ids; err}
    try {
        $r = Invoke-RestMethod -Uri "$NodeBase/accounts" -TimeoutSec $NodeTimeoutSec -ErrorAction Stop
        $ids = @()
        foreach ($a in @($r.accounts)) {
            if ("$($a.account_id)" -ne "") { $ids += "$($a.account_id)" }
        }
        return @{ ok = $true; ids = @($ids | Select-Object -Unique) }
    } catch {
        return @{ ok = $false; ids = @(); err = "$($_.Exception.Message)" }
    }
}

function Send-Alert([string]$token, [string]$text) {
    # best-effort, mirrors deploy/instances/watchdog_instances.ps1 payload/auth shape.
    # Target is the VPS group TG relay (AlertBase + EVENT_INGEST_KEY), NOT the main site --
    # the chengjie engine exposes no /api/ops/alert route. $token param kept for signature
    # stability but unused here.
    if ($DryRun) { Write-Log "DRYRUN" "would alert: $text"; return }
    if (-not $IngestKey) { Write-Log "WARN" "no EVENT_INGEST_KEY - alert logged only: $text"; return }
    try {
        $body = @{ text = $text; source = "wa-baileys-watchdog@$env:COMPUTERNAME" } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$AlertBase/api/ops/alert" -Method Post `
            -Headers @{ Authorization = "Bearer $IngestKey" } -ContentType "application/json" `
            -Body $body -TimeoutSec 10 -ErrorAction Stop | Out-Null
    } catch {
        Write-Log "WARN" "alert post failed (ignored): $($_.Exception.Message)"
    }
}

# -- self-heal primitives ----------------------------------------------------
function Invoke-Reconnect([string[]]$ids) {
    # gentle path: per-account reconnect endpoint (being added by a colleague; 404 tolerated)
    foreach ($id in $ids) {
        try {
            Invoke-RestMethod -Uri "$NodeBase/accounts/$id/reconnect" -Method Post `
                -ContentType "application/json" -Body "{}" -TimeoutSec 10 -ErrorAction Stop | Out-Null
            Write-Log "HEAL" "POST /accounts/$id/reconnect accepted"
        } catch {
            Write-Log "HEAL" "POST /accounts/$id/reconnect failed (tolerated): $($_.Exception.Message)"
        }
    }
}

function Get-PortOwnerPids {
    # netstat -ano based (per ops request); LISTENING rows whose local address ends in :$NodePort
    $result = @()
    $lines = @(netstat -ano | Select-String -Pattern "LISTENING")
    foreach ($ln in $lines) {
        if ("$ln" -match ('^\s*TCP\s+\S+:' + $NodePort + '\s+\S+\s+LISTENING\s+(\d+)\s*$')) {
            $result += [int]$Matches[1]
        }
    }
    return @($result | Where-Object { $_ -gt 0 } | Select-Object -Unique)
}

function Get-ProcInfo([int]$procId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
    if (-not $p) { return $null }
    return @{ name = "$($p.Name)"; cmd = "$($p.CommandLine)" }
}

function Test-IsBaileysNode($info) {
    # anti-friendly-fire: must be node.exe AND command line must reference whatsapp-baileys server.js
    if (-not $info) { return $false }
    if ($info.name -ne "node.exe") { return $false }
    return (($info.cmd -match "whatsapp-baileys") -and ($info.cmd -match "server\.js"))
}

function Invoke-HardRestart {
    # kill verified port owner + leftover baileys node processes, then re-launch via the
    # scheduled task. Returns @{ok; killed; reason}.
    $killed = @()
    foreach ($procId in (Get-PortOwnerPids)) {
        $info = Get-ProcInfo $procId
        if (Test-IsBaileysNode $info) {
            Write-Log "KILL" "stop pid=$procId (port $NodePort owner, verified baileys node.exe)"
            Stop-Process -Id $procId -Force
            $killed += $procId
        } else {
            $nm = "unknown"
            if ($info) { $nm = $info.name }
            return @{ ok = $false; killed = $killed;
                      reason = "port $NodePort owner pid=$procId is '$nm' (cmdline does not match whatsapp-baileys\server.js) - refusing to kill" }
        }
    }
    # leftovers: baileys node processes that lost the port (zombie after network drop)
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { ("$($_.CommandLine)" -match "whatsapp-baileys") -and ("$($_.CommandLine)" -match "server\.js") } |
        ForEach-Object {
            Write-Log "KILL" "stop leftover baileys node pid=$($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force
            $killed += [int]$_.ProcessId
        }
    Start-Sleep -Seconds 3
    # /End first: the task wrapper (powershell running start.ps1) may still be winding down
    # and default task policy ignores /Run while an instance is marked running.
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    schtasks /Run /TN $TaskName 2>&1 | Out-Null
    Write-Log "BOOT" "triggered scheduled task $TaskName"
    return @{ ok = $true; killed = @($killed | Select-Object -Unique) }
}

# -- main --------------------------------------------------------------------
$st  = Read-State
$now = Get-Epoch

$token = Get-AuthToken
if ($token -eq "") {
    Write-Log "SKIP" "auth_token not found under $DataDir\config - cannot query main site, exit"
    exit 0
}

$exp = Get-ExpectedAccounts $token
if (-not $exp.ok) {
    # main site down/unreachable: its own watchdog owns that problem; stay silent here
    Write-Log "SKIP" "main site not available ($($exp.err)) - silent exit"
    exit 0
}
if (@($exp.ids).Count -eq 0) {
    if ($st.missStreak -ne 0) { $st.missStreak = 0; Save-State $st }
    Write-Log "OK" "orchestrator manages no whatsapp accounts - nothing to watch"
    exit 0
}

$act = Get-NodeAccounts
$missing = @($exp.ids | Where-Object { @($act.ids) -notcontains $_ })

if ($act.ok -and $missing.Count -eq 0) {
    if ($st.missStreak -gt 0) {
        Write-Log "OK" "all expected accounts back ($($exp.ids -join ',')), streak reset"
    } else {
        Write-Log "OK" "all expected accounts present ($($exp.ids -join ','))"
    }
    $st.missStreak = 0
    Save-State $st
    exit 0
}

$reasonTxt = "missing=[$($missing -join ',')] expected=[$($exp.ids -join ',')] actual=[$(@($act.ids) -join ',')]"
if (-not $act.ok) { $reasonTxt = "node :$NodePort unreachable ($($act.err)); expected=[$($exp.ids -join ',')]" }

$newStreak = [int]$st.missStreak + 1
Write-Log "MISS" "$reasonTxt streak=$newStreak/$MissLimit"

if ($DryRun) {
    if ($newStreak -lt $MissLimit) {
        Write-Log "DRYRUN" "below miss limit - would only record streak and exit"
        exit 0
    }
    $sinceMin = 1e9
    if ($st.lastRestartTs -gt 0) { $sinceMin = ($now - $st.lastRestartTs) / 60 }
    if ($sinceMin -lt $RestartCooldownMin) {
        Write-Log "DRYRUN" ("would skip restart (cooldown: last restart {0:N0}m ago < ${RestartCooldownMin}m), alert only" -f $sinceMin)
    } elseif ($act.ok) {
        Write-Log "DRYRUN" "would try POST reconnect for [$($missing -join ',')], wait ${ReconnectWaitSec}s, then hard-restart via task $TaskName if still missing"
    } else {
        Write-Log "DRYRUN" "node HTTP down - would skip gentle reconnect and hard-restart via task $TaskName"
    }
    exit 0
}

$st.missStreak = $newStreak
Save-State $st

if ($newStreak -lt $MissLimit) {
    exit 0
}

# -- miss limit reached: self-heal -------------------------------------------
if ($act.ok) {
    # gentle path only makes sense while the Node HTTP interface is answering
    Invoke-Reconnect $missing
    Write-Log "HEAL" "reconnect requested for [$($missing -join ',')], waiting ${ReconnectWaitSec}s"
    Start-Sleep -Seconds $ReconnectWaitSec
    $act2 = Get-NodeAccounts
    $still = @($exp.ids | Where-Object { @($act2.ids) -notcontains $_ })
    if ($act2.ok -and $still.Count -eq 0) {
        Write-Log "RECOVERED" "reconnect brought all accounts back ($($exp.ids -join ','))"
        Send-Alert $token "[wa-baileys] accounts [$($missing -join ',')] were missing on :$NodePort for $newStreak rounds; POST /reconnect recovered them (no restart needed)."
        $st.missStreak = 0
        Save-State $st
        exit 0
    }
    $missing = $still
    Write-Log "HEAL" "still missing after reconnect: [$($missing -join ',')]"
    Send-Alert $token "[wa-baileys] accounts [$($missing -join ',')] missing on :$NodePort (streak $newStreak); POST /reconnect did not recover them, escalating to hard restart."
} else {
    Write-Log "HEAL" "node HTTP down - skipping gentle reconnect path"
    Send-Alert $token "[wa-baileys] node :$NodePort unreachable ($($act.err)) for $newStreak rounds; escalating to hard restart."
}

$sinceRestartMin = 1e9
if ($st.lastRestartTs -gt 0) { $sinceRestartMin = ($now - $st.lastRestartTs) / 60 }
if ($sinceRestartMin -lt $RestartCooldownMin) {
    Write-Log "COOLDOWN" ("accounts still missing but last restart was {0:N0}m ago (< ${RestartCooldownMin}m) - restart skipped" -f $sinceRestartMin)
    Send-Alert $token ("[wa-baileys] accounts [$($missing -join ',')] still missing but restart is in cooldown ({0:N0}m/{1}m) - skipped this round, manual check advised." -f $sinceRestartMin, $RestartCooldownMin)
    exit 0
}

Write-Log "RESTART" "hard restart: kill port $NodePort owner + leftovers, then schtasks /Run $TaskName"
$r = Invoke-HardRestart
if ($r.ok) {
    $st.missStreak = 0
    $st.lastRestartTs = $now
    Save-State $st
    Send-Alert $token "[wa-baileys] hard restart executed: killed node pid(s) [$($r.killed -join ',')], re-launched via task $TaskName (accounts missing: [$($missing -join ',')])."
    Write-Log "RESTART" "done (killed: [$($r.killed -join ',')]); next rounds re-verify"
} else {
    Write-Log "REFUSE" $r.reason
    Send-Alert $token "[wa-baileys] wanted to hard-restart but $($r.reason). Manual intervention needed."
}
exit 0
